"""Vista previa y publicación idempotente en Jira.

- `build_publication_preview`: Tarea "Test Plan - Regresión | <épica>" + un Test por caso, con los
  componentes de la épica, enlaces a la épica, al Test Plan y a cada historia cubierta.
- `JiraPublisher`: compara con lo existente (new/modified/unchanged/blocked/pending/potentially-obsolete),
  no duplica, no borra ni cierra, y retoma publicaciones parciales con un diario persistente.
- `JiraWriter`: interfaz de escritura. `McpJiraWriter` usa el MCP local sin modo lectura; solo se
  construye después de una aprobación válida.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from qa_regresion.config import ProjectProfile
from qa_regresion.domain.contracts import GherkinDesign
from qa_regresion.domain.enums import (
    CaseStatus,
    PublicationAction,
    PublicationClassification,
    WorkflowPhase,
)
from qa_regresion.domain.gherkin import render_scenario
from qa_regresion.domain.models import (
    AuditReport,
    ImpactRecord,
    PreviewLink,
    PreviewTest,
    PreviewTestPlan,
    PublicationItemPlan,
    PublicationPlan,
    PublicationPreview,
    WorkflowState,
)
from qa_regresion.domain.validation import fingerprint
from qa_regresion.publishers.approval import ApprovalGate, now_iso

HASH_MARKER = "qa-regresion-hash:"
PILOT_EXCLUDED = "fuera-del-piloto"
_TC_IN_SUMMARY = re.compile(r"\bTC-\d{3,}\b")


def epic_label(epic_key: str) -> str:
    return f"qa-epic-{epic_key}"


def _strip_run(text: str) -> str:
    return re.sub(r"(?m)^.*Identificador de ejecución:.*$", "", text)


# ---------------------------------------------------------------------- vista previa


_INTERNAL_ID = re.compile(r"(?<![\w`])(?:TD|FR|TR|TC|GAP|IMP|AF)-\d{3,}(?![\w`])")


def _code_ids(text: str) -> str:
    """Pone los IDs internos (TD-001, FR-002…) como código: Jira los tomaría por claves de incidencia."""
    return _INTERNAL_ID.sub(lambda m: f"`{m.group(0)}`", text)


def _test_description(case, profile: ProjectProfile) -> str:
    """Descripción del Test en Markdown (el MCP la convierte al formato de Jira)."""
    lines = []
    if case.status == CaseStatus.PENDING_BUSINESS_DECISION:
        lines += [f"> ⚠ **Pendiente de decisión de negocio:** {case.pending_reason}", ""]
    lines += [
        f"**Qué se prueba:** {_code_ids(case.title)}",
        "",
        "**Precondiciones:**",
        "",
        *[f"{i}. {_code_ids(p)}" for i, p in enumerate(case.preconditions, start=1)],
        "",
        f"**Datos de prueba:** `{case.dataset_id}`",
        "",
        f"**Prioridad de ejecución:** {case.priority.value} · **Suite:** {case.suite.value} · "
        f"**Automatización:** {case.automation.value} · **Estado:** {case.status.value}",
        "",
        "**Gherkin:**",
        "",
        "```gherkin",
        render_scenario(case, indent=""),
        "```",
        "",
        f"**Requisitos:** {_code_ids(', '.join(case.requirement_ids))}  ",
        f"**Hallazgos documentales:** {_code_ids(', '.join(case.finding_ids)) or '—'}  ",
        f"**Historias cubiertas:** {', '.join(case.story_keys) or '—'}  ",
        f"**Defectos:** {', '.join(case.defect_keys) or '—'}  ",
        f"**Fuentes:** {', '.join(case.source_ids)}",
    ]
    return "\n".join(lines)


def _plan_description(state: WorkflowState, design: GherkinDesign, audit: AuditReport,
                      impact: list[ImpactRecord], stories: list[str], out_of_scope: list[str],
                      associated: list, pilot_note: str | None) -> str:
    """Descripción del Test Plan en Markdown."""
    cases = design.cases
    published = [c for c in cases if c.status != CaseStatus.BLOCKED]
    cov = audit.coverage
    risks = [f"- {r.feature}: riesgo {r.risk.value} ({r.rationale})" for r in impact if r.risk.value == "Alta"]
    lines = [
        f"## Test Plan de regresión — {state.epic_key} {state.epic_name}",
        "",
        f"Identificador de ejecución: {state.run_id}",
        "",
        *([f"> {pilot_note}", ""] if pilot_note else []),
        "### Alcance",
        "",
        f"- Historias y mejoras: {', '.join(stories) or '—'}",
        f"- Fuera de alcance: {', '.join(out_of_scope) or '—'}",
        "",
        "### Riesgos",
        "",
        *(risks or ["- Sin riesgos altos identificados"]),
        "",
        "### Métricas",
        "",
        f"- Casos diseñados: {len(cases)} (publicables {len(published)}, pendientes de negocio "
        f"{sum(c.status == CaseStatus.PENDING_BUSINESS_DECISION for c in cases)}, "
        f"bloqueados {sum(c.status == CaseStatus.BLOCKED for c in cases)})",
        f"- Cobertura de requisitos: {cov.requirements.covered}/{cov.requirements.total}",
        f"- Cobertura de criterios de aceptación: {cov.acceptance_criteria.covered}/{cov.acceptance_criteria.total}",
        f"- Casos trazables: {cov.traceable_test_cases.covered}/{cov.traceable_test_cases.total}",
        "",
        "### Auditoría independiente",
        "",
        f"- Estado: {audit.audit_status.value} · listo para publicar: {audit.publication_ready}",
        f"- Hallazgos: críticos {len(audit.critical_findings)}, mayores {len(audit.major_findings)}, "
        f"menores {len(audit.minor_findings)}",
        "",
        "### Casos asociados",
        "",
        *[f"- `{c.id}` | {c.title} ({c.status.value})" for c in associated],
    ]
    return "\n".join(lines)


def _pilot_selection(cases: list, size: int) -> list:
    """Elige `size` casos priorizando historias distintas (para probar varios enlaces Relates)."""
    chosen, seen = [], set()
    # Primero los casos con una acción concreta (Dado/Cuando/Entonces), no los de verificación genérica.
    ordered = sorted(cases, key=lambda c: (c.when[0].startswith("el usuario accede a la funcionalidad"), c.id))
    for case in ordered:
        new_stories = set(case.story_keys) - seen
        if new_stories and case.status.value == "confirmed":
            chosen.append(case)
            seen |= set(case.story_keys)
        if len(chosen) == size:
            return sorted(chosen, key=lambda c: c.id)
    for case in cases:
        if case not in chosen:
            chosen.append(case)
        if len(chosen) == size:
            break
    return sorted(chosen, key=lambda c: c.id)


def build_publication_preview(state: WorkflowState, design: GherkinDesign, audit: AuditReport,
                              impact: list[ImpactRecord], profile: ProjectProfile, stories: list[str],
                              out_of_scope: list[str], attachments: list[str],
                              pilot_size: int | None = None) -> PublicationPreview:
    pub = profile.publication
    assert state.epic_key and state.epic_name is not None
    project = pub.project_key or state.project_key or state.epic_key.split("-")[0]
    components = sorted(state.epic_components) if pub.inherit_epic_components else []
    labels = sorted({*pub.labels, epic_label(state.epic_key)}) if pub.technical_labels else sorted(set(pub.labels))
    # Campos copiados de la épica (p. ej. Area Solicitante), en el formato que espera la API de Jira.
    extra = {fid: f["jira_value"] for fid, f in sorted(state.epic_fields.items())}
    inherited = {f["name"]: f["value"] for _fid, f in sorted(state.epic_fields.items())}
    not_published: list[dict[str, str]] = []
    publishable = []
    for case in design.cases:
        if case.status in (CaseStatus.BLOCKED, CaseStatus.EXCLUDED):
            not_published.append({"tc_id": case.id, "status": case.status.value,
                                  "reason": case.pending_reason or "caso bloqueado"})
        else:
            publishable.append(case)
    pilot_note = None
    if pilot_size and pilot_size < len(publishable):
        selected = _pilot_selection(publishable, pilot_size)
        for case in publishable:
            if case not in selected:
                not_published.append({"tc_id": case.id, "status": PILOT_EXCLUDED,
                                      "reason": "fuera del piloto; se publica en una corrida completa posterior"})
        pilot_note = (f"Piloto de publicación: {len(selected)} de {len(publishable)} casos. El resto se agrega "
                      f"después sobre esta misma Tarea, sin duplicar.")
        publishable = selected
    plan_description = _plan_description(state, design, audit, impact, stories, out_of_scope, publishable,
                                          pilot_note)
    plan_summary = f"Test Plan - Regresión | {state.epic_name}"
    epic_association = (f"hija de {state.epic_key} (campo padre)" if pub.epic_association == "parent"
                        else f"enlace {pub.epic_link_type} → {state.epic_key}")
    plan_hash = fingerprint({"summary": plan_summary, "description": _strip_run(plan_description),
                             "components": components, "labels": labels})
    test_plan = PreviewTestPlan(
        summary=plan_summary, issue_type=pub.test_plan_issue_type, project_key=project, epic_key=state.epic_key,
        epic_association=epic_association, components=components, labels=labels, extra_fields=extra,
        description=f"{plan_description}\n\n{HASH_MARKER} {plan_hash}", attachments=attachments,
        run_id=state.run_id, content_hash=plan_hash)
    tests: list[PreviewTest] = []
    plan_assoc = ("subtarea del Test Plan" if pub.test_plan_association == "parent"
                  else f"enlace {pub.test_plan_link_type} ↔ Test Plan")
    for case in publishable:
        summary = pub.test_summary_pattern.format(tc_id=case.id, title=case.title)
        test_labels = sorted({*labels, f"suite-{case.suite.value}", case.automation.value,
                              *(["pending-business-decision"] if case.status ==
                                CaseStatus.PENDING_BUSINESS_DECISION else [])}) if pub.technical_labels else labels
        description = _test_description(case, profile)
        links = [PreviewLink(link_type=pub.story_link_type, target=k) for k in case.story_keys]
        content_hash = fingerprint({"summary": summary, "description": description, "labels": test_labels,
                                    "components": components, "links": [l.model_dump() for l in links],
                                    "plan": plan_assoc})
        tests.append(PreviewTest(
            tc_id=case.id, summary=summary, issue_type=pub.test_case_issue_type, project_key=project,
            components=components, labels=test_labels, priority=case.priority, suite=case.suite,
            automation=case.automation, status=case.status,
            pending=case.status == CaseStatus.PENDING_BUSINESS_DECISION,
            description=f"{description}\n\n{HASH_MARKER} {content_hash}", test_plan_association=plan_assoc,
            extra_fields=extra, story_links=links, requirement_ids=case.requirement_ids,
            finding_ids=case.finding_ids,
            source_ids=case.source_ids, content_hash=content_hash))
    content_fp = fingerprint({"plan": plan_hash, "tests": [t.content_hash for t in tests],
                              "not_published": not_published})
    destination_fp = fingerprint({"project": project, "epic": state.epic_key, "components": components,
                                  "plan_type": pub.test_plan_issue_type, "test_type": pub.test_case_issue_type,
                                  "epic_assoc": pub.epic_association, "plan_assoc": pub.test_plan_association,
                                  "story_link": pub.story_link_type, "extra_fields": extra})
    return PublicationPreview(
        run_id=state.run_id, inherited_fields=inherited, epic_key=state.epic_key, epic_name=state.epic_name,
        project_key=project,
        epic_components=sorted(state.epic_components), dry_run=state.dry_run, publication_enabled=pub.enabled,
        approval_command=pub.approval_command, test_plan=test_plan, tests=tests, not_published=not_published,
        content_fingerprint=content_fp, destination_fingerprint=destination_fp)


def recompute_content_fingerprint(preview: PublicationPreview) -> str:
    """Recalcula la huella desde el contenido (detecta ediciones del JSON aunque no actualicen los hashes)."""
    tp = preview.test_plan
    plan_text = tp.description.rsplit(f"\n\n{HASH_MARKER}", 1)[0]
    plan_hash = fingerprint({"summary": tp.summary, "description": _strip_run(plan_text),
                             "components": tp.components, "labels": tp.labels})
    hashes = []
    for test in preview.tests:
        description = test.description.rsplit(f"\n\n{HASH_MARKER}", 1)[0]
        hashes.append(fingerprint({"summary": test.summary, "description": description, "labels": test.labels,
                                   "components": test.components,
                                   "links": [l.model_dump() for l in test.story_links],
                                   "plan": test.test_plan_association}))
    return fingerprint({"plan": plan_hash, "tests": hashes, "not_published": preview.not_published})


# ---------------------------------------------------------------------- escritura


class JiraWriter(Protocol):
    def find_issues(self, jql: str) -> list[dict[str, Any]]: ...

    def create_issue(self, fields: dict[str, Any]) -> str: ...

    def update_issue(self, key: str, fields: dict[str, Any]) -> None: ...

    def link_issues(self, link_type: str, inward_key: str, outward_key: str) -> None: ...

    def attach_files(self, key: str, paths: list[str]) -> None: ...


class McpJiraWriter:
    """Escritura vía el MCP local (sin READ_ONLY_MODE). Solo lo construye el comando de publicación."""

    def __init__(self, gateway: Any, profile: ProjectProfile):
        self.gateway = gateway
        self.tools = profile.atlassian.mcp.tools

    def find_issues(self, jql: str) -> list[dict[str, Any]]:
        data = self.gateway.call(self.tools.jira_search, {
            "jql": jql, "fields": "summary,description,labels,components,issuelinks,parent", "limit": 50})
        return data.get("issues", []) if isinstance(data, dict) else []

    def create_issue(self, fields: dict[str, Any]) -> str:
        extra: dict[str, Any] = {"labels": fields.get("labels", []), **(fields.get("extra_fields") or {})}
        if fields.get("parent"):
            extra["parent"] = fields["parent"]
        data = self.gateway.call(self.tools.jira_create_issue, {
            "project_key": fields["project"], "summary": fields["summary"], "issue_type": fields["issue_type"],
            "description": fields.get("description", ""), "components": ",".join(fields.get("components", [])),
            "additional_fields": json.dumps(extra, ensure_ascii=False)})
        issue = data.get("issue", data) if isinstance(data, dict) else {}
        key = issue.get("key")
        if not key:
            raise RuntimeError(f"Jira no devolvió una clave al crear {fields['summary']}")
        return key

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        payload = {k: v for k, v in fields.items() if k in ("summary", "description", "labels")}
        args: dict[str, Any] = {"issue_key": key, "fields": json.dumps(payload, ensure_ascii=False),
                                "return_fields": "summary"}
        if fields.get("components"):
            args["components"] = ",".join(fields["components"])
        if fields.get("extra_fields"):
            args["additional_fields"] = json.dumps(fields["extra_fields"], ensure_ascii=False)
        self.gateway.call(self.tools.jira_update_issue, args)

    def link_issues(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self.gateway.call(self.tools.jira_create_issue_link, {
            "link_type": link_type, "inward_issue_key": inward_key, "outward_issue_key": outward_key})

    def attach_files(self, key: str, paths: list[str]) -> None:
        data = self.gateway.call(self.tools.jira_update_issue, {
            "issue_key": key, "fields": "{}", "attachments": json.dumps(paths, ensure_ascii=False),
            "return_fields": "summary"})
        results = ((data or {}).get("issue") or {}).get("attachment_results")
        # El MCP no falla si un adjunto no sube: se exige la confirmación explícita de cada archivo.
        if not isinstance(results, dict) or results.get("failed") or len(results.get("uploaded") or []) != len(paths):
            raise RuntimeError(f"No se confirmaron los adjuntos de {key}: {str(results)[:300]}")


@dataclass
class PublicationJournal:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PublicationJournal":
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("test_plan_key", None)
        data.setdefault("tests", {})
        data.setdefault("links", [])
        data.setdefault("attachments", None)
        data.setdefault("events", [])
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def event(self, message: str) -> None:
        self.data["events"].append({"at": now_iso(), "event": message})
        self.save()


@dataclass
class PublicationResult:
    phase: WorkflowPhase
    plan: PublicationPlan
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    links_created: int = 0
    error: str | None = None


def _hash_of(issue: dict[str, Any]) -> str | None:
    m = re.search(re.escape(HASH_MARKER) + r"\s*([0-9a-f]{64})", issue.get("description") or "")
    return m.group(1) if m else None


class JiraPublisher:
    def __init__(self, writer: JiraWriter, profile: ProjectProfile, journal: PublicationJournal):
        self.writer = writer
        self.profile = profile
        self.pub = profile.publication
        self.journal = journal
        self._known_links: set[str] = set()

    @staticmethod
    def _link_marker(link_type: str, a: str, b: str) -> str:
        return f"{link_type.lower()}|{'|'.join(sorted((a, b)))}"

    def _remember_links(self, issue: dict[str, Any]) -> None:
        for link in issue.get("issuelinks") or []:
            name = link.get("type", {}).get("name") if isinstance(link.get("type"), dict) else link.get("type")
            for side in ("outward_issue", "inward_issue", "outwardIssue", "inwardIssue"):
                other = (link.get(side) or {}).get("key")
                if name and other and issue.get("key"):
                    self._known_links.add(self._link_marker(name, issue["key"], other))

    def _existing(self, preview: PublicationPreview) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
        """Busca el Test Plan y sus Tests ya publicados.

        Con relaciones padre (modelo del Test Plan de referencia) se buscan los hijos de la épica y las subtareas
        del plan; con etiquetas técnicas, por `qa-epic-<KEY>`. Un plan de otra convención (otro título) no se toca.
        """
        plan_type, test_type = self.pub.test_plan_issue_type, self.pub.test_case_issue_type
        if self.pub.epic_association == "parent":
            plans = self.writer.find_issues(f'parent = {preview.epic_key} AND issuetype = "{plan_type}"')
        else:
            label = epic_label(preview.epic_key)
            plans = self.writer.find_issues(
                f'project = {preview.project_key} AND labels = "{label}" AND issuetype = "{plan_type}"')
        plan = next((p for p in plans if p.get("summary") == preview.test_plan.summary), None)
        plan_key = (plan or {}).get("key") or self.journal.data.get("test_plan_key")
        if self.pub.test_plan_association == "parent":
            candidates = self.writer.find_issues(f'parent = {plan_key} AND issuetype = "{test_type}"') \
                if plan_key else []
        else:
            label = epic_label(preview.epic_key)
            candidates = self.writer.find_issues(
                f'project = {preview.project_key} AND labels = "{label}" AND issuetype = "{test_type}"')
        tests: dict[str, dict[str, Any]] = {}
        for issue in candidates:
            m = _TC_IN_SUMMARY.search(issue.get("summary") or "")
            if m:
                tests.setdefault(m.group(0), issue)
        for issue in [*([plan] if plan else []), *tests.values()]:
            self._remember_links(issue)
        return plan, tests

    def plan(self, preview: PublicationPreview) -> PublicationPlan:
        plan_issue, existing = self._existing(preview)
        items: list[PublicationItemPlan] = []
        plan_key = plan_issue.get("key") if plan_issue else self.journal.data.get("test_plan_key")
        if plan_key is None:
            items.append(PublicationItemPlan(ref="test-plan", kind="test-plan",
                                             classification=PublicationClassification.NEW,
                                             action=PublicationAction.CREATE))
        else:
            same = plan_issue is not None and _hash_of(plan_issue) == preview.test_plan.content_hash
            items.append(PublicationItemPlan(
                ref="test-plan", kind="test-plan", existing_key=plan_key,
                classification=PublicationClassification.UNCHANGED if same else PublicationClassification.MODIFIED,
                action=PublicationAction.SKIP if same else PublicationAction.UPDATE))
        for test in preview.tests:
            issue = existing.get(test.tc_id)
            key = issue.get("key") if issue else self.journal.data["tests"].get(test.tc_id)
            if key is None:
                cls = PublicationClassification.PENDING if test.pending else PublicationClassification.NEW
                items.append(PublicationItemPlan(ref=test.tc_id, kind="test", classification=cls,
                                                 action=PublicationAction.CREATE))
            elif issue is not None and _hash_of(issue) == test.content_hash:
                items.append(PublicationItemPlan(ref=test.tc_id, kind="test", existing_key=key,
                                                 classification=PublicationClassification.UNCHANGED,
                                                 action=PublicationAction.SKIP))
            else:
                cls = PublicationClassification.PENDING if test.pending else PublicationClassification.MODIFIED
                items.append(PublicationItemPlan(ref=test.tc_id, kind="test", existing_key=key, classification=cls,
                                                 action=PublicationAction.UPDATE))
        for blocked in preview.not_published:
            if blocked["status"] == PILOT_EXCLUDED:
                continue  # queda para la publicación completa; no es un bloqueo
            items.append(PublicationItemPlan(ref=blocked["tc_id"], kind="test",
                                             classification=PublicationClassification.BLOCKED,
                                             action=PublicationAction.REPORT_ONLY, reason=blocked["reason"]))
        current = {t.tc_id for t in preview.tests} | {b["tc_id"] for b in preview.not_published}
        for tc_id, issue in sorted(existing.items()):
            if tc_id not in current:
                items.append(PublicationItemPlan(
                    ref=tc_id, kind="test", existing_key=issue.get("key"),
                    classification=PublicationClassification.POTENTIALLY_OBSOLETE, action=PublicationAction.REPORT_ONLY,
                    reason="Existe en Jira pero no en este plan; no se cierra ni se elimina automáticamente."))
        return PublicationPlan(run_id=preview.run_id, items=items)

    def _link_once(self, link_type: str, inward: str, outward: str, result: PublicationResult) -> None:
        marker = self._link_marker(link_type, inward, outward)
        if marker in self.journal.data["links"] or marker in self._known_links:
            return
        self.writer.link_issues(link_type, inward, outward)
        self.journal.data["links"].append(marker)
        self.journal.save()
        result.links_created += 1

    def publish(self, preview: PublicationPreview, state: WorkflowState, gate: ApprovalGate,
                attachments: list[str]) -> PublicationResult:
        if not self.pub.enabled:
            raise PermissionError("la publicación está deshabilitada en el perfil (publication.enabled=false)")
        gate.consume(state, preview)  # valida token de un solo uso + contenido + destino
        plan = self.plan(preview)
        result = PublicationResult(phase=WorkflowPhase.PUBLISHING, plan=plan)
        by_ref = {i.ref: i for i in plan.items}
        self.journal.data["run_id"] = preview.run_id
        self.journal.event(f"inicio de publicación ({len(preview.tests)} Tests)")
        try:
            tp = preview.test_plan
            item = by_ref["test-plan"]
            plan_fields = {"project": tp.project_key, "summary": tp.summary, "issue_type": tp.issue_type,
                           "description": tp.description, "components": tp.components, "labels": tp.labels,
                           "extra_fields": tp.extra_fields}
            if self.pub.epic_association == "parent":
                plan_fields["parent"] = preview.epic_key
            if item.action == PublicationAction.CREATE:
                plan_key = self.writer.create_issue(plan_fields)
                result.created.append(plan_key)
            else:
                plan_key = item.existing_key
                assert plan_key
                if item.action == PublicationAction.UPDATE:
                    self.writer.update_issue(plan_key, plan_fields)
                    result.updated.append(plan_key)
                else:
                    result.unchanged.append(plan_key)
            self.journal.data["test_plan_key"] = plan_key
            self.journal.save()
            if self.pub.epic_association == "link":
                self._link_once(self.pub.epic_link_type, preview.epic_key, plan_key, result)

            for test in preview.tests:
                item = by_ref[test.tc_id]
                fields = {"project": test.project_key, "summary": test.summary, "issue_type": test.issue_type,
                          "description": test.description, "components": test.components, "labels": test.labels,
                          "extra_fields": test.extra_fields}
                if self.pub.test_plan_association == "parent":
                    fields["parent"] = plan_key
                if item.action == PublicationAction.CREATE:
                    key = self.writer.create_issue(fields)
                    result.created.append(key)
                else:
                    key = item.existing_key
                    assert key
                    if item.action == PublicationAction.UPDATE:
                        self.writer.update_issue(key, fields)
                        result.updated.append(key)
                    else:
                        result.unchanged.append(key)
                self.journal.data["tests"][test.tc_id] = key
                self.journal.save()
                if self.pub.test_plan_association == "link":
                    self._link_once(self.pub.test_plan_link_type, plan_key, key, result)
                for link in test.story_links:
                    self._link_once(link.link_type, key, link.target, result)

            attach_marker = preview.content_fingerprint
            if attachments and self.journal.data.get("attachments") != attach_marker:
                self.writer.attach_files(plan_key, attachments)
                self.journal.data["attachments"] = attach_marker
                self.journal.save()
            result.phase = WorkflowPhase.PUBLISHED
            self.journal.event("publicación completa")
        except Exception as exc:  # noqa: BLE001 - se deja constancia y se permite reintentar sin duplicar
            result.phase = WorkflowPhase.PARTIALLY_PUBLISHED
            result.error = f"{type(exc).__name__}: {exc}"
            self.journal.event(f"publicación parcial: {result.error}")
        return result

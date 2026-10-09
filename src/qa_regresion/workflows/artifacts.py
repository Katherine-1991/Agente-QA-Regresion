"""Escritura de artefactos locales y del resumen del borrador."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from qa_regresion.config import ProjectProfile
from qa_regresion.domain.enums import CaseStatus, GapKind, RequirementStatus, Tag
from qa_regresion.domain.models import WorkflowState, case_source_of

if TYPE_CHECKING:  # pragma: no cover
    from qa_regresion.agents.orchestrator import RunBundle

ATTACHMENTS = (
    "test-plan-regresion.md", "trazabilidad.csv", "matriz-impacto.csv", "catalogo-datos-prueba.md", "gaps.md",
    "source-manifest.json", "qa-validation-report.md",
)


def _csv(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()  # BOM: Excel abre bien los acentos


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def audit_yaml(report) -> str:
    data = {
        "audit_status": report.audit_status.value,
        "publication_ready": report.publication_ready,
        "critical_findings": [f"{f.id} [{f.category}] {f.message}" for f in report.critical_findings],
        "major_findings": [f"{f.id} [{f.category}] {f.message}" for f in report.major_findings],
        "minor_findings": [f"{f.id} [{f.category}] {f.message}" for f in report.minor_findings],
        "coverage": report.coverage.model_dump(),
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200)


class ArtifactWriter:
    def __init__(self, profile: ProjectProfile):
        self.profile = profile

    def save_state(self, state: WorkflowState, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "workflow-state.json"
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        return path

    def write_all(self, bundle: "RunBundle") -> dict[str, str]:
        out = bundle.output_dir
        assert out is not None and bundle.design is not None and bundle.audit is not None
        assert bundle.preview is not None
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}

        def write(name: str, content: str) -> None:
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            files[name] = str(path)

        features_dir = out / "features"
        features_dir.mkdir(exist_ok=True)
        for feature in bundle.design.features:
            write(f"features/{feature.file_name}", feature.text)
        write("trazabilidad.csv", self.traceability(bundle))
        write("matriz-impacto.csv", self.impact(bundle))
        write("catalogo-datos-prueba.md", self.data_catalog(bundle))
        write("gaps.md", self.gaps(bundle))
        write("source-manifest.json", self.manifest(bundle))
        write("qa-validation-report.md", self.qa_report(bundle))
        write("publication-preview.json", bundle.preview.model_dump_json(indent=2))
        write(REVIEW_FILE, render_case_review(bundle.preview))
        write("test-plan-regresion.md", self.test_plan(bundle))
        write("tc-registry.json", json.dumps(bundle.design.stable_ids, indent=2, sort_keys=True))
        write("analisis.json", json.dumps({
            "requirements": [r.model_dump(mode="json") for r in bundle.requirements],
            "findings": [f.model_dump(mode="json") for f in bundle.findings],
            "defects": [d.model_dump(mode="json") for d in bundle.defects],
            "candidates": [c.model_dump(mode="json") for c in bundle.design.candidates],
            "audits": [a.model_dump(mode="json") for a in bundle.audits],
        }, indent=2, ensure_ascii=False))
        return files

    # ------------------------------------------------------------------ CSV

    def traceability(self, bundle: "RunBundle") -> str:
        rows = [["TC", "titulo", "requisitos_FR", "hallazgos_TR", "defectos", "fuentes", "historias_jira",
                 "prioridad", "suite", "datos", "automatizacion", "estado", "etiquetas"]]
        for c in bundle.design.cases:
            rows.append([c.id, c.title, ", ".join(c.requirement_ids), ", ".join(c.finding_ids),
                         ", ".join(c.defect_keys), ", ".join(c.source_ids), ", ".join(c.story_keys),
                         c.priority.value, c.suite.value, c.dataset_id, c.automation.value, c.status.value,
                         " ".join(t.value for t in c.tags)])
        return _csv(rows)

    def impact(self, bundle: "RunBundle") -> str:
        rows = [["IMP", "funcionalidad", "historias", "requisitos_FR", "hallazgos_TR", "casos_TC", "defectos",
                 "aguas_arriba", "aguas_abajo", "riesgo", "fundamento"]]
        for r in bundle.impact:
            rows.append([r.id, r.feature, ", ".join(r.story_keys), ", ".join(r.requirement_ids),
                         ", ".join(r.finding_ids), ", ".join(r.test_case_ids), ", ".join(r.defect_keys),
                         ", ".join(r.upstream), ", ".join(r.downstream), r.risk.value, r.rationale])
        return _csv(rows)

    # ------------------------------------------------------------------ Markdown

    def data_catalog(self, bundle: "RunBundle") -> str:
        usage: dict[str, list[str]] = {}
        for c in bundle.design.cases:
            usage.setdefault(c.dataset_id, []).append(c.id)
        lines = [f"# Catálogo de datos de prueba — {bundle.state.epic_key}", "",
                 "| ID | Descripción | Aprovisionamiento | Valores / instrucciones | Requisitos | Casos |",
                 "|---|---|---|---|---|---|"]
        for d in bundle.design.datasets:
            detail = "; ".join(f"{k}={v}" for k, v in d.values.items()) if d.values else (d.notes or d.fixture_ref)
            lines.append(f"| {d.id} | {_md_cell(d.description)} | {d.provisioning.value} | {_md_cell(detail)} | "
                         f"{', '.join(d.requirement_ids)} | {', '.join(usage.get(d.id, []))} |")
        lines += ["", "Los datos `to-provision` deben prepararse en el ambiente de prueba antes de ejecutar; "
                      "no se incluyen credenciales ni datos personales reales."]
        return "\n".join(lines) + "\n"

    def gaps(self, bundle: "RunBundle") -> str:
        lines = [f"# Gaps y decisiones pendientes — {bundle.state.epic_key} {bundle.state.epic_name}", ""]
        order = [GapKind.CONTRADICTION, GapKind.DOC_ONLY_RULE, GapKind.MODIFIED_RULE, GapKind.MISSING_INFORMATION,
                 GapKind.INSUFFICIENT_EVIDENCE, GapKind.INACCESSIBLE_SOURCE, GapKind.IGNORED_INSTRUCTION,
                 GapKind.LIMITATION]
        titles = {
            GapKind.CONTRADICTION: "Contradicciones entre fuentes",
            GapKind.DOC_ONLY_RULE: "Reglas solo documentales (pendientes de negocio)",
            GapKind.MODIFIED_RULE: "Reglas modificadas",
            GapKind.MISSING_INFORMATION: "Información faltante",
            GapKind.INSUFFICIENT_EVIDENCE: "Evidencia insuficiente",
            GapKind.INACCESSIBLE_SOURCE: "Fuentes inaccesibles",
            GapKind.IGNORED_INSTRUCTION: "Instrucciones embebidas ignoradas (contenido no confiable)",
            GapKind.LIMITATION: "Limitaciones de la ejecución",
        }
        for kind in order:
            items = [g for g in bundle.gaps if g.kind == kind]
            if not items:
                continue
            lines += [f"## {titles[kind]}", ""]
            for g in items:
                refs = ", ".join([*g.related_requirements, *g.related_findings])
                lines.append(f"- **{g.id}** ({g.severity.value}) {g.description}" + (f" — {refs}" if refs else ""))
                if g.decision_needed:
                    lines.append(f"  - Decisión requerida: {g.decision_needed}")
                for e in g.evidence[:3]:
                    lines.append(f"  - Evidencia: `{e.source_id}` · {e.source_location} · «{e.paraphrase}» "
                                 f"({e.read_status.value})")
            lines.append("")
        sources = self.profile.design.case_sources
        no_case = [r for r in bundle.requirements
                   if r.status != RequirementStatus.EXCLUDED and case_source_of(r) not in sources]
        if no_case:
            lines += [f"## Requisitos sin caso propio (fuentes de casos: {', '.join(sources)})", "",
                      "Se analizaron y quedan trazados, pero no generan casos porque no son "
                      + ("criterios de aceptación." if sources == ["acceptance-criteria"] else "fuente de casos."), ""]
            lines += [f"- **{r.id}** [{case_source_of(r)}] {', '.join(r.story_keys) or '—'} · "
                      f"{_md_cell(r.statement[:180])}" for r in no_case]
            lines.append("")
        tr_counts = Counter(f.classification.value for f in bundle.findings)
        lines += ["## Clasificación de hallazgos documentales", "",
                  *[f"- {k}: {v}" for k, v in sorted(tr_counts.items())], ""]
        lines += ["## Hallazgos TR", "", "| TR | Clasificación | Tipo | Requisitos | Afirmación | Fuente |",
                  "|---|---|---|---|---|---|"]
        for f in bundle.findings:
            lines.append(f"| {f.id} | {f.classification.value} | {f.kind.value} | "
                         f"{', '.join(f.related_requirements) or '—'} | {_md_cell(f.statement[:160])} | "
                         f"{_md_cell(f.evidence[0].source_location)} |")
        return "\n".join(lines) + "\n"

    def manifest(self, bundle: "RunBundle") -> str:
        state = bundle.state
        data = {
            "run_id": state.run_id, "epic_key": state.epic_key, "epic_name": state.epic_name,
            "epic_components": state.epic_components, "epic_folder": state.epic_folder,
            "dry_run": state.dry_run, "remote_read_calls": state.remote_read_calls,
            "remote_write_calls": state.remote_write_calls,
            "sources": [s.manifest_entry() for s in bundle.all_sources()],
            "ignored_paths": bundle.ignored_paths,
            "ignored_instructions": state.ignored_instructions,
            "reference_mapping": bundle.collection.reference_mapping if bundle.collection else None,
            "limitations": state.limitations,
            "notes": bundle.run_notes,
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def qa_report(self, bundle: "RunBundle") -> str:
        final = bundle.audit
        lines = [f"# Reporte de validación QA — {bundle.state.epic_key} {bundle.state.epic_name}", "",
                 f"- Ejecución: `{bundle.state.run_id}` · ciclos de corrección: {bundle.state.repair_cycles}",
                 f"- Auditor: independiente (solo lectura; no crea ni reescribe casos)", "",
                 "## Resultado estructurado", "", "```yaml", audit_yaml(final).rstrip(), "```", "",
                 "## Controles", ""]
        for check in final.checks:
            lines.append(f"- [{'x' if check.passed else ' '}] {check.name} — {check.detail}")
        lines += ["", "## Historial de auditoría", ""]
        for report in bundle.audits:
            lines.append(f"- Ciclo {report.cycle}: {report.audit_status.value} (críticos "
                         f"{len(report.critical_findings)}, mayores {len(report.major_findings)}, menores "
                         f"{len(report.minor_findings)})")
        lines += ["", "## Hallazgos del ciclo final", ""]
        for f in final.all_findings():
            lines.append(f"- **{f.id}** {f.severity.value} · {f.category} · {f.message} → {f.responsible_agent.value}"
                         + (f" · {f.recommendation}" if f.recommendation else ""))
        if bundle.state.ignored_instructions:
            lines += ["", "## Instrucciones embebidas ignoradas", "",
                      "Estas frases provienen de fuentes no confiables y no tuvieron ningún efecto:", ""]
            lines += [f"- {_md_cell(x)}" for x in bundle.state.ignored_instructions]
        return "\n".join(lines) + "\n"

    def test_plan(self, bundle: "RunBundle") -> str:
        state, design, final, preview = bundle.state, bundle.design, bundle.audit, bundle.preview
        cases = design.cases
        reqs = bundle.requirements
        stories = sorted({k for r in reqs if r.status != RequirementStatus.EXCLUDED for k in r.story_keys})
        oos = [r for r in reqs if r.category.value == "out-of-scope"]
        tags = Counter(t.value for c in cases for t in c.tags)
        status = Counter(c.status.value for c in cases)
        sources = Counter(s.source_type.value for s in bundle.all_sources())
        read = Counter(s.read_status.value for s in bundle.all_sources())
        lines = [
            f"# Test Plan - Regresión | {state.epic_name}", "",
            f"- Épica: **{state.epic_key}** — {state.epic_name}",
            f"- Componentes (heredados por la Tarea y los Tests): {', '.join(state.epic_components) or '—'}",
            f"- Ejecución: `{state.run_id}` · modo: {'dry-run (solo lectura)' if state.dry_run else 'publicación'}",
            f"- Estado: **borrador** — publicación bloqueada hasta el mensaje exacto "
            f"`{self.profile.publication.approval_command}`", "",
            "## Alcance", "",
            f"- Historias/mejoras cubiertas: {', '.join(stories) or '—'}",
            "- Fuera de alcance:", *([f"  - {r.statement}" for r in oos] or ["  - —"]), "",
            "## Fuentes analizadas", "",
            *[f"- {k}: {v}" for k, v in sorted(sources.items())],
            f"- Estado de lectura: " + ", ".join(f"{k} {v}" for k, v in sorted(read.items())), "",
            "## Estrategia", "",
            "- `@smoke`: un caso de camino crítico por funcionalidad.",
            "- `@critical-path`: flujos positivos derivados de criterios de aceptación confirmados.",
            "- `@full-regression`: todos los casos publicables.",
            "- Los casos `@pending-business-decision` se publican marcados y no se ejecutan como bloqueantes.", "",
            "## Riesgos (matriz de impacto)", "",
            "| IMP | Funcionalidad | Riesgo | Fundamento | Casos |", "|---|---|---|---|---|",
            *[f"| {r.id} | {_md_cell(r.feature)} | {r.risk.value} | {_md_cell(r.rationale)} | "
              f"{', '.join(r.test_case_ids)} |" for r in bundle.impact], "",
            "## Métricas", "",
            f"- Requisitos FR: {len(reqs)} (activos {sum(r.status != RequirementStatus.EXCLUDED for r in reqs)})",
            f"- Hallazgos TR: {len(bundle.findings)}",
            f"- Casos TC: {len(cases)} — " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())),
            "- Etiquetas: " + ", ".join(f"{k} {v}" for k, v in sorted(tags.items())),
            f"- Cobertura de requisitos: {final.coverage.requirements.covered}/{final.coverage.requirements.total}",
            f"- Cobertura de criterios: {final.coverage.acceptance_criteria.covered}/"
            f"{final.coverage.acceptance_criteria.total}",
            f"- Casos trazables: {final.coverage.traceable_test_cases.covered}/"
            f"{final.coverage.traceable_test_cases.total}", "",
            "## Auditoría independiente", "",
            f"- Resultado: **{final.audit_status.value}** · listo para publicar: {final.publication_ready}",
            f"- Hallazgos: críticos {len(final.critical_findings)}, mayores {len(final.major_findings)}, "
            f"menores {len(final.minor_findings)} (detalle en `qa-validation-report.md`)", "",
            "## Casos", "",
            "| TC | Título | Prioridad | Suite | Automatización | Estado | Historias |", "|---|---|---|---|---|---|---|",
            *[f"| {c.id} | {_md_cell(c.title)} | {c.priority.value} | {c.suite.value} | {c.automation.value} | "
              f"{c.status.value} | {', '.join(c.story_keys) or '—'} |" for c in cases], "",
            "## Vista previa de publicación en Jira (no ejecutada)", "",
            f"- Relaciones: {relations_line(bundle)}",
            f"- Tarea: **{preview.test_plan.summary}** ({preview.test_plan.issue_type}, proyecto "
            f"{preview.project_key}, {preview.test_plan.epic_association}, componentes "
            f"{', '.join(preview.test_plan.components) or '—'})",
            f"- Tests a crear: {len(preview.tests)} ({preview.tests[0].issue_type if preview.tests else '—'}); "
            f"no publicados: {len(preview.not_published)}", "",
            "| Test | Asociación | Historias que se enlazarían | Pendiente |", "|---|---|---|---|",
            *[f"| {_md_cell(t.summary)} | {t.test_plan_association} | "
              f"{', '.join(l.target for l in t.story_links) or '— (solo épica/documentación)'} | "
              f"{'sí' if t.pending else 'no'} |" for t in preview.tests], "",
            "## Limitaciones", "", *([f"- {x}" for x in state.limitations] or ["- —"]), "",
            "## Artefactos", "",
            *[f"- `{name}`" for name in (*ATTACHMENTS, "publication-preview.json", "features/*.feature")],
        ]
        return "\n".join(lines) + "\n"


REVIEW_FILE = "borrador-casos-a-publicar.md"


def render_case_review(preview) -> str:
    """Borrador para revisión humana: exactamente lo que se crearía en Jira, caso por caso."""
    from qa_regresion.publishers.jira_publisher import HASH_MARKER, PILOT_EXCLUDED

    def body(text: str) -> str:
        return text.rsplit(f"\n\n{HASH_MARKER}", 1)[0]

    tp = preview.test_plan
    lines = [
        f"# Borrador para revisión — {tp.summary}", "",
        "> Esto es exactamente lo que se crearía en Jira. Revisá cada caso; la publicación solo se ejecuta "
        f"después de tu revisión y del mensaje independiente `{preview.approval_command}`.", "",
        f"- Ejecución: `{preview.run_id}` · épica {preview.epic_key} — {preview.epic_name}",
        f"- Tests a crear en esta publicación: **{len(preview.tests)}**", "",
        "## Tarea (Test Plan)", "",
        f"- Título: **{tp.summary}**",
        f"- Tipo: {tp.issue_type} · proyecto {tp.project_key}",
        f"- Relación con la épica: {tp.epic_association}",
        f"- Componentes: {', '.join(tp.components) or '—'} · Etiquetas: {', '.join(tp.labels) or '—'}",
        *[f"- {name}: {value} (heredado de la épica)" for name, value in preview.inherited_fields.items()],
        f"- Adjuntos: {', '.join(tp.attachments) or '—'}", "",
        "### Descripción de la Tarea", "", body(tp.description), "",
        f"## Tests ({len(preview.tests)})", "",
    ]
    for n, test in enumerate(preview.tests, start=1):
        lines += [
            f"### {n}. {test.summary}", "",
            f"- Tipo: {test.issue_type} · {test.test_plan_association}",
            f"- Historias enlazadas ({test.story_links[0].link_type if test.story_links else '—'}): "
            f"{', '.join(l.target for l in test.story_links) or '— (solo épica/documentación)'}",
            f"- Componentes: {', '.join(test.components) or '—'} · Etiquetas: {', '.join(test.labels) or '—'}"
            + "".join(f" · {name}: {value}" for name, value in preview.inherited_fields.items()),
            f"- Pendiente de negocio: {'sí' if test.pending else 'no'}", "",
            body(test.description), "", "---", "",
        ]
    pilot = [x["tc_id"] for x in preview.not_published if x["status"] == PILOT_EXCLUDED]
    other = [x for x in preview.not_published if x["status"] != PILOT_EXCLUDED]
    if pilot or other:
        lines += ["## No incluidos en esta publicación", ""]
        if pilot:
            lines.append(f"- Fuera del piloto ({len(pilot)}): {', '.join(pilot)}")
        for x in other:
            lines.append(f"- {x['tc_id']} ({x['status']}): {x['reason']}")
    return "\n".join(lines) + "\n"


def relations_line(bundle: "RunBundle") -> str:
    """Resumen de cómo se relacionarán Test Plan, épica, Tests e historias, y si coincide con la referencia."""
    preview = bundle.preview
    assert preview is not None
    story_type = next((l.link_type for t in preview.tests for l in t.story_links), "—")
    text = (f"Tarea → épica: {preview.test_plan.epic_association} · Test → Tarea: "
            f"{preview.tests[0].test_plan_association if preview.tests else '—'} · Test → historias: {story_type}")
    mapping = bundle.collection.reference_mapping if bundle.collection else None
    if mapping:
        mismatches = mapping.get("profile_mismatches") or []
        text += (f" (igual que {mapping['reference_issue']})" if not mismatches
                 else f" (DIFIERE de {mapping['reference_issue']}: {'; '.join(mismatches)})")
    return text


def render_draft(bundle: "RunBundle", approval_command: str) -> str:
    """Resumen del borrador y de la vista previa de Jira para mostrar en consola."""
    state = bundle.state
    lines = [f"Estado: {state.phase.value}", state.message]
    if bundle.design is None or bundle.preview is None or bundle.audit is None:
        if state.epic_candidates and state.phase.value == "AWAITING_EPIC_SELECTION":
            lines.append("Coincidencias:")
            lines += [f"  - {c.key} | {c.summary} ({c.status})" for c in state.epic_candidates]
        return "\n".join(x for x in lines if x)
    cases = bundle.design.cases
    final = bundle.audit
    preview = bundle.preview
    lines = [
        "=" * 78,
        f"BORRADOR — Test Plan - Regresión | {state.epic_name} ({state.epic_key})",
        "=" * 78,
        f"Ejecución {state.run_id} · dry-run · lecturas remotas {state.remote_read_calls} · "
        f"escrituras remotas {state.remote_write_calls}",
        f"Componentes de la épica: {', '.join(state.epic_components) or '—'}",
        f"FR {len(bundle.requirements)} · TR {len(bundle.findings)} · TC {len(cases)} · gaps {len(bundle.gaps)} · "
        f"ciclos de corrección {state.repair_cycles}",
        f"Auditoría: {final.audit_status.value} · publication_ready={final.publication_ready} · cobertura FR "
        f"{final.coverage.requirements.covered}/{final.coverage.requirements.total} · CA "
        f"{final.coverage.acceptance_criteria.covered}/{final.coverage.acceptance_criteria.total}",
        "",
        "Casos:",
    ]
    for c in cases:
        flag = {CaseStatus.PENDING_BUSINESS_DECISION: " [PENDIENTE]", CaseStatus.BLOCKED: " [BLOQUEADO]"}.get(
            c.status, "")
        lines.append(f"  {c.id} {c.title[:70]}{flag}")
        lines.append(f"         {' '.join(t.value for t in c.tags if t != Tag.FULL_REGRESSION)}")
    lines += [
        "",
        "Vista previa de Jira (NO publicada):",
        f"  Relaciones: {relations_line(bundle)}",
        f"  Tarea: {preview.test_plan.summary} · {preview.test_plan.epic_association} · componentes: "
        f"{', '.join(preview.test_plan.components) or '—'} · etiquetas: {', '.join(preview.test_plan.labels)}"
        + "".join(f" · {name}: {value}" for name, value in preview.inherited_fields.items()),
    ]
    for t in preview.tests:
        stories = ", ".join(l.target for l in t.story_links) or "—"
        lines.append(f"  Test: {t.summary[:70]} → historias: {stories}")
    if preview.not_published:
        lines.append(f"  No se publicarán: {', '.join(x['tc_id'] for x in preview.not_published)}")
    if state.ignored_instructions:
        lines += ["", f"Instrucciones embebidas ignoradas: {len(state.ignored_instructions)} (ver gaps.md)"]
    lines += ["", f"Artefactos: {state.output_dir}",
              f"Borrador de los casos para revisar antes de publicar: {Path(state.output_dir or '.') / REVIEW_FILE}",
              "", f"Estado final: {state.phase.value}"]
    if state.phase.value == "AWAITING_PUBLICATION_APPROVAL":
        lines.append(f"Revisar el borrador de los casos; recién después, para publicar, enviar como mensaje "
                     f"independiente exactamente: {approval_command}")
    else:
        lines.append(state.message)
    return "\n".join(lines)

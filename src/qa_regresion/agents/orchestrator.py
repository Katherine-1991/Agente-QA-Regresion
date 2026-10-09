"""Agente 1 — Líder QA y orquestador.

Recibe `/QA-Regresión <nombre>`, resuelve la épica, inventaria fuentes, ejecuta los validadores,
consolida (matriz de impacto), delega el diseño, envía al auditor independiente, gestiona hasta
dos ciclos de corrección y deja el borrador en AWAITING_PUBLICATION_APPROVAL. Nunca publica.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qa_regresion.adapters.atlassian_mcp import AtlassianCollection, AtlassianReader, GatewayError, ReadOnlyGuard
from qa_regresion.adapters.figma import DesignReader, UnavailableFigmaAdapter
from qa_regresion.adapters.filesystem import load_documents, prepare_output_dir, resolve_epic_folder, UnsafePathError
from qa_regresion.agents.documentation_validator import DocumentationValidator
from qa_regresion.agents.functional_validator import FunctionalValidator
from qa_regresion.agents.gherkin_designer import GherkinDesigner
from qa_regresion.agents.independent_auditor import IndependentAuditor
from qa_regresion.config import ProjectProfile, expand_pattern
from qa_regresion.domain.contracts import (
    AuditInput,
    DesignInput,
    DocumentationAnalysis,
    DocumentationInput,
    FunctionalAnalysis,
    FunctionalInput,
    GherkinDesign,
    RepairRequest,
)
from qa_regresion.domain.enums import (
    AgentName,
    AuditStatus,
    FindingKind,
    GapKind,
    Priority,
    RequirementCategory,
    RequirementStatus,
    Severity,
    SourceType,
    TraceClassification,
    WorkflowPhase,
)
from qa_regresion.domain.models import (
    AuditReport,
    BusinessGap,
    Evidence,
    HistoricalDefect,
    ImpactRecord,
    NormalizedRequirement,
    PublicationPreview,
    SourceRecord,
    TechnicalFinding,
    WorkflowState,
)
from qa_regresion.domain.text_analysis import (
    classify_category,
    detect_actors,
    detect_roles,
    extract_values,
    shorten,
    split_gwt,
)
from qa_regresion.domain.validation import SchemaViolation, WriteAttemptBlocked, fingerprint, validate_exchange
from qa_regresion.workflows.state_machine import WorkflowStateMachine

# Acepta "/QA-Regresión X" y la forma que deja Git Bash al convertir "/..." en ruta ("C:/Program Files/Git/QA-...").
COMMAND_RE = re.compile(r"^\s*(?:/|[A-Za-z]:[/\\][^\n]*?[/\\])QA-Regresi[oó]n\s+(?P<name>\S.*?)\s*$", re.IGNORECASE)
MAX_REPAIR_CYCLES = 2


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def parse_command(command: str) -> str:
    m = COMMAND_RE.match(command or "")
    if not m:
        raise ValueError("Formato esperado: /QA-Regresión <nombre de la épica>")
    return m.group("name").strip().strip('"').strip("'")


@dataclass
class RunBundle:
    state: WorkflowState
    collection: AtlassianCollection | None = None
    documents: list[SourceRecord] = field(default_factory=list)
    ignored_paths: list[str] = field(default_factory=list)
    functional: FunctionalAnalysis | None = None
    documentation: DocumentationAnalysis | None = None
    requirements: list[NormalizedRequirement] = field(default_factory=list)
    findings: list[TechnicalFinding] = field(default_factory=list)
    gaps: list[BusinessGap] = field(default_factory=list)
    defects: list[HistoricalDefect] = field(default_factory=list)
    impact: list[ImpactRecord] = field(default_factory=list)
    design: GherkinDesign | None = None
    audits: list[AuditReport] = field(default_factory=list)
    preview: PublicationPreview | None = None
    output_dir: Path | None = None
    epic_folder: Path | None = None
    run_notes: list[str] = field(default_factory=list)

    @property
    def audit(self) -> AuditReport | None:
        return self.audits[-1] if self.audits else None

    def all_sources(self) -> list[SourceRecord]:
        sources: list[SourceRecord] = []
        if self.collection:
            sources += [self.collection.epic, *self.collection.issues, *self.collection.comments,
                        *self.collection.confluence_pages]
        return sources + self.documents


class QALeadOrchestrator:
    name = AgentName.ORCHESTRATOR

    def __init__(self, profile: ProjectProfile, project_root: Path, reader: AtlassianReader,
                 read_guard: ReadOnlyGuard | None = None, figma: DesignReader | None = None,
                 functional: FunctionalValidator | None = None, documentation: DocumentationValidator | None = None,
                 designer: GherkinDesigner | None = None, auditor: IndependentAuditor | None = None,
                 artifact_writer: Any = None):
        self.profile = profile
        self.project_root = project_root
        self.reader = reader
        self.read_guard = read_guard
        self.figma = figma or UnavailableFigmaAdapter()
        self.functional = functional or FunctionalValidator(profile)
        self.documentation = documentation or DocumentationValidator()
        self.designer = designer or GherkinDesigner()
        self.auditor = auditor or IndependentAuditor()
        if artifact_writer is None:
            from qa_regresion.workflows.artifacts import ArtifactWriter
            artifact_writer = ArtifactWriter(profile)
        self.artifacts = artifact_writer

    # ------------------------------------------------------------------ ejecución

    def run(self, command: str, *, run_id: str | None = None, selected_key: str | None = None,
            pilot_size: int | None = None) -> RunBundle:
        self._pilot_size = pilot_size
        state = WorkflowState(run_id=run_id or new_run_id(), command=command, epic_query="", dry_run=True)
        sm = WorkflowStateMachine(state)
        bundle = RunBundle(state=state)
        try:
            state.epic_query = parse_command(command)
            sm.go(WorkflowPhase.RESOLVING_EPIC)
            candidates = self.reader.resolve_epic(state.epic_query)
            state.epic_candidates = candidates
            if selected_key:
                candidates = [c for c in candidates if c.key == selected_key.upper()]
                if not candidates:
                    sm.fail(f"{selected_key} no está entre las coincidencias de «{state.epic_query}»")
                    return bundle
            if not candidates:
                sm.fail(f"No se encontró ninguna épica para «{state.epic_query}»")
                return bundle
            if len(candidates) > 1:
                sm.go(WorkflowPhase.AWAITING_EPIC_SELECTION, f"{len(candidates)} coincidencias")
                state.message = "Hay varias épicas que coinciden; elegir una con --select <CLAVE>."
                return bundle
            epic = candidates[0]
            sm.go(WorkflowPhase.COLLECTING_SOURCES, epic.key)
            self._collect(bundle, epic.key, epic.project_key)

            sm.go(WorkflowPhase.VALIDATING_FUNCTIONAL)
            assert bundle.collection is not None
            f_input = validate_exchange(FunctionalInput, FunctionalInput(
                epic=bundle.collection.epic, issues=bundle.collection.issues, comments=bundle.collection.comments,
                confluence_pages=bundle.collection.confluence_pages), producer="orchestrator")
            bundle.functional = validate_exchange(FunctionalAnalysis, self.functional.run(f_input),
                                                  producer="functional_validator")

            sm.go(WorkflowPhase.VALIDATING_DOCUMENTATION)
            d_input = validate_exchange(DocumentationInput, DocumentationInput(
                epic_key=epic.key, documents=bundle.documents, requirements=bundle.functional.requirements,
                gap_offset=len(bundle.functional.gaps), limitations=state.limitations), producer="orchestrator")
            bundle.documentation = validate_exchange(DocumentationAnalysis, self.documentation.run(d_input),
                                                     producer="documentation_validator")

            sm.go(WorkflowPhase.CONSOLIDATING)
            self._consolidate(bundle)

            sm.go(WorkflowPhase.DESIGNING_CASES)
            design = validate_exchange(GherkinDesign, self.designer.design(self._design_input(bundle)),
                                       producer="gherkin_designer")

            cycle = 0
            while True:
                sm.go(WorkflowPhase.AUDITING, f"ciclo {cycle}")
                report = self._audit(bundle, design, cycle)
                bundle.audits.append(report)
                if report.audit_status != AuditStatus.FAIL or cycle >= MAX_REPAIR_CYCLES:
                    break
                cycle += 1
                state.repair_cycles = cycle
                sm.go(WorkflowPhase.REPAIRING, f"ciclo {cycle}: {len(report.all_findings())} hallazgos")
                design = self._repair(bundle, design, report, cycle)
            bundle.design = design

            sm.go(WorkflowPhase.GENERATING_DRAFT)
            self._draft(bundle)
            final = bundle.audit
            assert final is not None
            state.audit_status = final.audit_status
            state.publication_ready = final.publication_ready
            if final.audit_status == AuditStatus.FAIL:
                sm.go(WorkflowPhase.BLOCKED, f"auditoría FAIL tras {state.repair_cycles} ciclos de corrección")
                state.message = "Resultado bloqueado: la auditoría independiente no aprobó tras dos correcciones."
            else:
                sm.go(WorkflowPhase.AWAITING_PUBLICATION_APPROVAL, "borrador listo; publicación bloqueada")
                state.message = (f"Borrador listo. Para publicar, enviar como mensaje independiente exactamente "
                                 f"'{self.profile.publication.approval_command}'.")
        except WriteAttemptBlocked as exc:
            sm.fail(f"Se bloqueó un intento de escritura remota: {exc}")
        except GatewayError as exc:
            sm.fail(f"MCP de Atlassian no accesible: {exc}")
        except (SchemaViolation, UnsafePathError, ValueError) as exc:
            sm.fail(str(exc))
        finally:
            if self.read_guard is not None:
                state.remote_read_calls = len(self.read_guard.calls)
                state.remote_write_calls = self.read_guard.write_calls
            if bundle.output_dir is not None:
                self.artifacts.save_state(state, bundle.output_dir)
        return bundle

    # ------------------------------------------------------------------ pasos

    def _collect(self, bundle: RunBundle, epic_key: str, project_key: str | None) -> None:
        state = bundle.state
        collection = self.reader.collect(epic_key)
        bundle.collection = collection
        state.epic_key = epic_key
        state.epic_name = collection.epic.title
        state.epic_components = list(collection.epic.fields.get("components") or [])
        state.epic_fields = dict(collection.epic_fields)
        state.project_key = project_key or epic_key.split("-")[0]
        state.limitations += collection.limitations
        existing_tests = sorted(r.source_id for r in collection.issues
                                if self.profile.issue_kind(r.issue_type) == "test_case")
        if existing_tests:
            state.limitations.append(
                f"Se detectaron {len(existing_tests)} incidencias de tipo Test ya relacionadas con la épica "
                f"({existing_tests[0]} … {existing_tests[-1]}); no fueron creadas por este sistema y no se modifican.")
        if not state.epic_components:
            state.limitations.append(f"La épica {epic_key} no tiene componentes: la Tarea y los Tests quedarán "
                                     f"sin componentes (se heredan los de la épica).")

        folder = resolve_epic_folder(self.profile, self.project_root, epic_key)
        bundle.epic_folder = folder.path
        state.epic_folder = str(folder.expected_path)
        state.limitations += folder.limitations
        bundle.documents, bundle.ignored_paths = load_documents(folder.path, self.profile)
        figma_records, figma_limits = self.figma.read(epic_key)
        bundle.documents += figma_records
        state.limitations += figma_limits
        unreadable = [d for d in bundle.documents if d.read_status.value != "complete"]
        if unreadable:
            state.limitations.append(f"{len(unreadable)} documentos leídos parcialmente o inaccesibles "
                                     f"(detalle por archivo en gaps.md y source-manifest.json).")

        output = Path(expand_pattern(self.profile.output.root_pattern, {
            "epic-folder": str(folder.expected_path), "project-root": str(self.project_root),
            "epic-key": epic_key}))
        bundle.output_dir = output
        state.output_dir = str(output)
        registry = output / "tc-registry.json"
        self._stable_ids = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
        bundle.run_notes += prepare_output_dir(output, folder.path, self.profile.output.overwrite, state.run_id)

    def _consolidate(self, bundle: RunBundle) -> None:
        assert bundle.functional and bundle.documentation
        state = bundle.state
        requirements = [r.model_copy(deep=True) for r in bundle.functional.requirements]
        findings = [f.model_copy(deep=True) for f in bundle.documentation.findings]
        gaps = [*bundle.functional.gaps, *bundle.documentation.gaps]
        limit = self.profile.documents.max_doc_only_cases
        promoted = 0
        for tr in findings:
            if not (tr.generates_case and tr.classification == TraceClassification.SOLO_DOCUMENTACION):
                continue
            if promoted >= limit:
                tr.generates_case = False
                tr.note = "No se convirtió en caso: supera documents.max_doc_only_cases."
                continue
            promoted += 1
            fr_id = f"FR-{len(requirements) + 1:03d}"
            category = RequirementCategory.STATE_TRANSITION if tr.kind == FindingKind.STATE_MODEL \
                else classify_category(tr.statement)
            pre, cond, out = split_gwt(tr.statement.split(": ", 1)[-1])
            requirements.append(NormalizedRequirement(
                id=fr_id, statement=tr.statement, category=category,
                status=RequirementStatus.PENDING_BUSINESS_DECISION, feature=tr.feature or "Documentación",
                actors=detect_actors(tr.statement), roles=detect_roles(tr.statement),
                story_keys=[k for k in tr.story_keys if k in self._story_keys(bundle)],
                precondition=pre, condition=cond, outcome=out, values=extract_values(tr.statement),
                evidence=tr.evidence))
            tr.related_requirements = [fr_id]
            for gap in gaps:
                if gap.kind == GapKind.DOC_ONLY_RULE and tr.id in gap.related_findings:
                    gap.related_requirements = sorted(set(gap.related_requirements) | {fr_id})
        overflow = sum(1 for t in findings if t.note and "max_doc_only_cases" in t.note)
        if overflow:
            state.limitations.append(f"{overflow} reglas solo documentales no se convirtieron en casos (límite "
                                     f"{limit}); figuran en gaps.md.")
        next_gap = len(gaps) + 1
        for note in bundle.functional.ignored_instructions:
            source = note.split(":", 1)[0]
            gaps.append(BusinessGap(
                id=f"GAP-{next_gap:03d}", kind=GapKind.IGNORED_INSTRUCTION, severity=Severity.MINOR,
                description=f"Instrucción embebida ignorada en {source}; no tuvo efectos.",
                evidence=[Evidence(source_type=SourceType.JIRA_COMMENT if "#comment" in source else SourceType.JIRA_OTHER,
                                   source_id=source, source_location=source, paraphrase=shorten(note, 200),
                                   read_status="complete")]))
            next_gap += 1
        for limitation in state.limitations:
            gaps.append(BusinessGap(id=f"GAP-{next_gap:03d}", kind=GapKind.LIMITATION, severity=Severity.MINOR,
                                    description=limitation))
            next_gap += 1
        state.ignored_instructions = [*bundle.functional.ignored_instructions,
                                      *bundle.documentation.ignored_instructions]
        bundle.requirements, bundle.findings, bundle.gaps = requirements, findings, gaps
        bundle.defects = list(bundle.functional.defects)
        bundle.impact = self._impact(bundle)

    @staticmethod
    def _story_keys(bundle: RunBundle) -> set[str]:
        if not bundle.collection:
            return set()
        return {r.source_id for r in bundle.collection.issues
                if r.source_type in (SourceType.JIRA_STORY, SourceType.JIRA_IMPROVEMENT)}

    def _impact(self, bundle: RunBundle) -> list[ImpactRecord]:
        by_feature: dict[str, list[NormalizedRequirement]] = {}
        for req in bundle.requirements:
            if req.status != RequirementStatus.EXCLUDED:
                by_feature.setdefault(req.feature, []).append(req)
        contested = {fr for g in bundle.gaps if g.kind == GapKind.CONTRADICTION for fr in g.related_requirements}
        records = []
        for i, (feature, reqs) in enumerate(by_feature.items(), start=1):
            ids = [r.id for r in reqs]
            defects = sorted({d for r in reqs for d in r.defect_keys})
            cats = {r.category for r in reqs}
            reasons = []
            if defects:
                reasons.append(f"defectos históricos {', '.join(defects)}")
            if cats & {RequirementCategory.INTEGRATION}:
                reasons.append("integraciones observables")
            if cats & {RequirementCategory.ROLE_PERMISSION, RequirementCategory.STATE_TRANSITION}:
                reasons.append("roles o transiciones de estado")
            if set(ids) & contested:
                reasons.append("contradicción entre fuentes")
            risk = Priority.ALTA if reasons else (
                Priority.MEDIA if cats & {RequirementCategory.BOUNDARY, RequirementCategory.VALIDATION}
                else Priority.BAJA)
            records.append(ImpactRecord(
                id=f"IMP-{i:03d}", feature=feature, story_keys=sorted({k for r in reqs for k in r.story_keys}),
                requirement_ids=ids, finding_ids=sorted({t.id for t in bundle.findings
                                                         if set(t.related_requirements) & set(ids)}),
                defect_keys=defects, upstream=sorted({u for r in reqs for u in r.upstream}),
                downstream=sorted({d for r in reqs for d in r.downstream}), risk=risk,
                rationale="; ".join(reasons) or "reglas funcionales sin riesgos especiales"))
        return records

    def _design_input(self, bundle: RunBundle, stable: dict[str, str] | None = None) -> DesignInput:
        state = bundle.state
        assert state.epic_key and state.epic_name is not None
        return validate_exchange(DesignInput, DesignInput(
            epic_key=state.epic_key, epic_name=state.epic_name, requirements=bundle.requirements,
            findings=bundle.findings, gaps=bundle.gaps, defects=bundle.defects,
            stable_ids=stable if stable is not None else getattr(self, "_stable_ids", {}),
            max_doc_only_cases=self.profile.documents.max_doc_only_cases,
            language=self.profile.output.gherkin_language,
            case_sources=self.profile.design.case_sources), producer="orchestrator")

    def _audit(self, bundle: RunBundle, design: GherkinDesign, cycle: int) -> AuditReport:
        before = fingerprint(design)
        quarantined = [s.source_id for s in bundle.all_sources() if s.untrusted_instructions]
        audit_input = AuditInput(
            epic_key=bundle.state.epic_key or "", requirements=bundle.requirements, findings=bundle.findings,
            gaps=bundle.gaps, defects=bundle.defects, design=design.model_copy(deep=True),
            quarantined_source_ids=quarantined, known_story_keys=sorted(self._story_keys(bundle)), cycle=cycle,
            case_sources=self.profile.design.case_sources)
        report = validate_exchange(AuditReport, self.auditor.audit(audit_input), producer="independent_auditor")
        if fingerprint(design) != before:
            raise SchemaViolation("el auditor independiente modificó los casos (no está permitido)")
        return report

    def _repair(self, bundle: RunBundle, design: GherkinDesign, report: AuditReport, cycle: int) -> GherkinDesign:
        functional = [f for f in report.all_findings() if f.responsible_agent == AgentName.FUNCTIONAL_VALIDATOR]
        if functional:
            quarantined = {s.source_id for s in bundle.all_sources() if s.untrusted_instructions}
            for req in bundle.requirements:
                if any(e.source_id in quarantined for e in req.evidence):
                    req.evidence = [e for e in req.evidence if e.source_id not in quarantined] or req.evidence
                    if all(e.source_id in quarantined for e in req.evidence):
                        req.status = RequirementStatus.EXCLUDED
            design = self.designer.design(self._design_input(bundle, design.stable_ids))
        designer_findings = [f for f in report.all_findings() if f.responsible_agent == AgentName.GHERKIN_DESIGNER
                             and f.severity != Severity.MINOR]
        if designer_findings:
            design = self.designer.repair(self._design_input(bundle, design.stable_ids), design,
                                          RepairRequest(cycle=cycle, findings=designer_findings))
        return validate_exchange(GherkinDesign, design, producer="gherkin_designer")

    def _draft(self, bundle: RunBundle) -> None:
        from qa_regresion.publishers.jira_publisher import build_publication_preview
        from qa_regresion.workflows.artifacts import ATTACHMENTS

        assert bundle.design and bundle.audit and bundle.output_dir
        cases_by_fr: dict[str, list[str]] = {}
        for case in bundle.design.cases:
            for fr in case.requirement_ids:
                cases_by_fr.setdefault(fr, []).append(case.id)
        for record in bundle.impact:
            record.test_case_ids = sorted({tc for fr in record.requirement_ids for tc in cases_by_fr.get(fr, [])})
        stories = sorted(self._story_keys(bundle))
        out_of_scope = sorted({k for r in bundle.requirements if r.category == RequirementCategory.OUT_OF_SCOPE
                               for k in r.story_keys})
        if self.read_guard is not None:  # se registran antes de escribir el manifiesto
            bundle.state.remote_read_calls = len(self.read_guard.calls)
            bundle.state.remote_write_calls = self.read_guard.write_calls
        bundle.preview = build_publication_preview(bundle.state, bundle.design, bundle.audit, bundle.impact,
                                                   self.profile, stories, out_of_scope, list(ATTACHMENTS),
                                                   pilot_size=getattr(self, "_pilot_size", None))
        bundle.state.draft_fingerprint = bundle.preview.content_fingerprint
        bundle.state.destination_fingerprint = bundle.preview.destination_fingerprint
        bundle.state.artifacts = self.artifacts.write_all(bundle)

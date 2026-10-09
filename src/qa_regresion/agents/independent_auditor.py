"""Agente 5 — Auditor QA independiente.

Solo lee: recibe una copia del diseño y devuelve un AuditReport. No crea ni reescribe casos;
el orquestador verifica con una huella que el diseño no cambió durante la auditoría.
"""

from __future__ import annotations

from collections import defaultdict

from qa_regresion.domain.contracts import AuditInput
from qa_regresion.domain.enums import (
    AgentName,
    AuditStatus,
    CaseKind,
    CaseStatus,
    DataProvisioning,
    GapKind,
    ReadStatus,
    RequirementCategory,
    RequirementStatus,
    Severity,
    Tag,
    TraceClassification,
)
from qa_regresion.domain.gherkin import step_signature, validate_feature
from qa_regresion.domain.models import (
    AuditCheck,
    AuditFinding,
    AuditReport,
    Coverage,
    CoverageMetric,
    case_source_of,
)

COVERING = (CaseStatus.CONFIRMED, CaseStatus.PENDING_BUSINESS_DECISION)


class IndependentAuditor:
    name = "independent_auditor"

    def audit(self, inp: AuditInput) -> AuditReport:
        findings: list[AuditFinding] = []
        checks: list[AuditCheck] = []

        def add(severity: Severity, category: str, message: str, targets: list[str], agent: AgentName,
                recommendation: str = "") -> None:
            findings.append(AuditFinding(id=f"AF-{len(findings) + 1:03d}", severity=severity, category=category,
                                         message=message, target_ids=targets, responsible_agent=agent,
                                         recommendation=recommendation))

        design = inp.design
        cases = design.cases
        reqs = {r.id: r for r in inp.requirements}
        all_active = [r for r in inp.requirements if r.status != RequirementStatus.EXCLUDED]
        # Solo los requisitos de las fuentes configuradas (design.case_sources) deben tener caso propio.
        active = [r for r in all_active if case_source_of(r) in inp.case_sources]
        without_source = [r.id for r in all_active if case_source_of(r) not in inp.case_sources]
        datasets = {d.id: d for d in design.datasets}
        covered_by: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            if case.status in COVERING:
                for fr in case.requirement_ids:
                    covered_by[fr].append(case.id)

        # 1. Trazabilidad y evidencia.
        bad_trace = []
        for case in cases:
            unknown = [r for r in case.requirement_ids if r not in reqs]
            if unknown or not case.evidence or not case.source_ids:
                bad_trace.append(case.id)
                add(Severity.CRITICAL, "broken-traceability", f"{case.id} referencia requisitos inexistentes o no "
                    f"tiene evidencia/fuentes ({unknown})", [case.id], AgentName.GHERKIN_DESIGNER)
            excluded = [r for r in case.requirement_ids if r in reqs and reqs[r].status == RequirementStatus.EXCLUDED]
            if excluded and case.status != CaseStatus.EXCLUDED:
                add(Severity.MAJOR, "excluded-requirement-tested",
                    f"{case.id} prueba requisitos excluidos o fuera de alcance: {excluded}", [case.id],
                    AgentName.GHERKIN_DESIGNER)
            quarantined = [s for s in case.source_ids if s in inp.quarantined_source_ids]
            if quarantined:
                add(Severity.CRITICAL, "untrusted-source", f"{case.id} usa como evidencia una fuente con "
                    f"instrucciones embebidas: {quarantined}", [case.id], AgentName.FUNCTIONAL_VALIDATOR)
        checks.append(AuditCheck(name="trazabilidad", passed=not bad_trace,
                                 detail=f"{len(cases) - len(bad_trace)}/{len(cases)} casos trazables"))

        # 2. Requisitos y criterios cubiertos.
        uncovered = [r for r in active if not covered_by.get(r.id)]
        justified = {fr for g in inp.gaps for fr in g.related_requirements}
        for req in uncovered:
            blocked_only = any(req.id in c.requirement_ids for c in cases if c.status == CaseStatus.BLOCKED)
            if blocked_only or req.id in justified and all(e.read_status == ReadStatus.INACCESSIBLE
                                                           for e in req.evidence):
                add(Severity.MINOR, "blocked-requirement", f"{req.id} solo tiene casos bloqueados", [req.id],
                    AgentName.GHERKIN_DESIGNER)
            else:
                category = ("uncovered-acceptance-criterion" if req.acceptance_criterion_ref
                            else "defect-not-covered" if req.category == RequirementCategory.DEFECT_REGRESSION
                            else "uncovered-requirement")
                add(Severity.MAJOR, category, f"{req.id} no está cubierto por ningún caso", [req.id],
                    AgentName.GHERKIN_DESIGNER, "Diseñar al menos un caso para el requisito.")
        ac_total = [r for r in active if r.acceptance_criterion_ref]
        ac_covered = [r for r in ac_total if covered_by.get(r.id)]
        checks.append(AuditCheck(name="requisitos cubiertos", passed=not uncovered,
                                 detail=f"{len(active) - len(uncovered)}/{len(active)}"))

        # 3. Riesgos: defectos históricos y límites.
        defect_cases = {k for c in cases if Tag.DEFECT_REGRESSION in c.tags for k in c.defect_keys}
        for defect in (inp.defects if "defects" in inp.case_sources else []):
            if defect.key not in defect_cases:
                fr_ids = [r.id for r in active if defect.key in r.defect_keys]
                add(Severity.MAJOR, "defect-not-covered", f"El defecto {defect.key} no tiene caso "
                    f"@defect-regression", fr_ids or [defect.key], AgentName.GHERKIN_DESIGNER)
        for req in active:
            if req.category == RequirementCategory.BOUNDARY and req.values and not any(
                    req.id in c.requirement_ids and c.kind == CaseKind.BOUNDARY for c in cases):
                add(Severity.MINOR, "missing-boundary-case", f"{req.id} define límites sin caso @boundary",
                    [req.id], AgentName.GHERKIN_DESIGNER)
        checks.append(AuditCheck(name="defectos históricos", passed="defects" not in inp.case_sources
                                 or len(defect_cases) >= len(inp.defects),
                                 detail=f"{len(defect_cases)}/{len(inp.defects)} con caso de regresión"))

        # 4. Duplicados y calidad Gherkin.
        signatures: dict[str, str] = {}
        for case in cases:
            sig = step_signature(case.given, case.when, case.then) + repr(case.examples_rows)
            if sig in signatures:
                add(Severity.CRITICAL, "duplicate-case", f"{case.id} duplica a {signatures[sig]}",
                    [case.id, signatures[sig]], AgentName.GHERKIN_DESIGNER)
            signatures[sig] = case.id
        gherkin_errors = 0
        for feature in design.features:
            for error in validate_feature(feature.text):
                gherkin_errors += 1
                add(Severity.CRITICAL, "invalid-gherkin", f"{feature.file_name}: {error}", feature.case_ids,
                    AgentName.GHERKIN_DESIGNER)
        listed = {cid for f in design.features for cid in f.case_ids}
        missing = [c.id for c in cases if c.id not in listed]
        if missing:
            add(Severity.CRITICAL, "case-without-feature", f"Casos fuera de todo .feature: {missing}", missing,
                AgentName.GHERKIN_DESIGNER)
        checks.append(AuditCheck(name="calidad Gherkin", passed=gherkin_errors == 0 and not missing,
                                 detail=f"{gherkin_errors} errores"))

        # 5. Datos reproducibles, prioridades, suites y automatización.
        for case in cases:
            dataset = datasets.get(case.dataset_id)
            if dataset is None:
                add(Severity.MAJOR, "missing-dataset", f"{case.id} usa {case.dataset_id}, ausente del catálogo",
                    [case.id], AgentName.GHERKIN_DESIGNER)
            elif case.automation.value == "automation-candidate" and \
                    dataset.provisioning == DataProvisioning.TO_PROVISION:
                add(Severity.MINOR, "automation-without-data", f"{case.id} es candidato a automatizar con datos "
                    f"sin aprovisionar", [case.id], AgentName.GHERKIN_DESIGNER)
        checks.append(AuditCheck(name="datos reproducibles", passed=all(c.dataset_id in datasets for c in cases)))

        # 6. Relaciones con historias.
        known = set(inp.known_story_keys)
        for case in cases:
            expected = {k for r in case.requirement_ids if r in reqs for k in reqs[r].story_keys}
            missing_links = expected - set(case.story_keys)
            if missing_links:
                add(Severity.MAJOR, "missing-story-link", f"{case.id} no enlaza {sorted(missing_links)}",
                    [case.id], AgentName.GHERKIN_DESIGNER)
            unknown_links = set(case.story_keys) - known
            if unknown_links:
                add(Severity.MAJOR, "unknown-story-link", f"{case.id} enlaza historias no relevadas "
                    f"{sorted(unknown_links)}", [case.id], AgentName.GHERKIN_DESIGNER)

        # 7. Casos pendientes y reglas solo documentales / contradicciones.
        contested = {fr for g in inp.gaps if g.kind == GapKind.CONTRADICTION for fr in g.related_requirements}
        doc_only = {fr for t in inp.findings if t.classification == TraceClassification.SOLO_DOCUMENTACION
                    for fr in t.related_requirements}
        pending_frs = {r.id for r in active if r.status == RequirementStatus.PENDING_BUSINESS_DECISION}
        must_be_pending = contested | doc_only | pending_frs
        for case in cases:
            if case.status == CaseStatus.CONFIRMED and set(case.requirement_ids) & must_be_pending:
                add(Severity.MAJOR, "pending-status-missing", f"{case.id} se apoya en una regla en disputa o solo "
                    f"documental y no está marcado como pendiente", [case.id], AgentName.GHERKIN_DESIGNER)
        if without_source:
            add(Severity.MINOR, "requirements-without-case-source",
                f"{len(without_source)} requisitos no son fuente de casos según design.case_sources "
                f"({', '.join(inp.case_sources)}); quedan trazados en gaps.md sin caso propio",
                without_source[:50], AgentName.ORCHESTRATOR)
        pending_cases = [c.id for c in cases if c.status == CaseStatus.PENDING_BUSINESS_DECISION]
        if pending_cases:
            add(Severity.MINOR, "pending-cases", f"{len(pending_cases)} casos quedan @pending-business-decision",
                pending_cases, AgentName.ORCHESTRATOR, "Resolver los gaps con negocio antes de ejecutar.")
        blocked_cases = [c.id for c in cases if c.status == CaseStatus.BLOCKED]
        if blocked_cases:
            add(Severity.MINOR, "blocked-cases", f"{len(blocked_cases)} casos bloqueados no se publicarán",
                blocked_cases, AgentName.ORCHESTRATOR)

        # 8. Cobertura de tipos por funcionalidad.
        by_feature: dict[str, set[CaseKind]] = defaultdict(set)
        for case in cases:
            by_feature[case.feature].add(case.kind)
        for feature, kinds in by_feature.items():
            if len(kinds) > 1 and not kinds & {CaseKind.NEGATIVE, CaseKind.BOUNDARY, CaseKind.ROLE}:
                add(Severity.MINOR, "no-negative-case", f"«{feature}» no tiene casos negativos", [],
                    AgentName.GHERKIN_DESIGNER)

        traceable = [c for c in cases if c.id not in bad_trace]
        coverage = Coverage(
            requirements=CoverageMetric(covered=len(active) - len(uncovered), total=len(active)),
            acceptance_criteria=CoverageMetric(covered=len(ac_covered), total=len(ac_total)),
            traceable_test_cases=CoverageMetric(covered=len(traceable), total=len(cases)))
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        major = [f for f in findings if f.severity == Severity.MAJOR]
        minor = [f for f in findings if f.severity == Severity.MINOR]
        if critical or major:
            status = AuditStatus.FAIL
        elif minor:
            status = AuditStatus.PASS_WITH_WARNINGS
        else:
            status = AuditStatus.PASS
        publishable = [c for c in cases if c.status in COVERING]
        checks.append(AuditCheck(name="preparación para publicación", passed=status != AuditStatus.FAIL and
                                 bool(publishable), detail=f"{len(publishable)} casos publicables"))
        return AuditReport(audit_status=status, publication_ready=status != AuditStatus.FAIL and bool(publishable),
                           critical_findings=critical, major_findings=major, minor_findings=minor,
                           coverage=coverage, cycle=inp.cycle, checks=checks)

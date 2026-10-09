from __future__ import annotations

import pytest

from conftest import SIM_COMMAND, make_harness
from qa_regresion.agents.gherkin_designer import GherkinDesigner, conjugate_action
from qa_regresion.agents.independent_auditor import IndependentAuditor
from qa_regresion.agents.orchestrator import MAX_REPAIR_CYCLES
from qa_regresion.domain.contracts import AuditInput, DesignInput, GherkinDesign
from qa_regresion.domain.enums import (
    AuditStatus,
    CandidateDecision,
    CaseKind,
    CaseStatus,
    ReadStatus,
    RequirementCategory,
    SourceType,
    WorkflowPhase,
)
from qa_regresion.domain.models import Evidence, NormalizedRequirement
from qa_regresion.domain.validation import SchemaViolation, fingerprint


def ev(source="QAS-1", status=ReadStatus.COMPLETE):
    return Evidence(source_type=SourceType.JIRA_STORY, source_id=source, source_location="CA01",
                    paraphrase="regla", read_status=status)


def fr(i, statement, category=RequirementCategory.ACCEPTANCE_CRITERION, **kw):
    data = dict(id=f"FR-{i:03d}", statement=statement, category=category, feature="Alta",
                story_keys=["QAS-1"], acceptance_criterion_ref=f"QAS-1/CA{i:02d}", evidence=[ev()])
    data.update(kw)
    return NormalizedRequirement(**data)


def design_for(reqs, **kw):
    inp = DesignInput(epic_key="QAS-100", epic_name="Épica", requirements=reqs, **kw)
    return inp, GherkinDesigner().design(inp)


# ------------------------------------------------------------------ diseñador


def test_duplicate_requirements_are_merged():
    a = fr(1, "Al presionar Guardar, el sistema registra la solicitud.")
    b = fr(2, "Al presionar Guardar, el sistema registra la solicitud.")
    _, design = design_for([a, b])
    merged = [c for c in design.candidates if c.decision == CandidateDecision.MERGED]
    assert len(merged) == 1
    assert len(design.cases) == 1
    assert design.cases[0].requirement_ids == ["FR-001", "FR-002"]


def test_inaccessible_evidence_blocks_case():
    req = fr(1, "Al presionar Guardar, el sistema registra la solicitud.",
             evidence=[ev(status=ReadStatus.INACCESSIBLE)])
    _, design = design_for([req])
    assert design.cases[0].status == CaseStatus.BLOCKED
    assert any(c.decision == CandidateDecision.BLOCKED for c in design.candidates)


def test_selectors_are_rewritten():
    req = fr(1, "Al presionar el botón //button[@id='guardar'], el sistema registra la solicitud.")
    _, design = design_for([req])
    assert any(c.decision == CandidateDecision.REWRITTEN for c in design.candidates)
    assert "//button" not in design.features[0].text


def test_multiple_actions_are_split():
    req = fr(1, "Al completar el formulario y luego presionar Enviar, el sistema muestra la confirmación.")
    _, design = design_for([req])
    assert any(c.decision == CandidateDecision.SPLIT for c in design.candidates)
    assert len(design.cases) == 2


def test_boundary_case_uses_explicit_examples():
    req = fr(1, "El monto máximo es 5.000.", category=RequirementCategory.BOUNDARY)
    _, design = design_for([req])
    boundary = next(c for c in design.cases if c.kind == CaseKind.BOUNDARY)
    assert boundary.examples_rows == [["5000", "acepta"], ["5001", "rechaza"]]


def test_acceptance_criterion_produces_exactly_one_case():
    req = fr(1, 'Solo el rol "Supervisor" puede aprobar devoluciones.',
             category=RequirementCategory.ROLE_PERMISSION, roles=["Supervisor"])
    _, design = design_for([req])
    assert len(design.cases) == 1  # el criterio define el caso; no se inventan complementos
    case = design.cases[0]
    assert case.title.startswith("CA01 - ")
    assert case.when == ["el usuario aprueba devoluciones"]
    assert case.then == ["el sistema permite aprobar devoluciones"]


def test_business_rule_role_produces_authorized_and_unauthorized_cases():
    req = fr(1, 'Solo el rol "Supervisor" puede aprobar devoluciones.',
             category=RequirementCategory.ROLE_PERMISSION, roles=["Supervisor"], acceptance_criterion_ref=None)
    _, design = design_for([req])
    thens = sorted(c.then[0] for c in design.cases)
    assert thens == ["el sistema no permite aprobar devoluciones", "el sistema permite aprobar devoluciones"]


def test_gwt_acceptance_criterion_keeps_its_steps():
    req = fr(1, "Ingreso de sector: Dado que el usuario se encuentra en el buscador, cuando selecciona un sector, "
                "entonces el sistema debe registrar el sector seleccionado.", title="Ingreso de sector",
             precondition="el usuario se encuentra en el buscador", condition="selecciona un sector",
             outcome="el sistema debe registrar el sector seleccionado")
    _, design = design_for([req])
    case = design.cases[0]
    assert case.title == "CA01 - Ingreso de sector"
    assert case.given[0] == "el usuario se encuentra en el buscador"
    assert case.when == ["el usuario selecciona un sector"]
    assert case.then == ["el sistema debe registrar el sector seleccionado"]
    assert len(case.preconditions) == len(set(case.preconditions))


def test_case_sources_restrict_cases_to_acceptance_criteria():
    ac = fr(1, "Al presionar Guardar, el sistema registra la solicitud.")
    rule = fr(2, "El sistema debe registrar la fecha de alta.", category=RequirementCategory.BUSINESS_RULE,
              acceptance_criterion_ref=None)
    inp, design = design_for([ac, rule], case_sources=["acceptance-criteria"])
    assert [c.requirement_ids for c in design.cases] == [["FR-001"]]
    report = IndependentAuditor().audit(AuditInput(epic_key="QAS-100", requirements=[ac, rule], design=design,
                                                   known_story_keys=["QAS-1"], case_sources=["acceptance-criteria"]))
    assert report.audit_status == AuditStatus.PASS_WITH_WARNINGS  # la regla sin CA se informa, no falla
    assert report.coverage.requirements.total == 1
    assert any(f.category == "requirements-without-case-source" and "FR-002" in f.target_ids
               for f in report.minor_findings)


def test_stable_ids_are_reused_across_runs():
    reqs = [fr(1, "Al presionar Guardar, el sistema registra la solicitud."),
            fr(2, "Al presionar Borrar, el sistema elimina el borrador.")]
    _, first = design_for(reqs)
    _, second = design_for(list(reversed(reqs)), stable_ids=first.stable_ids)
    assert {c.title: c.id for c in first.cases} == {c.title: c.id for c in second.cases}


def test_conjugation():
    assert conjugate_action("Al presionar Buscar") == "el usuario presiona Buscar"
    assert conjugate_action("aprobar la solicitud") == "el usuario aprueba la solicitud"
    assert conjugate_action("selecciona la opción X") == "el usuario selecciona la opción X"


# ------------------------------------------------------------------ auditor


def audit(reqs, design, **kw):
    return IndependentAuditor().audit(AuditInput(epic_key="QAS-100", requirements=reqs, design=design,
                                                 known_story_keys=["QAS-1"], **kw))


def test_auditor_pass():
    reqs = [fr(1, "Al presionar Guardar, el sistema registra la solicitud.")]
    _, design = design_for(reqs)
    report = audit(reqs, design)
    assert report.audit_status == AuditStatus.PASS
    assert report.publication_ready
    assert report.coverage.requirements.covered == report.coverage.requirements.total == 1


def test_auditor_fail_on_uncovered_requirement_and_duplicates():
    reqs = [fr(1, "Al presionar Guardar, el sistema registra la solicitud."),
            fr(2, "Al presionar Borrar, el sistema elimina el borrador.")]
    _, design = design_for(reqs[:1])
    duplicated = GherkinDesign(**{**design.model_dump(), "cases": [*design.cases,
                                                                 design.cases[0].model_copy(update={"id": "TC-099"})]})
    report = audit(reqs, duplicated)
    assert report.audit_status == AuditStatus.FAIL and not report.publication_ready
    categories = {f.category for f in report.all_findings()}
    assert {"uncovered-acceptance-criterion", "duplicate-case"} <= categories
    assert report.coverage.requirements.covered == 1 and report.coverage.requirements.total == 2


def test_auditor_does_not_modify_design():
    reqs = [fr(1, "Al presionar Guardar, el sistema registra la solicitud.")]
    _, design = design_for(reqs)
    before = fingerprint(design)
    audit(reqs, design)
    assert fingerprint(design) == before


def test_orchestrator_detects_auditor_tampering(workspace):
    class TamperingAuditor(IndependentAuditor):
        def audit(self, inp):  # noqa: D401 - prueba
            report = super().audit(inp)
            inp.design.cases[0].title = "modificado por el auditor"
            return report

    harness = make_harness(workspace)
    harness.orchestrator.auditor = TamperingAuditor()
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-tamper")
    # El auditor recibe una copia: modificarla no altera el diseño, así que el flujo sigue intacto.
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    assert all(c.title != "modificado por el auditor" for c in bundle.design.cases)


def test_auditor_failure_is_repaired_by_designer(workspace):
    class ForgetfulDesigner(GherkinDesigner):
        """Olvida diseñar el primer requisito; la reparación lo agrega."""

        def design(self, inp):
            skip = inp.requirements[0].id
            trimmed = inp.model_copy(update={"requirements": [r for r in inp.requirements if r.id != skip]})
            return super().design(trimmed)

    harness = make_harness(workspace, designer=ForgetfulDesigner())
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-repair")
    assert bundle.audits[0].audit_status == AuditStatus.FAIL
    assert bundle.state.repair_cycles >= 1
    assert bundle.audit.audit_status != AuditStatus.FAIL
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL


def test_repair_limit_two_cycles_then_blocked(workspace):
    class StubbornDesigner(GherkinDesigner):
        def design(self, inp):
            skip = inp.requirements[0].id
            trimmed = inp.model_copy(update={"requirements": [r for r in inp.requirements if r.id != skip]})
            return super().design(trimmed)

        def repair(self, inp, design, request):
            return design  # nunca corrige

    harness = make_harness(workspace, designer=StubbornDesigner())
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-limit")
    assert bundle.state.repair_cycles == MAX_REPAIR_CYCLES == 2
    assert len(bundle.audits) == 3
    assert all(a.audit_status == AuditStatus.FAIL for a in bundle.audits)
    assert bundle.state.phase == WorkflowPhase.BLOCKED
    assert not bundle.state.publication_ready
    assert (bundle.output_dir / "qa-validation-report.md").exists()  # se entrega el resultado bloqueado


def test_schema_violation_from_agent_fails_run(workspace):
    class BrokenValidator:
        def run(self, inp):
            return {"requirements": [{"id": "FR-1", "statement": "x"}]}

    harness = make_harness(workspace, functional=BrokenValidator())
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-schema")
    assert bundle.state.phase == WorkflowPhase.FAILED
    assert "inválido" in bundle.state.message


def test_validate_exchange_raises_schema_violation():
    from qa_regresion.domain.contracts import FunctionalAnalysis
    from qa_regresion.domain.validation import validate_exchange

    with pytest.raises(SchemaViolation):
        validate_exchange(FunctionalAnalysis, {"requirements": "no-es-lista"}, producer="test")

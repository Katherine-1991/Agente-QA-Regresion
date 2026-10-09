from __future__ import annotations

import pytest
from pydantic import ValidationError

from qa_regresion.domain.enums import (
    AuditStatus,
    Automation,
    CaseKind,
    CaseStatus,
    Priority,
    ReadStatus,
    Severity,
    SourceType,
    Suite,
    Tag,
)
from qa_regresion.domain.gherkin import render_feature, validate_feature
from qa_regresion.domain.models import (
    AgentName,
    AuditFinding,
    AuditReport,
    Coverage,
    CoverageMetric,
    Evidence,
    GherkinTestCase,
    TestDataSet,
)

EV = Evidence(source_type=SourceType.JIRA_STORY, source_id="QAS-1", source_location="descripción",
              paraphrase="regla", read_status=ReadStatus.COMPLETE)


def make_case(**overrides):
    data = dict(id="TC-001", title="Caso", feature="F", kind=CaseKind.POSITIVE,
                tags=[Tag.POSITIVE, Tag.FULL_REGRESSION, Tag.MANUAL_ONLY], given=["el usuario está autenticado"],
                when=["el usuario confirma"], then=["el sistema registra la solicitud"],
                preconditions=["usuario autenticado"], dataset_id="TD-001", priority=Priority.MEDIA,
                suite=Suite.FULL_REGRESSION, automation=Automation.MANUAL_ONLY, status=CaseStatus.CONFIRMED,
                requirement_ids=["FR-001"], evidence=[EV], source_ids=["QAS-1"])
    data.update(overrides)
    return GherkinTestCase(**data)


# ------------------------------------------------------------------ trazabilidad obligatoria


def test_case_requires_evidence_and_requirements():
    with pytest.raises(ValidationError):
        make_case(evidence=[])
    with pytest.raises(ValidationError):
        make_case(requirement_ids=[])
    with pytest.raises(ValidationError):
        make_case(requirement_ids=["REQ-1"])


def test_evidence_requires_all_fields():
    with pytest.raises(ValidationError):
        Evidence(source_type=SourceType.DOCUMENT, source_id="", source_location="x", paraphrase="y",
                 read_status=ReadStatus.COMPLETE)
    with pytest.raises(ValidationError):
        Evidence(source_type=SourceType.DOCUMENT, source_id="d", source_location="x", paraphrase="y",
                 read_status="leido")


def test_pending_case_needs_tag_and_reason():
    with pytest.raises(ValidationError):
        make_case(status=CaseStatus.PENDING_BUSINESS_DECISION, pending_reason="GAP-001")
    with pytest.raises(ValidationError):
        make_case(status=CaseStatus.PENDING_BUSINESS_DECISION,
                  tags=[Tag.POSITIVE, Tag.FULL_REGRESSION, Tag.MANUAL_ONLY, Tag.PENDING_BUSINESS_DECISION])
    ok = make_case(status=CaseStatus.PENDING_BUSINESS_DECISION, pending_reason="GAP-001",
                   tags=[Tag.POSITIVE, Tag.FULL_REGRESSION, Tag.MANUAL_ONLY, Tag.PENDING_BUSINESS_DECISION])
    assert ok.status == CaseStatus.PENDING_BUSINESS_DECISION


def test_automation_tag_must_match_field():
    with pytest.raises(ValidationError):
        make_case(automation=Automation.CANDIDATE)


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        make_case(publish_now=True)


def test_dataset_must_be_reproducible():
    with pytest.raises(ValidationError):
        TestDataSet(id="TD-001", description="datos", provisioning="explicit", values={})
    with pytest.raises(ValidationError):
        TestDataSet(id="TD-001", description="datos", provisioning="to-provision")


def test_audit_report_status_consistency():
    cov = Coverage(requirements=CoverageMetric(covered=1, total=1),
                   acceptance_criteria=CoverageMetric(covered=0, total=0),
                   traceable_test_cases=CoverageMetric(covered=1, total=1))
    finding = AuditFinding(id="AF-001", severity=Severity.MAJOR, category="x", message="m",
                           responsible_agent=AgentName.GHERKIN_DESIGNER)
    with pytest.raises(ValidationError):
        AuditReport(audit_status=AuditStatus.PASS, publication_ready=True, major_findings=[finding],
                    coverage=cov, cycle=0)
    with pytest.raises(ValidationError):
        AuditReport(audit_status=AuditStatus.FAIL, publication_ready=True, major_findings=[finding],
                    coverage=cov, cycle=0)
    with pytest.raises(ValidationError):
        CoverageMetric(covered=2, total=1)


# ------------------------------------------------------------------ Gherkin


def test_rendered_case_is_valid_gherkin():
    text = render_feature("F", "desc", [make_case()])
    assert text.startswith("# language: es")
    assert validate_feature(text) == []


def test_outline_with_examples_is_valid():
    case = make_case(kind=CaseKind.BOUNDARY, tags=[Tag.BOUNDARY, Tag.FULL_REGRESSION, Tag.MANUAL_ONLY],
                     when=["el usuario ingresa <valor>"], then=["el sistema <resultado> el valor"],
                     examples_header=["valor", "resultado"], examples_rows=[["10", "acepta"], ["11", "rechaza"]])
    assert validate_feature(render_feature("F", "", [case])) == []


@pytest.mark.parametrize("body,expected", [
    ("Dado x\n    Cuando a\n    Y b\n    Entonces c", "exactamente una acción principal"),
    ("Dado x\n    Cuando a", "falta 'Entonces'"),
    ("Cuando a\n    Entonces c", "falta 'Dado'"),
    ("Dado x\n    Entonces c\n    Cuando a", "orden de pasos inválido"),
    ("Dado x\n    Cuando hace click en //div[@id='ok']\n    Entonces c", "selectores"),
    ("Dado x\n    Cuando ingresa <valor>\n    Entonces c", "no es esquema"),
])
def test_invalid_gherkin_is_detected(body, expected):
    text = f"# language: es\nCaracterística: F\n\n  @TC-001 @positive\n  Escenario: TC-001 - x\n    {body}\n"
    errors = validate_feature(text)
    assert any(expected in e for e in errors), errors


def test_unknown_tag_and_missing_tc_tag_are_detected():
    text = "# language: es\nCaracterística: F\n\n  @inventada\n  Escenario: x\n    Dado a\n    Cuando b\n    Entonces c\n"
    errors = validate_feature(text)
    assert any("etiqueta no permitida" in e for e in errors)
    assert any("@TC-###" in e for e in errors)


def test_outline_without_examples_is_invalid():
    text = ("# language: es\nCaracterística: F\n\n  @TC-001\n  Esquema del escenario: x\n    Dado a\n"
            "    Cuando ingresa <v>\n    Entonces c\n")
    assert any("Ejemplos" in e or "encabezado" in e for e in validate_feature(text))


def test_acceptance_criteria_with_bold_keywords_are_all_extracted():
    """Formato real de historias: '## CA01', '### CA02' y Dado/cuando/entonces en negrita."""
    from qa_regresion.agents.functional_validator import FunctionalValidator
    from qa_regresion.config import load_profile

    text = ("# Criterios de aceptación\n\n## CA01 - Habilitación\n\n**Dado** que el usuario visualiza una póliza,\n"
            "**cuando** el Premio sea igual a cero,\n**entonces** el sistema debe mostrar Transferir **deshabilitado.**\n\n"
            "### CA02 - Habilitación por componentes\n\n**Dado** que el usuario ingresó un componente,\n"
            "**cuando** el Premio resulte distinto de cero,\n**entonces** el sistema debe habilitar Transferir.\n\n"
            "## CA03 - Confirmación\n\n**Dado** que el usuario selecciona Transferir,\n**cuando** el sistema recibe "
            "la solicitud,\n**entonces** debe mostrar un modal de confirmación.\n\n# Criterios de aceptación — Acción\n\n"
            "- A1. El campo Acción debe visualizarse vacío al ingresar a la pantalla.\n"
            "- A2. El placeholder debe mostrar el texto \"Seleccionar\".\n")
    fv = FunctionalValidator(load_profile(None))
    labels = [label for s in fv._sections(text) if s.kind == "ac" for label, _t, _b in fv._acceptance_items(s.lines)]
    assert labels == ["CA01", "CA02", "CA03", "A1", "A2"]

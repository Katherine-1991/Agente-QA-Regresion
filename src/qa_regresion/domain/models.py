"""Modelos tipados que intercambian los agentes.

Toda afirmación funcional lleva al menos una `Evidence`. Los modelos rechazan campos
desconocidos (`extra="forbid"`) para que un agente no pueda colar datos fuera de contrato.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qa_regresion.domain.enums import (
    AgentName,
    AuditStatus,
    Automation,
    CandidateDecision,
    CaseKind,
    CaseStatus,
    DataProvisioning,
    FindingKind,
    GapKind,
    Priority,
    PublicationAction,
    PublicationClassification,
    ReadStatus,
    RequirementCategory,
    RequirementStatus,
    Severity,
    SourceType,
    Suite,
    Tag,
    TraceClassification,
    WorkflowPhase,
)

FR_ID = r"^FR-\d{3,}$"
TR_ID = r"^TR-\d{3,}$"
TC_ID = r"^TC-\d{3,}$"
TD_ID = r"^TD-\d{3,}$"
GAP_ID = r"^GAP-\d{3,}$"
IMP_ID = r"^IMP-\d{3,}$"
AF_ID = r"^AF-\d{3,}$"
ISSUE_KEY = r"^[A-Z][A-Z0-9_]+-\d+$"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False, validate_assignment=True)


# --------------------------------------------------------------------------- evidencia y fuentes


class Evidence(Strict):
    source_type: SourceType
    source_id: str = Field(min_length=1)
    source_location: str = Field(min_length=1)
    paraphrase: str = Field(min_length=1)
    read_status: ReadStatus


class SourceRecord(Strict):
    source_id: str = Field(min_length=1)
    source_type: SourceType
    title: str = ""
    location: str = Field(min_length=1)
    read_status: ReadStatus
    issue_type: str | None = None
    status: str | None = None
    relation: str | None = None
    parent_id: str | None = None
    format: str | None = None
    size_bytes: int | None = None
    content_hash: str | None = None
    text: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    untrusted_instructions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def manifest_entry(self) -> dict[str, Any]:
        """Representación sin el texto completo, para source-manifest.json."""
        data = self.model_dump(mode="json", exclude={"text", "fields"})
        data["text_length"] = len(self.text)
        data["field_names"] = sorted(self.fields)
        return data


class EpicCandidate(Strict):
    key: str = Field(pattern=ISSUE_KEY)
    summary: str
    status: str | None = None
    project_key: str | None = None


class HistoricalDefect(Strict):
    key: str = Field(pattern=ISSUE_KEY)
    summary: str
    status: str | None = None
    resolution: str | None = None
    related_story_keys: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


# --------------------------------------------------------------------------- análisis


def case_source_of(req: "NormalizedRequirement") -> str:
    """Fuente de casos a la que pertenece un requisito (ver design.case_sources)."""
    if req.category == RequirementCategory.DEFECT_REGRESSION:
        return "defects"
    if req.acceptance_criterion_ref:
        return "acceptance-criteria"
    if req.status == RequirementStatus.PENDING_BUSINESS_DECISION:
        return "documentation"
    if any(e.source_type == SourceType.JIRA_COMMENT for e in req.evidence):
        return "decisions"
    return "business-rules"


class NormalizedRequirement(Strict):
    id: str = Field(pattern=FR_ID)
    statement: str = Field(min_length=3)
    category: RequirementCategory
    status: RequirementStatus = RequirementStatus.CONFIRMED
    feature: str = Field(min_length=1)
    actors: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    story_keys: list[str] = Field(default_factory=list)
    acceptance_criterion_ref: str | None = None
    title: str | None = None
    precondition: str | None = None
    condition: str | None = None
    outcome: str | None = None
    values: list[str] = Field(default_factory=list)
    defect_keys: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    evidence: list[Evidence] = Field(min_length=1)


class TechnicalFinding(Strict):
    id: str = Field(pattern=TR_ID)
    statement: str = Field(min_length=3)
    kind: FindingKind
    classification: TraceClassification
    feature: str | None = None
    related_requirements: list[str] = Field(default_factory=list)
    story_keys: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    conflicting_values: list[str] = Field(default_factory=list)
    generates_case: bool = False
    note: str | None = None
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistency(self) -> "TechnicalFinding":
        if self.classification in (TraceClassification.ALINEADO, TraceClassification.CONTRADICTORIO,
                                   TraceClassification.MODIFICADO) and not self.related_requirements:
            raise ValueError(f"{self.id}: la clasificación {self.classification.value} exige requisitos relacionados")
        return self


class BusinessGap(Strict):
    id: str = Field(pattern=GAP_ID)
    kind: GapKind
    description: str = Field(min_length=3)
    severity: Severity = Severity.MINOR
    decision_needed: str | None = None
    related_requirements: list[str] = Field(default_factory=list)
    related_findings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_required(self) -> "BusinessGap":
        if self.kind in (GapKind.CONTRADICTION, GapKind.DOC_ONLY_RULE, GapKind.MODIFIED_RULE) and not self.evidence:
            raise ValueError(f"{self.id}: un gap funcional requiere evidencia")
        return self


class TestDataSet(Strict):
    __test__ = False  # evita que pytest lo tome como clase de prueba

    id: str = Field(pattern=TD_ID)
    description: str = Field(min_length=3)
    provisioning: DataProvisioning
    values: dict[str, str] = Field(default_factory=dict)
    fixture_ref: str | None = None
    notes: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reproducible(self) -> "TestDataSet":
        if self.provisioning == DataProvisioning.EXPLICIT and not self.values:
            raise ValueError(f"{self.id}: datos explícitos sin valores")
        if self.provisioning == DataProvisioning.FIXTURE and not self.fixture_ref:
            raise ValueError(f"{self.id}: fixture sin referencia")
        if self.provisioning == DataProvisioning.TO_PROVISION and not self.notes:
            raise ValueError(f"{self.id}: datos a aprovisionar sin instrucciones")
        return self


class ImpactRecord(Strict):
    id: str = Field(pattern=IMP_ID)
    feature: str
    story_keys: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    test_case_ids: list[str] = Field(default_factory=list)
    defect_keys: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    risk: Priority = Priority.MEDIA
    rationale: str = ""


# --------------------------------------------------------------------------- diseño de casos


class TestCaseCandidate(Strict):
    __test__ = False

    candidate_id: str = Field(pattern=r"^CAND-\d{3,}$")
    title: str = Field(min_length=3)
    kind: CaseKind
    feature: str
    requirement_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    defect_keys: list[str] = Field(default_factory=list)
    story_keys: list[str] = Field(default_factory=list)
    given: list[str] = Field(min_length=1)
    when: list[str] = Field(min_length=1)
    then: list[str] = Field(min_length=1)
    examples_header: list[str] | None = None
    examples_rows: list[list[str]] | None = None
    dataset_id: str | None = None
    status_hint: CaseStatus = CaseStatus.CONFIRMED
    pending_reason: str | None = None
    decision: CandidateDecision | None = None
    decision_reason: str | None = None
    merged_into: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class GherkinTestCase(Strict):
    id: str = Field(pattern=TC_ID)
    title: str = Field(min_length=3)
    feature: str = Field(min_length=1)
    kind: CaseKind
    tags: list[Tag] = Field(min_length=1)
    given: list[str] = Field(min_length=1)
    when: list[str] = Field(min_length=1)
    then: list[str] = Field(min_length=1)
    examples_header: list[str] | None = None
    examples_rows: list[list[str]] | None = None
    preconditions: list[str] = Field(min_length=1)
    dataset_id: str = Field(pattern=TD_ID)
    priority: Priority
    suite: Suite
    automation: Automation
    status: CaseStatus
    requirement_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    defect_keys: list[str] = Field(default_factory=list)
    story_keys: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    pending_reason: str | None = None
    evidence: list[Evidence] = Field(min_length=1)
    candidate_ids: list[str] = Field(default_factory=list)

    @field_validator("requirement_ids")
    @classmethod
    def _fr_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.match(FR_ID, item):
                raise ValueError(f"ID de requisito inválido: {item}")
        return value

    @model_validator(mode="after")
    def _tags_consistent(self) -> "GherkinTestCase":
        tags = set(self.tags)
        if self.status == CaseStatus.PENDING_BUSINESS_DECISION and Tag.PENDING_BUSINESS_DECISION not in tags:
            raise ValueError(f"{self.id}: caso pendiente sin @pending-business-decision")
        if self.status == CaseStatus.PENDING_BUSINESS_DECISION and not self.pending_reason:
            raise ValueError(f"{self.id}: caso pendiente sin motivo")
        auto_tag = Tag.AUTOMATION_CANDIDATE if self.automation == Automation.CANDIDATE else Tag.MANUAL_ONLY
        if auto_tag not in tags:
            raise ValueError(f"{self.id}: la etiqueta de automatización no coincide con {self.automation.value}")
        if Tag.AUTOMATION_CANDIDATE in tags and Tag.MANUAL_ONLY in tags:
            raise ValueError(f"{self.id}: no puede ser @automation-candidate y @manual-only a la vez")
        if Tag.FULL_REGRESSION not in tags:
            raise ValueError(f"{self.id}: todo caso pertenece a @full-regression")
        if (self.examples_header is None) != (self.examples_rows is None):
            raise ValueError(f"{self.id}: ejemplos incompletos")
        return self


class GherkinFeature(Strict):
    name: str
    file_name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*\.feature$")
    description: str = ""
    case_ids: list[str] = Field(min_length=1)
    text: str


# --------------------------------------------------------------------------- auditoría


class AuditFinding(Strict):
    id: str = Field(pattern=AF_ID)
    severity: Severity
    category: str
    message: str
    target_ids: list[str] = Field(default_factory=list)
    responsible_agent: AgentName
    recommendation: str = ""


class CoverageMetric(Strict):
    covered: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> "CoverageMetric":
        if self.covered > self.total:
            raise ValueError("covered no puede superar total")
        return self


class Coverage(Strict):
    requirements: CoverageMetric
    acceptance_criteria: CoverageMetric
    traceable_test_cases: CoverageMetric


class AuditCheck(Strict):
    name: str
    passed: bool
    detail: str = ""


class AuditReport(Strict):
    audit_status: AuditStatus
    publication_ready: bool
    critical_findings: list[AuditFinding] = Field(default_factory=list)
    major_findings: list[AuditFinding] = Field(default_factory=list)
    minor_findings: list[AuditFinding] = Field(default_factory=list)
    coverage: Coverage
    cycle: int = Field(ge=0)
    checks: list[AuditCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_consistent(self) -> "AuditReport":
        blocking = bool(self.critical_findings or self.major_findings)
        if blocking and self.audit_status != AuditStatus.FAIL:
            raise ValueError("hallazgos críticos o mayores exigen FAIL")
        if self.audit_status == AuditStatus.FAIL and self.publication_ready:
            raise ValueError("un FAIL no puede estar listo para publicar")
        if not blocking and self.minor_findings and self.audit_status == AuditStatus.PASS:
            raise ValueError("hallazgos menores exigen PASS_WITH_WARNINGS")
        return self

    def all_findings(self) -> list[AuditFinding]:
        return [*self.critical_findings, *self.major_findings, *self.minor_findings]


# --------------------------------------------------------------------------- publicación


class PreviewLink(Strict):
    link_type: str
    target: str
    direction: str = "outward"


class PreviewTestPlan(Strict):
    summary: str
    issue_type: str
    project_key: str
    epic_key: str = Field(pattern=ISSUE_KEY)
    epic_association: str
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    description: str
    attachments: list[str] = Field(default_factory=list)
    extra_fields: dict[str, Any] = Field(default_factory=dict)
    run_id: str
    content_hash: str


class PreviewTest(Strict):
    tc_id: str = Field(pattern=TC_ID)
    summary: str
    issue_type: str
    project_key: str
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    priority: Priority
    suite: Suite
    automation: Automation
    status: CaseStatus
    pending: bool
    description: str
    test_plan_association: str
    extra_fields: dict[str, Any] = Field(default_factory=dict)
    story_links: list[PreviewLink] = Field(default_factory=list)
    requirement_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    content_hash: str

    @model_validator(mode="after")
    def _tc_in_summary(self) -> "PreviewTest":
        if self.tc_id not in self.summary:
            raise ValueError(f"el título del Test debe incluir {self.tc_id}")
        return self


class PublicationPreview(Strict):
    run_id: str
    inherited_fields: dict[str, str] = Field(default_factory=dict)
    epic_key: str = Field(pattern=ISSUE_KEY)
    epic_name: str
    project_key: str
    epic_components: list[str] = Field(default_factory=list)
    dry_run: bool
    publication_enabled: bool
    approval_command: str
    test_plan: PreviewTestPlan
    tests: list[PreviewTest] = Field(default_factory=list)
    not_published: list[dict[str, str]] = Field(default_factory=list)
    content_fingerprint: str
    destination_fingerprint: str

    @model_validator(mode="after")
    def _contract(self) -> "PublicationPreview":
        expected = f"Test Plan - Regresión | {self.epic_name}"
        if self.test_plan.summary != expected:
            raise ValueError(f"título del Test Plan inválido: se esperaba '{expected}'")
        if self.test_plan.epic_key != self.epic_key:
            raise ValueError("el Test Plan debe relacionarse con la épica de la ejecución")
        epic_components = sorted(self.epic_components)
        if sorted(self.test_plan.components) != epic_components:
            raise ValueError("los componentes del Test Plan deben ser los de la épica")
        for test in self.tests:
            if sorted(test.components) != epic_components:
                raise ValueError(f"los componentes de {test.tc_id} deben ser los de la épica")
        ids = [t.tc_id for t in self.tests]
        if len(ids) != len(set(ids)):
            raise ValueError("TC duplicados en la vista previa")
        return self


class PublicationItemPlan(Strict):
    ref: str
    kind: str  # test-plan | test
    classification: PublicationClassification
    action: PublicationAction
    existing_key: str | None = None
    reason: str = ""


class PublicationPlan(Strict):
    run_id: str
    items: list[PublicationItemPlan] = Field(default_factory=list)


class ApprovalRecord(Strict):
    token_id: str
    run_id: str
    draft_fingerprint: str
    destination_fingerprint: str
    granted_at: str
    consumed: bool = False
    consumed_at: str | None = None


# --------------------------------------------------------------------------- estado


class PhaseTransition(Strict):
    from_phase: WorkflowPhase | None
    to_phase: WorkflowPhase
    at: str = Field(default_factory=utcnow)
    note: str = ""


class WorkflowState(Strict):
    run_id: str
    command: str
    epic_query: str
    phase: WorkflowPhase = WorkflowPhase.RECEIVED
    dry_run: bool = True
    epic_key: str | None = None
    epic_name: str | None = None
    epic_components: list[str] = Field(default_factory=list)
    epic_fields: dict[str, Any] = Field(default_factory=dict)
    project_key: str | None = None
    epic_candidates: list[EpicCandidate] = Field(default_factory=list)
    epic_folder: str | None = None
    output_dir: str | None = None
    history: list[PhaseTransition] = Field(default_factory=list)
    repair_cycles: int = Field(default=0, ge=0)
    audit_status: AuditStatus | None = None
    publication_ready: bool = False
    limitations: list[str] = Field(default_factory=list)
    ignored_instructions: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    draft_fingerprint: str | None = None
    destination_fingerprint: str | None = None
    draft_presented_at: str | None = None
    approval: ApprovalRecord | None = None
    remote_write_calls: int = 0
    remote_read_calls: int = 0
    created_at: str = Field(default_factory=utcnow)
    message: str = ""

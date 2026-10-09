"""Enumeraciones del dominio. Los valores son los literales que aparecen en los artefactos."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class SourceType(StrEnum):
    JIRA_EPIC = "jira-epic"
    JIRA_STORY = "jira-story"
    JIRA_IMPROVEMENT = "jira-improvement"
    JIRA_OUT_OF_SCOPE = "jira-out-of-scope"
    JIRA_INCIDENT = "jira-incident"
    JIRA_OTHER = "jira-other"
    JIRA_COMMENT = "jira-comment"
    CONFLUENCE_PAGE = "confluence-page"
    DOCUMENT = "document"
    FIGMA = "figma"


class ReadStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INACCESSIBLE = "inaccessible"


class RequirementCategory(StrEnum):
    BUSINESS_RULE = "business-rule"
    ACCEPTANCE_CRITERION = "acceptance-criterion"
    VALIDATION = "validation"
    ROLE_PERMISSION = "role-permission"
    STATE_TRANSITION = "state-transition"
    BOUNDARY = "boundary"
    PERSISTENCE = "persistence"
    INTEGRATION = "integration"
    DEFECT_REGRESSION = "defect-regression"
    OUT_OF_SCOPE = "out-of-scope"


class RequirementStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING_BUSINESS_DECISION = "pending-business-decision"
    EXCLUDED = "excluded"


class TraceClassification(StrEnum):
    ALINEADO = "alineado"
    SOLO_DOCUMENTACION = "solo-documentacion"
    CONTRADICTORIO = "contradictorio"
    MODIFICADO = "modificado"
    SIN_EVIDENCIA_SUFICIENTE = "sin-evidencia-suficiente"


class FindingKind(StrEnum):
    FUNCTIONAL_RULE = "functional-rule"
    TECHNICAL_CONSTRAINT = "technical-constraint"
    STATE_MODEL = "state-model"
    INTERFACE_CONTRACT = "interface-contract"
    INVENTORY = "inventory"


class GapKind(StrEnum):
    CONTRADICTION = "contradiction"
    DOC_ONLY_RULE = "doc-only-rule"
    MODIFIED_RULE = "modified-rule"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"
    INACCESSIBLE_SOURCE = "inaccessible-source"
    MISSING_INFORMATION = "missing-information"
    IGNORED_INSTRUCTION = "ignored-instruction"
    LIMITATION = "limitation"


class CaseStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING_BUSINESS_DECISION = "pending-business-decision"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


class CandidateDecision(StrEnum):
    ACCEPTED = "accepted"
    REWRITTEN = "rewritten"
    MERGED = "merged"
    SPLIT = "split"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class Tag(StrEnum):
    POSITIVE = "@positive"
    NEGATIVE = "@negative"
    BOUNDARY = "@boundary"
    ROLE = "@role"
    INTEGRATION = "@integration"
    STATE_TRANSITION = "@state-transition"
    SMOKE = "@smoke"
    CRITICAL_PATH = "@critical-path"
    FULL_REGRESSION = "@full-regression"
    AUTOMATION_CANDIDATE = "@automation-candidate"
    MANUAL_ONLY = "@manual-only"
    DEFECT_REGRESSION = "@defect-regression"
    PENDING_BUSINESS_DECISION = "@pending-business-decision"


class Priority(StrEnum):
    ALTA = "Alta"
    MEDIA = "Media"
    BAJA = "Baja"


class Suite(StrEnum):
    SMOKE = "smoke"
    CRITICAL_PATH = "critical-path"
    FULL_REGRESSION = "full-regression"


class Automation(StrEnum):
    CANDIDATE = "automation-candidate"
    MANUAL_ONLY = "manual-only"


class CaseKind(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"
    ROLE = "role"
    STATE_TRANSITION = "state-transition"
    INTEGRATION = "integration"
    DEFECT_REGRESSION = "defect-regression"


class DataProvisioning(StrEnum):
    EXPLICIT = "explicit"
    FIXTURE = "fixture"
    TO_PROVISION = "to-provision"


class AuditStatus(StrEnum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class AgentName(StrEnum):
    ORCHESTRATOR = "orchestrator"
    FUNCTIONAL_VALIDATOR = "functional_validator"
    DOCUMENTATION_VALIDATOR = "documentation_validator"
    GHERKIN_DESIGNER = "gherkin_designer"
    INDEPENDENT_AUDITOR = "independent_auditor"


class WorkflowPhase(StrEnum):
    RECEIVED = "RECEIVED"
    RESOLVING_EPIC = "RESOLVING_EPIC"
    AWAITING_EPIC_SELECTION = "AWAITING_EPIC_SELECTION"
    COLLECTING_SOURCES = "COLLECTING_SOURCES"
    VALIDATING_FUNCTIONAL = "VALIDATING_FUNCTIONAL"
    VALIDATING_DOCUMENTATION = "VALIDATING_DOCUMENTATION"
    CONSOLIDATING = "CONSOLIDATING"
    DESIGNING_CASES = "DESIGNING_CASES"
    AUDITING = "AUDITING"
    REPAIRING = "REPAIRING"
    GENERATING_DRAFT = "GENERATING_DRAFT"
    AWAITING_PUBLICATION_APPROVAL = "AWAITING_PUBLICATION_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class PublicationClassification(StrEnum):
    NEW = "new"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"
    PENDING = "pending"
    POTENTIALLY_OBSOLETE = "potentially-obsolete"


class PublicationAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    REPORT_ONLY = "report-only"


class MessageSource(StrEnum):
    """Canal por el que llegó un mensaje. Solo USER_CHAT puede aprobar una publicación."""

    USER_CHAT = "user-chat"
    JIRA = "jira"
    CONFLUENCE = "confluence"
    FIGMA = "figma"
    DOCUMENT = "document"
    SYSTEM = "system"

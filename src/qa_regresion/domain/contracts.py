"""Contratos de entrada/salida de cada agente. El orquestador revalida cada intercambio."""

from __future__ import annotations

from pydantic import Field

from qa_regresion.domain.models import (
    AuditFinding,
    AuditReport,
    BusinessGap,
    GherkinFeature,
    GherkinTestCase,
    HistoricalDefect,
    ImpactRecord,
    NormalizedRequirement,
    SourceRecord,
    Strict,
    TechnicalFinding,
    TestCaseCandidate,
    TestDataSet,
)


class FunctionalInput(Strict):
    epic: SourceRecord
    issues: list[SourceRecord] = Field(default_factory=list)
    comments: list[SourceRecord] = Field(default_factory=list)
    confluence_pages: list[SourceRecord] = Field(default_factory=list)


class FunctionalAnalysis(Strict):
    requirements: list[NormalizedRequirement] = Field(default_factory=list)
    defects: list[HistoricalDefect] = Field(default_factory=list)
    gaps: list[BusinessGap] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    features: dict[str, list[str]] = Field(default_factory=dict)
    ignored_instructions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DocumentationInput(Strict):
    epic_key: str
    documents: list[SourceRecord] = Field(default_factory=list)
    requirements: list[NormalizedRequirement] = Field(default_factory=list)
    gap_offset: int = 0
    limitations: list[str] = Field(default_factory=list)


class DocumentationAnalysis(Strict):
    findings: list[TechnicalFinding] = Field(default_factory=list)
    gaps: list[BusinessGap] = Field(default_factory=list)
    ignored_instructions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DesignInput(Strict):
    epic_key: str
    epic_name: str
    requirements: list[NormalizedRequirement]
    findings: list[TechnicalFinding] = Field(default_factory=list)
    gaps: list[BusinessGap] = Field(default_factory=list)
    defects: list[HistoricalDefect] = Field(default_factory=list)
    stable_ids: dict[str, str] = Field(default_factory=dict)
    max_doc_only_cases: int = 25
    language: str = "es"
    case_sources: list[str] = Field(default_factory=lambda: ["acceptance-criteria", "business-rules", "decisions",
                                                              "defects", "documentation"])


class GherkinDesign(Strict):
    candidates: list[TestCaseCandidate] = Field(default_factory=list)
    cases: list[GherkinTestCase] = Field(default_factory=list)
    datasets: list[TestDataSet] = Field(default_factory=list)
    features: list[GherkinFeature] = Field(default_factory=list)
    stable_ids: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class RepairRequest(Strict):
    cycle: int
    findings: list[AuditFinding]


class AuditInput(Strict):
    epic_key: str
    requirements: list[NormalizedRequirement]
    findings: list[TechnicalFinding] = Field(default_factory=list)
    gaps: list[BusinessGap] = Field(default_factory=list)
    defects: list[HistoricalDefect] = Field(default_factory=list)
    design: GherkinDesign
    quarantined_source_ids: list[str] = Field(default_factory=list)
    known_story_keys: list[str] = Field(default_factory=list)
    cycle: int = 0
    case_sources: list[str] = Field(default_factory=lambda: ["acceptance-criteria", "business-rules", "decisions",
                                                              "defects", "documentation"])


class ConsolidatedAnalysis(Strict):
    impact: list[ImpactRecord] = Field(default_factory=list)
    gaps: list[BusinessGap] = Field(default_factory=list)


__all__ = [
    "AuditInput", "AuditReport", "ConsolidatedAnalysis", "DesignInput", "DocumentationAnalysis",
    "DocumentationInput", "FunctionalAnalysis", "FunctionalInput", "GherkinDesign", "RepairRequest",
]

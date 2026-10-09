"""Agente 3 — Validador documental.

Recorre los documentos de la carpeta de la épica (y Figma si está disponible), genera hallazgos
TR-### y los clasifica contra los requisitos FR: alineado, solo-documentacion, contradictorio,
modificado o sin-evidencia-suficiente. Las reglas solo documentales quedan pendientes de negocio.
"""

from __future__ import annotations

import re

from qa_regresion.agents.matching import best_match
from qa_regresion.domain.contracts import DocumentationAnalysis, DocumentationInput
from qa_regresion.domain.enums import (
    FindingKind,
    GapKind,
    ReadStatus,
    RequirementStatus,
    Severity,
    SourceType,
    TraceClassification,
)
from qa_regresion.domain.models import BusinessGap, Evidence, SourceRecord, TechnicalFinding
from qa_regresion.domain.text_analysis import (
    clean_markup,
    extract_values,
    is_strong_rule,
    normalize,
    shorten,
    split_sentences,
)
from qa_regresion.domain.validation import detect_untrusted_instructions

_KEY = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
_KIND_BY_META = {
    "state-model": FindingKind.STATE_MODEL,
    "interface-contract": FindingKind.INTERFACE_CONTRACT,
    "technical-constraint": FindingKind.TECHNICAL_CONSTRAINT,
}


class DocumentationValidator:
    name = "documentation_validator"

    @staticmethod
    def _same_transition(meta: dict, statement: str) -> bool:
        """Una transición documental solo se alinea si el requisito menciona ambos estados."""
        norm = normalize(statement)
        return all(normalize(meta.get(k) or "") in norm for k in ("from", "to") if meta.get(k))

    def run(self, inp: DocumentationInput) -> DocumentationAnalysis:
        findings: list[TechnicalFinding] = []
        gaps: list[BusinessGap] = []
        ignored: list[str] = []
        by_text: dict[str, TechnicalFinding] = {}
        reqs = [r for r in inp.requirements]
        active = [r for r in reqs if r.status != RequirementStatus.EXCLUDED]
        story_features = {k: r.feature for r in reqs for k in r.story_keys}

        def next_tr() -> str:
            return f"TR-{len(findings) + 1:03d}"

        def next_gap() -> str:
            return f"GAP-{inp.gap_offset + len(gaps) + 1:03d}"

        def evidence(doc: SourceRecord, location: str, text: str, status: ReadStatus | None = None) -> Evidence:
            return Evidence(source_type=doc.source_type, source_id=doc.source_id,
                            source_location=f"{doc.location} · {location}", paraphrase=shorten(text, 220),
                            read_status=status or doc.read_status)

        for doc in sorted(inp.documents, key=lambda d: d.source_id):
            units = doc.fields.get("units") or []
            if doc.read_status == ReadStatus.INACCESSIBLE or (not units and doc.read_status == ReadStatus.PARTIAL):
                reason = "; ".join(doc.limitations) or "sin contenido legible"
                tr = TechnicalFinding(
                    id=next_tr(), statement=f"Documento {doc.location} no analizable: {reason}",
                    kind=FindingKind.INVENTORY, classification=TraceClassification.SIN_EVIDENCIA_SUFICIENTE,
                    evidence=[evidence(doc, "archivo completo", reason)])
                findings.append(tr)
                if doc.read_status == ReadStatus.INACCESSIBLE:
                    gaps.append(BusinessGap(id=next_gap(), kind=GapKind.INACCESSIBLE_SOURCE, severity=Severity.MINOR,
                                            description=f"{doc.location}: {reason}", related_findings=[tr.id],
                                            evidence=tr.evidence))
                continue
            pieces: list[tuple[str, str, dict]] = []
            for unit in units:
                meta = unit.get("meta") or {}
                if meta.get("tabular") or meta.get("kind"):
                    pieces.append((clean_markup(unit["text"]), unit["location"], meta))
                else:
                    pieces += [(clean_markup(s), unit["location"], meta) for s in split_sentences(unit["text"])]
            for text, location, meta in pieces:
                injected = detect_untrusted_instructions(text)
                if injected:
                    for snippet in injected:
                        note = f"{doc.source_id} ({location}): {snippet}"
                        ignored.append(note)
                        gaps.append(BusinessGap(
                            id=next_gap(), kind=GapKind.IGNORED_INSTRUCTION, severity=Severity.MINOR,
                            description=f"Instrucción embebida ignorada en {doc.location} ({location}).",
                            evidence=[evidence(doc, location, snippet)]))
                    continue
                kind = _KIND_BY_META.get(meta.get("kind", ""), FindingKind.FUNCTIONAL_RULE)
                if kind == FindingKind.FUNCTIONAL_RULE and not (meta.get("tabular") or is_strong_rule(text)):
                    continue
                key = normalize(text)
                if key in by_text:
                    existing = by_text[key]
                    existing.evidence = [*existing.evidence, evidence(doc, location, text)]
                    continue
                story_keys = sorted(set(_KEY.findall(meta.get("story") or "")))
                hint = story_keys[0] if story_keys else None
                match = best_match(text, reqs if kind != FindingKind.TECHNICAL_CONSTRAINT else active, hint)
                if kind == FindingKind.STATE_MODEL and match.requirement is not None and \
                        not self._same_transition(meta, match.requirement.statement):
                    match.requirement, match.score, match.conflicts = None, 0.0, []
                context = match.requirement if match.requirement is not None and match.score >= 0.25 else None
                feature = (story_features.get(hint) if hint else None) or meta.get("feature") or \
                    (context.feature if context else None) or f"Documentación: {doc.title}"
                ev = evidence(doc, location, text)
                classification = TraceClassification.SOLO_DOCUMENTACION
                related: list[str] = []
                generates_case = False
                note = None
                conflicts: list[str] = []
                if match.found and match.requirement is not None:
                    req = match.requirement
                    related = [req.id]
                    if match.conflicts:
                        classification = TraceClassification.CONTRADICTORIO
                        conflicts = match.conflicts
                    elif req.supersedes or req.status == RequirementStatus.EXCLUDED or any(
                            e.source_type == SourceType.JIRA_COMMENT for e in req.evidence):
                        classification = TraceClassification.MODIFICADO
                        note = "La regla documental fue modificada por una decisión registrada en Jira."
                    else:
                        classification = TraceClassification.ALINEADO
                elif doc.read_status != ReadStatus.COMPLETE:
                    classification = TraceClassification.SIN_EVIDENCIA_SUFICIENTE
                    note = "Fuente leída parcialmente; no alcanza para afirmar la regla."
                else:
                    generates_case = kind in (FindingKind.FUNCTIONAL_RULE, FindingKind.STATE_MODEL)
                tr = TechnicalFinding(
                    id=next_tr(), statement=shorten(text, 400), kind=kind, classification=classification,
                    feature=feature, related_requirements=related, story_keys=story_keys,
                    values=extract_values(text), conflicting_values=conflicts, generates_case=generates_case,
                    note=note, evidence=[ev])
                findings.append(tr)
                by_text[key] = tr
                if classification == TraceClassification.CONTRADICTORIO:
                    gaps.append(BusinessGap(
                        id=next_gap(), kind=GapKind.CONTRADICTION, severity=Severity.MAJOR,
                        description=f"{tr.id} ({doc.location}, {location}) contradice {related[0]}: "
                                    f"{'; '.join(conflicts)}.",
                        decision_needed="Definir con negocio qué valor rige; los casos quedan pendientes.",
                        related_requirements=related, related_findings=[tr.id],
                        evidence=[ev, *match.requirement.evidence[:1]] if match.requirement else [ev]))
                elif classification == TraceClassification.MODIFICADO:
                    gaps.append(BusinessGap(
                        id=next_gap(), kind=GapKind.MODIFIED_RULE, severity=Severity.MINOR,
                        description=f"{tr.id} describe una regla que fue modificada ({related[0]}).",
                        related_requirements=related, related_findings=[tr.id], evidence=[ev]))
                elif classification == TraceClassification.SOLO_DOCUMENTACION and generates_case:
                    gaps.append(BusinessGap(
                        id=next_gap(), kind=GapKind.DOC_ONLY_RULE, severity=Severity.MINOR,
                        description=f"{tr.id} solo figura en {doc.location} ({location}); se diseña como "
                                    f"@pending-business-decision.",
                        decision_needed="Confirmar con negocio si la regla aplica.", related_findings=[tr.id],
                        evidence=[ev]))
        return DocumentationAnalysis(findings=findings, gaps=gaps, ignored_instructions=ignored,
                                     limitations=list(inp.limitations))

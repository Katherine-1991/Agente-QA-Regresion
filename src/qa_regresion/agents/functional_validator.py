"""Agente 2 — Validador funcional.

Extrae requisitos atómicos FR-### de la épica, historias, mejoras, fuera de alcance, incidencias,
comentarios con decisiones y páginas de Confluence. Nunca completa vacíos: si una historia no
tiene reglas, registra un BusinessGap en lugar de inventar requisitos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from qa_regresion.agents.matching import ALIGN_THRESHOLD, best_match
from qa_regresion.config import ProjectProfile
from qa_regresion.domain.contracts import FunctionalAnalysis, FunctionalInput
from qa_regresion.domain.enums import (
    GapKind,
    ReadStatus,
    RequirementCategory,
    RequirementStatus,
    Severity,
    SourceType,
)
from qa_regresion.domain.models import (
    BusinessGap,
    Evidence,
    HistoricalDefect,
    NormalizedRequirement,
    SourceRecord,
)
from qa_regresion.domain.text_analysis import (
    classify_category,
    clean_markup,
    detect_actors,
    detect_roles,
    extract_values,
    is_rule_like,
    is_strong_rule,
    normalize,
    shorten,
    similarity,
    split_gwt,
    split_sentences,
)
from qa_regresion.domain.validation import strip_untrusted_instructions

# "CA01 - Título", "AC-2: texto" o etiquetas cortas tipo "G1." / "A6)" / "C14:" al inicio de línea.
_AC_LABEL = re.compile(r"^(?:#+\s*)?(?:[-*•·]\s+)?[*_\s]*((?i:CA|AC|CR)[\s\-_]*\d+[a-z]?|[A-Z]\d{1,2}(?=[*_\s]*[.:)\-–—]))"
                       r"[*_\s]*[:\-–—.)]?[*_\s]*(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|\s*:?-{2,}")
_PRECISION = re.compile(r"\((\d+)\s*,\s*(\d+)\)")
_USER_STORY_LINE = re.compile(r"^(como|quiero|para|as an?|i want|so that)\b", re.IGNORECASE)
_GWT_LINE = re.compile(r"^(dado|dada|dados|dadas|cuando|entonces|y|pero|given|when|then|and)\b", re.IGNORECASE)
_SKIP_HEADINGS = ("referencias", "anexos", "links", "enlaces", "adjuntos")
_DONE_STATUS = ("cerrad", "resuelt", "done", "closed", "resolved", "finaliz", "implementad")
DEDUP_THRESHOLD = 0.8
SUPERSEDE_THRESHOLD = 0.55


@dataclass
class _Section:
    kind: str  # ac | oos | rules | body | skip
    heading: str
    lines: list[str] = field(default_factory=list)


def _heading_text(line: str) -> str | None:
    s = line.strip()
    m = re.match(r"^#{1,6}\s+(.+)$", s)
    if m:
        return m.group(1).strip().strip("*: ")
    m = re.match(r"^\*\*([^*]+)\*\*:?$", s)  # toda la línea es una sola negrita ("**entonces** … **x**" no lo es)
    if m:
        return m.group(1).strip(": ")
    if s.endswith(":") and 0 < len(s.split()) <= 6 and not s.startswith(("-", "*", "|", "•")):
        return s[:-1].strip()
    return None


class FunctionalValidator:
    name = "functional_validator"

    def __init__(self, profile: ProjectProfile):
        self.profile = profile
        self.cfg = profile.atlassian
        self._requirements: list[NormalizedRequirement] = []
        self._gaps: list[BusinessGap] = []
        self._ignored: list[str] = []

    # ------------------------------------------------------------------ utilidades

    def _next_fr(self) -> str:
        return f"FR-{len(self._requirements) + 1:03d}"

    def _next_gap(self) -> str:
        return f"GAP-{len(self._gaps) + 1:03d}"

    def _section_kind(self, heading: str) -> str:
        norm = normalize(heading)
        if any(norm.startswith(normalize(h).rstrip(":")) for h in self.cfg.acceptance_criteria_headings):
            return "ac"
        if any(norm.startswith(normalize(h)) for h in self.cfg.out_of_scope_headings):
            return "oos"
        if any(norm.startswith(normalize(h)) for h in self.cfg.business_rule_headings):
            return "rules"
        if norm.startswith(_SKIP_HEADINGS):
            return "skip"
        return "body"

    def _sections(self, text: str) -> list[_Section]:
        sections = [_Section("body", "")]
        in_code = False
        attached = False
        for raw in (text or "").splitlines():
            line = raw.rstrip()
            if line.strip().startswith("```"):
                in_code = not in_code
                attached = False
                continue
            if in_code:
                current = sections[-1]
                if current.kind == "ac" and line.strip():
                    current.lines.append(line.strip())  # p. ej. la URL esperada dentro de un CA
                elif line.strip() and not attached and current.lines and current.lines[-1].endswith(":"):
                    current.lines[-1] = f"{current.lines[-1]} {line.strip()}"  # valor esperado en un bloque
                    attached = True
                continue
            if line.strip() and sections[-1].lines and sections[-1].lines[-1].endswith(":") and \
                    _heading_text(line) is None and not line.strip().startswith(("|", "-", "*", "•")) and \
                    not _AC_LABEL.match(clean_markup(line.strip())):
                sections[-1].lines[-1] = f"{sections[-1].lines[-1]} {clean_markup(line.strip())}"
                continue
            heading = _heading_text(line)
            if heading is not None and not _AC_LABEL.match(clean_markup(heading)):
                title = clean_markup(heading)
                sections.append(_Section(self._section_kind(title), title))
                continue
            stripped = line.strip()
            if stripped:
                sections[-1].lines.append(stripped if stripped.startswith("|") else clean_markup(stripped))
        return sections

    @staticmethod
    def _row_statement(header: list[str] | None, cells: list[str]) -> str | None:
        cells = [c for c in cells]
        if len([c for c in cells if c]) < 2:
            return None
        if header and len(header) == len(cells):
            parts = [f"{h}: {c}" for h, c in zip(header[1:], cells[1:])
                     if c and c not in ("-", "—", "N/A", "n/a")]
            text = f"{cells[0]} — " + "; ".join(parts)
        else:
            text = " — ".join(c for c in cells if c)
        return _PRECISION.sub(lambda m: f"hasta {m.group(1)} dígitos ({m.group(2)} decimales)", text)

    def _section_sentences(self, lines: list[str]) -> list[tuple[str, bool]]:
        """Oraciones de una sección, marcando las filas de tabla (que se interpretan con su encabezado)."""
        out: list[tuple[str, bool]] = []
        header: list[str] | None = None
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("|"):
                if _TABLE_SEPARATOR.match(line):
                    i += 1
                    continue
                cells = [clean_markup(c) for c in line.strip().strip("|").split("|")]
                if i + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[i + 1]):
                    header, i = cells, i + 2
                    continue
                sentence = self._row_statement(header, cells)
                if sentence:
                    out.append((sentence, True))
            else:
                header = None
                out.extend((s, False) for s in split_sentences(line))
            i += 1
        return out

    @staticmethod
    def _acceptance_items(lines: list[str]) -> list[tuple[str | None, str | None, str]]:
        """Devuelve (etiqueta, título, cuerpo). 'CA01 - Título' + líneas Dado/cuando/entonces = título y cuerpo;
        'CA01 - texto' sin líneas siguientes = el texto es el criterio."""
        items: list[list] = []
        for line in lines:
            m = _AC_LABEL.match(line)
            if m:
                label = re.sub(r"[\s\-_]+", "", m.group(1)).upper()
                items.append([label, m.group(2).strip() or None, []])
                continue
            clean = re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s+", "", line).strip()
            if not clean:
                continue
            if items and (_GWT_LINE.match(clean) or not re.match(r"^\s*(?:[-*•·]|\d+[.)])\s+", line)):
                items[-1][2].append(clean)
            else:
                items.append([None, None, [clean]])
        out: list[tuple[str | None, str | None, str]] = []
        for label, head, body_lines in items:
            body = " ".join(body_lines).strip()
            if body:
                out.append((label, head, body))
            elif head:
                out.append((label, None, head))
        return out

    def _evidence(self, record: SourceRecord, location: str, statement: str) -> Evidence:
        return Evidence(source_type=record.source_type, source_id=record.source_id,
                        source_location=location, paraphrase=shorten(statement, 220),
                        read_status=record.read_status)

    def _add(self, *, statement: str, feature: str, story_keys: list[str], evidence: Evidence,
             ac_ref: str | None = None, status: RequirementStatus = RequirementStatus.CONFIRMED,
             category: RequirementCategory | None = None, defect_keys: list[str] | None = None,
             precondition: str | None = None, condition: str | None = None,
             outcome: str | None = None, body: str | None = None,
             title: str | None = None) -> NormalizedRequirement:
        statement = re.sub(r"\s+", " ", statement).strip()
        if condition is None and outcome is None:
            precondition, condition, outcome = split_gwt(body or statement)
        # Deduplicación dentro de la misma funcionalidad.
        for existing in self._requirements:
            # Dos criterios de aceptación distintos nunca se fusionan, aunque compartan vocabulario.
            if existing.acceptance_criterion_ref and ac_ref and existing.acceptance_criterion_ref != ac_ref:
                continue
            if existing.feature == feature and existing.status == status and \
                    similarity(existing.statement, statement) >= DEDUP_THRESHOLD:
                if evidence.source_location not in {e.source_location for e in existing.evidence} or \
                        evidence.source_id not in {e.source_id for e in existing.evidence}:
                    existing.evidence = [*existing.evidence, evidence]
                if ac_ref and not existing.acceptance_criterion_ref:
                    existing.acceptance_criterion_ref = ac_ref
                return existing
        if category is None:
            # Con Dado/Cuando/Entonces se clasifica por la acción y el resultado, no por el contexto del Dado.
            category = classify_category(f"{condition} {outcome}" if condition and outcome else statement)
            if ac_ref and category == RequirementCategory.BUSINESS_RULE:
                category = RequirementCategory.ACCEPTANCE_CRITERION
        req = NormalizedRequirement(
            id=self._next_fr(), statement=statement, category=category, status=status, feature=feature,
            actors=detect_actors(statement), roles=detect_roles(statement), story_keys=story_keys,
            acceptance_criterion_ref=ac_ref, title=title, precondition=precondition, condition=condition,
            outcome=outcome,
            values=extract_values(statement), defect_keys=defect_keys or [], evidence=[evidence])
        self._requirements.append(req)
        return req

    def _clean(self, record: SourceRecord, text: str) -> str:
        if record.untrusted_instructions:
            for snippet in record.untrusted_instructions:
                note = f"{record.source_id}: {snippet}"
                if note not in self._ignored:
                    self._ignored.append(note)
            return strip_untrusted_instructions(text, record.untrusted_instructions)
        return text

    # ------------------------------------------------------------------ extracción por historia

    def _extract_issue(self, record: SourceRecord, feature: str, story_keys: list[str],
                       status: RequirementStatus = RequirementStatus.CONFIRMED) -> int:
        before = len(self._requirements)
        text = self._clean(record, record.text)
        sections = self._sections(text)
        custom_ac = record.fields.get("acceptance_criteria")
        if custom_ac:
            sections.append(_Section("ac", "campo de criterios de aceptación",
                                     [ln.strip() for ln in self._clean(record, custom_ac).splitlines() if ln.strip()]))
        ac_counter = 0
        # Primero los criterios de aceptación: las reglas narrativas parecidas se suman como evidencia del CA.
        sections = [s for s in sections if s.kind == "ac"] + [s for s in sections if s.kind != "ac"]
        for section in sections:
            if section.kind == "skip":
                continue
            if section.kind == "ac":
                for label, title, body in self._acceptance_items(section.lines):
                    ac_counter += 1
                    ref = f"{record.source_id}/{label or f'CA{ac_counter:02d}'}"
                    location = f"{section.heading or 'criterios'} > {label or f'ítem {ac_counter}'}"
                    statement = f"{title}: {body}" if title else body
                    self._add(statement=statement, feature=feature, story_keys=story_keys, ac_ref=ref,
                              status=status, body=body, title=title,
                              evidence=self._evidence(record, location, statement))
                continue
            if section.kind == "oos":
                for sentence in split_sentences("\n".join(section.lines)):
                    self._add(statement=sentence, feature=feature, story_keys=story_keys,
                              status=RequirementStatus.EXCLUDED, category=RequirementCategory.OUT_OF_SCOPE,
                              evidence=self._evidence(record, f"sección {section.heading}", sentence))
                continue
            for sentence, is_row in self._section_sentences(section.lines):
                if not sentence or _USER_STORY_LINE.match(sentence) or len(sentence) > 600:
                    continue
                # En reglas y tablas basta un marcador de regla; en la narrativa se exige una regla explícita.
                lenient = section.kind == "rules" or is_row
                if not (is_rule_like(sentence) if lenient else is_strong_rule(sentence)):
                    continue
                location = f"descripción > {section.heading}" if section.heading else "descripción"
                self._add(statement=sentence, feature=feature, story_keys=story_keys, status=status,
                          evidence=self._evidence(record, location, sentence))
        return len(self._requirements) - before

    # ------------------------------------------------------------------ ejecución

    def run(self, inp: FunctionalInput) -> FunctionalAnalysis:
        self._requirements, self._gaps, self._ignored = [], [], []
        epic = inp.epic
        features: dict[str, list[str]] = {}
        stories = [r for r in inp.issues if r.source_type in (SourceType.JIRA_STORY, SourceType.JIRA_IMPROVEMENT)]
        story_keys = {r.source_id for r in stories}
        by_key = {r.source_id: r for r in inp.issues}

        epic_feature = f"{epic.title} (general)"
        self._extract_issue(epic, epic_feature, [])

        for story in sorted(stories, key=lambda r: r.source_id):
            feature = story.title
            features.setdefault(feature, []).append(story.source_id)
            found = self._extract_issue(story, feature, [story.source_id])
            if found == 0:
                self._gaps.append(BusinessGap(
                    id=self._next_gap(), kind=GapKind.MISSING_INFORMATION, severity=Severity.MAJOR,
                    description=f"{story.source_id} ({story.title}) no tiene criterios ni reglas identificables; "
                                f"no se infieren requisitos.",
                    decision_needed="Completar los criterios de aceptación de la historia.",
                    evidence=[self._evidence(story, "descripción", story.title or story.source_id)]))

        for oos in (r for r in inp.issues if r.source_type == SourceType.JIRA_OUT_OF_SCOPE):
            statement = f"Fuera de alcance: {oos.title}"
            first = next(iter(split_sentences(self._clean(oos, oos.text))), "")
            if first:
                statement += f". {first}"
            self._add(statement=statement, feature="Fuera de alcance", story_keys=[oos.source_id],
                      status=RequirementStatus.EXCLUDED, category=RequirementCategory.OUT_OF_SCOPE,
                      evidence=self._evidence(oos, "resumen y descripción", statement))

        defects = self._defects(inp, by_key, story_keys)
        self._decisions(inp, by_key)
        self._confluence(inp, story_keys)
        self._impact_links(inp, by_key)

        roles = sorted({r for req in self._requirements for r in req.roles})
        actors = sorted({a for req in self._requirements for a in req.actors})
        return FunctionalAnalysis(requirements=self._requirements, defects=defects, gaps=self._gaps,
                                  actors=actors, roles=roles, features=features, ignored_instructions=self._ignored)

    def _defects(self, inp: FunctionalInput, by_key: dict[str, SourceRecord],
                 story_keys: set[str]) -> list[HistoricalDefect]:
        defects = []
        for record in sorted((r for r in inp.issues if r.source_type == SourceType.JIRA_INCIDENT),
                             key=lambda r: r.source_id):
            linked = {link.get("outward_issue") or link.get("inward_issue") for link in record.fields.get("issuelinks", [])}
            related = sorted(k for k in linked | {record.parent_id} if k and k in story_keys)
            evidence = self._evidence(record, "resumen y descripción", record.title)
            defects.append(HistoricalDefect(key=record.source_id, summary=record.title, status=record.status,
                                            resolution=record.fields.get("resolution"),
                                            related_story_keys=related, evidence=[evidence]))
            text = self._clean(record, record.text)
            expected = None
            steps = None
            m = re.search(r"resultado esperado\s*[:\-]?\s*\n?(.+)", text, re.IGNORECASE)
            if m:
                expected = m.group(1).strip().splitlines()[0].strip(" -*")
            m = re.search(r"pasos(?: para reproducir)?\s*[:\-]?\s*\n?(.+?)(?:\n\s*\n|resultado|$)", text,
                          re.IGNORECASE | re.DOTALL)
            if m:
                steps = "; ".join(s.strip(" -*0123456789.)") for s in m.group(1).strip().splitlines() if s.strip())
            feature = by_key[related[0]].title if related else "Defectos históricos"
            closed = any(s in normalize(record.status or "") for s in _DONE_STATUS) or bool(
                record.fields.get("resolution"))
            statement = f"No debe reproducirse el defecto {record.source_id}: {record.title}"
            if not closed:
                statement += " (defecto abierto)"
            self._add(statement=statement, feature=feature, story_keys=related,
                      category=RequirementCategory.DEFECT_REGRESSION, defect_keys=[record.source_id],
                      condition=steps, outcome=expected, evidence=evidence)
        return defects

    def _decisions(self, inp: FunctionalInput, by_key: dict[str, SourceRecord]) -> None:
        markers = [normalize(m) for m in self.cfg.decision_markers]
        for comment in inp.comments:
            if comment.untrusted_instructions:
                self._clean(comment, comment.text)
                continue  # un comentario con instrucciones embebidas no aporta requisitos
            norm = normalize(comment.text)
            if not any(m in norm for m in markers):
                continue
            parent = by_key.get(comment.parent_id or "")
            if parent is None and comment.parent_id == inp.epic.source_id:
                feature, stories = f"{inp.epic.title} (general)", []
            elif parent is not None and parent.source_type in (SourceType.JIRA_STORY, SourceType.JIRA_IMPROVEMENT):
                feature, stories = parent.title, [parent.source_id]
            else:
                continue
            text = re.sub(r"^\s*\W*(?:" + "|".join(re.escape(m) for m in self.cfg.decision_markers) +
                          r")\w*\s*[:\-–]?\s*", "", comment.text.strip(), flags=re.IGNORECASE)
            for sentence in split_sentences(text) or [text]:
                if len(sentence) < 10:
                    continue
                previous = [r for r in self._requirements if r.feature == feature
                            and r.status == RequirementStatus.CONFIRMED]
                match = best_match(sentence, previous)
                evidence = self._evidence(comment, f"comentario de {comment.parent_id}", sentence)
                req = self._add(statement=sentence, feature=feature, story_keys=stories, evidence=evidence)
                if match.requirement is not None and match.score >= SUPERSEDE_THRESHOLD and \
                        match.requirement.id != req.id:
                    req.supersedes = match.requirement.id
                    # La decisión reemplaza al criterio: hereda su referencia para seguir generando su caso.
                    req.acceptance_criterion_ref = req.acceptance_criterion_ref or                         match.requirement.acceptance_criterion_ref
                    req.title = req.title or match.requirement.title
                    match.requirement.status = RequirementStatus.EXCLUDED
                    self._gaps.append(BusinessGap(
                        id=self._next_gap(), kind=GapKind.MODIFIED_RULE, severity=Severity.MINOR,
                        description=f"{req.id} (decisión en {comment.source_id}) reemplaza a {match.requirement.id}.",
                        related_requirements=[req.id, match.requirement.id], evidence=[evidence]))

    def _confluence(self, inp: FunctionalInput, story_keys: set[str]) -> None:
        for page in inp.confluence_pages:
            text = self._clean(page, page.text)
            for n, sentence in enumerate(split_sentences(text), start=1):
                if not is_strong_rule(sentence):
                    continue
                mentioned = [k for k in story_keys if k in sentence]
                evidence = self._evidence(page, f"página {page.title} · oración {n}", sentence)
                confirmed = [r for r in self._requirements if r.status == RequirementStatus.CONFIRMED]
                match = best_match(sentence, confirmed, mentioned[0] if mentioned else None)
                if match.found and match.requirement is not None:
                    if match.conflicts:
                        self._gaps.append(BusinessGap(
                            id=self._next_gap(), kind=GapKind.CONTRADICTION, severity=Severity.MAJOR,
                            description=f"Confluence ({page.title}) contradice {match.requirement.id}: "
                                        f"{'; '.join(match.conflicts)}.",
                            decision_needed="Definir cuál fuente manda.",
                            related_requirements=[match.requirement.id],
                            evidence=[evidence, *match.requirement.evidence[:1]]))
                    else:
                        match.requirement.evidence = [*match.requirement.evidence, evidence]
                    continue
                feature = next((r.feature for r in self._requirements if mentioned and mentioned[0] in r.story_keys),
                               "General")
                req = self._add(statement=sentence, feature=feature, story_keys=mentioned,
                                status=RequirementStatus.PENDING_BUSINESS_DECISION, evidence=evidence)
                self._gaps.append(BusinessGap(
                    id=self._next_gap(), kind=GapKind.DOC_ONLY_RULE, severity=Severity.MINOR,
                    description=f"{req.id} solo figura en Confluence ({page.title}); queda pendiente de negocio.",
                    decision_needed="Confirmar si la regla aplica.", related_requirements=[req.id],
                    evidence=[evidence]))

    def _impact_links(self, inp: FunctionalInput, by_key: dict[str, SourceRecord]) -> None:
        for record in inp.issues:
            ups, downs = [], []
            for link in record.fields.get("issuelinks", []):
                name = normalize(link.get("type") or "")
                blocking = any(w in name for w in ("block", "bloque", "depend"))
                if link.get("inward_issue"):
                    (ups if blocking else downs).append(f"{link['inward_issue']} ({link.get('type')})")
                if link.get("outward_issue"):
                    downs.append(f"{link['outward_issue']} ({link.get('type')})")
            if not ups and not downs:
                continue
            for req in self._requirements:
                if record.source_id in req.story_keys:
                    req.upstream = sorted(set(req.upstream) | set(ups))
                    req.downstream = sorted(set(req.downstream) | set(downs))
        for req in self._requirements:
            if req.category == RequirementCategory.INTEGRATION:
                req.downstream = sorted(set(req.downstream) | {f"integración: {shorten(req.statement, 60)}"})


__all__ = ["FunctionalValidator", "ALIGN_THRESHOLD", "ReadStatus"]

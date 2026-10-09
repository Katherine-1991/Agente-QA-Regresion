"""Agente 4 — Diseñador Gherkin.

Convierte requisitos FR en candidatos, decide (accepted/rewritten/merged/split/blocked/rejected),
deduplica, asigna IDs TC-### estables, datos de prueba, prioridad, suite y automatización, y
renderiza un `.feature` por funcionalidad. Solo él crea o reescribe casos.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from qa_regresion.domain.contracts import DesignInput, GherkinDesign, RepairRequest
from qa_regresion.domain.enums import (
    Automation,
    CandidateDecision,
    CaseKind,
    CaseStatus,
    DataProvisioning,
    GapKind,
    Priority,
    ReadStatus,
    RequirementCategory,
    RequirementStatus,
    Suite,
    Tag,
)
from qa_regresion.domain.gherkin import contains_implementation_detail, render_feature, step_signature
from qa_regresion.domain.models import (
    case_source_of,
    GherkinFeature,
    GherkinTestCase,
    NormalizedRequirement,
    TestCaseCandidate,
    TestDataSet,
)
from qa_regresion.domain.text_analysis import extract_numbers, extract_quoted, is_negative, jaccard, normalize, \
    shorten, slugify, split_gwt

_IRREGULAR = {"elegir": "elige", "hacer": "hace", "poner": "pone", "salir": "sale", "transferir": "transfiere",
              "pedir": "pide", "seguir": "sigue", "convertir": "convierte", "advertir": "advierte",
              "volver": "vuelve", "mostrar": "muestra", "probar": "prueba", "querer": "quiere", "ver": "ve",
              "ir": "va", "tener": "tiene", "obtener": "obtiene", "mover": "mueve", "aprobar": "aprueba",
              "comprobar": "comprueba", "encontrar": "encuentra", "devolver": "devuelve", "resolver": "resuelve",
              "cerrar": "cierra", "comenzar": "comienza", "empezar": "empieza", "requerir": "requiere",
              "conseguir": "consigue", "impedir": "impide", "repetir": "repite", "medir": "mide",
              "contar": "cuenta", "acordar": "acuerda", "sentir": "siente", "decir": "dice",
              "presentar": "presenta", "reintentar": "reintenta", "plegar": "pliega",
              "desplegar": "despliega"}
_SUBJECT_START = ("el ", "la ", "los ", "las ", "un ", "una ", "se ", "existe", "hay ", "no ", "ya ", "dicho",
                  "dicha", "este ", "esta ", "ese ", "esa ", "su ", "sus ")
_SPLIT_MARKERS = re.compile(r"\s+(?:y luego|y después|y despues|luego)\s+", re.IGNORECASE)


_FUNCTION_WORDS = {"el", "la", "los", "las", "un", "una", "unos", "unas", "se", "si", "al", "cada", "todo", "toda",
                   "todos", "todas", "no", "solo", "sólo", "dicho", "dicha", "este", "esta", "ese", "esa", "su", "sus",
                   "luego", "mientras", "cuando", "debe", "deben", "existe", "hay", "con", "sin", "para", "en"}


def _lower_first(text: str) -> str:
    """Pasa a minúscula solo palabras funcionales ('El sistema' → 'el sistema'), nunca nombres propios."""
    first = text.split(" ", 1)[0] if text else ""
    return text[:1].lower() + text[1:] if first.lower() in _FUNCTION_WORDS else text


# Verbos que en los criterios suelen tener sujeto no humano ("cuando finaliza la operación").
_IMPERSONAL = {"finaliza", "finalice", "termina", "termine", "carga", "cargue", "resulta", "resulte", "sea", "sean",
               "existe", "existan", "ocurre", "ocurra", "falla", "falle", "vence", "venza", "cambia", "cambie"}


def _present(infinitive: str) -> str:
    if infinitive in _IRREGULAR:
        return _IRREGULAR[infinitive]
    return infinitive[:-2] + ("a" if infinitive.endswith("ar") else "e")


def conjugate_action(condition: str) -> str:
    """'Al presionar Buscar' -> 'el usuario presiona Buscar'. Heurística simple y explicable."""
    text = re.sub(r"\s+", " ", condition).strip().rstrip(",.")
    text = re.sub(r"^(?:al|cuando|si|una vez que|mientras)\s+", "", text, flags=re.IGNORECASE)
    words = text.split(" ", 1)
    first = words[0]
    verb = first.lower()
    rest = words[1] if len(words) > 1 else ""
    reflexive = re.match(r"^([a-záéíóúñ]{3,}(?:ar|er|ir))se$", verb)
    if reflexive:
        # "plegarse" -> "se pliega"; "posicionarse sobre X" -> "el usuario se posiciona sobre X"
        conj = _present(reflexive.group(1))
        return f"el usuario se {conj} {rest}".strip() if rest else f"se {conj}"
    if verb in _IRREGULAR or (re.match(r"^[a-záéíóúñ]{3,}(ar|er|ir)$", verb)
                              and not normalize(text).startswith(_SUBJECT_START)):
        return f"el usuario {_present(verb)} {rest}".strip()
    if normalize(verb) in _IMPERSONAL or (first[:1].isupper() and first.lower() not in _FUNCTION_WORDS):
        return _lower_first(text)  # sujeto propio: "finaliza la operación", "Acción no tenga un valor"
    sentence = _lower_first(text)
    return sentence if normalize(sentence).startswith(_SUBJECT_START) or " usuario" in normalize(
        sentence)[:20] else f"el usuario {sentence}"


def _clean_outcome(outcome: str) -> str:
    text = re.sub(r"\s+", " ", outcome).strip().rstrip(".")
    text = re.sub(r"^(?:entonces|then)\s+", "", text, flags=re.IGNORECASE)
    return _lower_first(text)


def _sanitize(step: str) -> str:
    step = re.sub(r"(//\w[^\s]*|\bxpath\b\S*|\bcss\s*=\s*\S+|data-testid\S*|\bBy\.\w+\S*|#[a-z][\w-]*\b)",
                  "el elemento visible correspondiente", step, flags=re.IGNORECASE)
    return re.sub(r"<(?:div|span|input|button|a|td|tr|table|form|select|option|label|img|ul|li|p)\b[^>]*>", "",
                  step, flags=re.IGNORECASE)


class GherkinDesigner:
    name = "gherkin_designer"

    def __init__(self) -> None:
        self._cand_counter = 0

    # ------------------------------------------------------------------ candidatos

    def _cid(self) -> str:
        self._cand_counter += 1
        return f"CAND-{self._cand_counter:03d}"

    @staticmethod
    def _contested(inp: DesignInput) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for gap in inp.gaps:
            if gap.kind == GapKind.CONTRADICTION:
                for fr in gap.related_requirements:
                    reasons.setdefault(fr, f"{gap.id}: {shorten(gap.description, 160)}")
        return reasons

    @staticmethod
    def _findings_by_fr(inp: DesignInput) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for tr in inp.findings:
            for fr in tr.related_requirements:
                out.setdefault(fr, []).append(tr.id)
        return out

    def _status(self, fr: NormalizedRequirement, contested: dict[str, str],
                doc_gaps: dict[str, str]) -> tuple[CaseStatus, str | None]:
        if all(e.read_status == ReadStatus.INACCESSIBLE for e in fr.evidence):
            return CaseStatus.BLOCKED, "La evidencia de origen es inaccesible."
        if fr.id in contested:
            return CaseStatus.PENDING_BUSINESS_DECISION, f"Contradicción entre fuentes ({contested[fr.id]})"
        if fr.status == RequirementStatus.PENDING_BUSINESS_DECISION:
            return CaseStatus.PENDING_BUSINESS_DECISION, doc_gaps.get(fr.id, "Regla presente solo en documentación; "
                                                                             "requiere confirmación de negocio.")
        return CaseStatus.CONFIRMED, None

    @staticmethod
    def _actor(fr: NormalizedRequirement) -> str:
        if fr.roles:
            role = fr.roles[0]
            return f"un usuario con «{role}»" if role.startswith("permiso") else f"un usuario con el rol «{role}»"
        actors = [a for a in fr.actors if a != "usuario"]
        if actors:
            return f"un usuario {actors[0]}"
        return "un usuario autorizado"

    @staticmethod
    def _ability(fr: NormalizedRequirement) -> str | None:
        """'Solo el rol X puede aprobar devoluciones' -> 'aprobar devoluciones'."""
        m = re.search(r"\bpueden?\s+([a-záéíóúñ]+(?:ar|er|ir)\b[^.;]*)", fr.statement, re.IGNORECASE)
        return m.group(1).strip().rstrip(".") if m else None

    def _base_given(self, fr: NormalizedRequirement) -> list[str]:
        given = []
        if fr.precondition:
            given.append(_lower_first(fr.precondition.rstrip(",.")))
        else:
            given.append(f"{self._actor(fr)} autenticado en la aplicación")
        given.append(f"el usuario se encuentra en la funcionalidad «{fr.feature}»")
        return given

    def _when(self, fr: NormalizedRequirement) -> str:
        if fr.condition:
            return conjugate_action(fr.condition)
        return f"el usuario ejecuta la operación alcanzada por la regla {fr.acceptance_criterion_ref or fr.id}"

    def _then(self, fr: NormalizedRequirement) -> str:
        if fr.outcome:
            return _clean_outcome(fr.outcome)
        body = fr.statement.split(": ", 1)[-1] if fr.acceptance_criterion_ref else fr.statement
        body = shorten(body.rstrip("."), 200)
        if re.search(r"\bdeben?\b", body, re.IGNORECASE):
            return _lower_first(body)
        return f"se verifica que {_lower_first(body)}"

    def _new(self, fr: NormalizedRequirement, kind: CaseKind, title: str, given: list[str], when: list[str],
             then: list[str], status: CaseStatus, reason: str | None, findings: list[str],
             header: list[str] | None = None, rows: list[list[str]] | None = None) -> TestCaseCandidate:
        if fr.acceptance_criterion_ref:
            label = fr.acceptance_criterion_ref.split("/")[-1]
            if not title.startswith(label):
                title = f"{label} - {title}"
        return TestCaseCandidate(
            candidate_id=self._cid(), title=shorten(title.rstrip(". "), 180), kind=kind, feature=fr.feature,
            requirement_ids=[fr.id], finding_ids=findings, defect_keys=list(fr.defect_keys),
            story_keys=list(fr.story_keys), given=given, when=when, then=then, examples_header=header,
            examples_rows=rows, status_hint=status, pending_reason=reason, evidence=list(fr.evidence))

    def _boundary_examples(self, fr: NormalizedRequirement) -> tuple[list[str], list[list[str]]] | None:
        numbers = [n for n in extract_numbers(fr.statement)]
        if not numbers:
            return None
        norm = normalize(fr.statement)
        try:
            values = [float(n) for n in numbers]
        except ValueError:
            return None

        def fmt(v: float) -> str:
            return str(int(v)) if float(v).is_integer() else f"{v:.2f}"

        step = 1.0 if all(float(v).is_integer() for v in values) else 0.01
        header = ["valor", "resultado"]
        minimum = any(w in norm for w in ("minimo", "al menos", "mayor a", "mayor que", "superior", "desde",
                                          "como minimo"))
        maximum = any(w in norm for w in ("maximo", "no mas de", "hasta", "menor a", "menor que", "inferior",
                                          "como maximo"))
        if re.search(r"\bentre\b", norm) and len(values) >= 2:
            low, high = sorted(values[:2])
            rows = [[fmt(low), "acepta"], [fmt(high), "acepta"], [fmt(low - step), "rechaza"],
                    [fmt(high + step), "rechaza"]]
        elif any(w in norm for w in ("digito", "caracter", "longitud")):
            n = int(values[0])
            base = "1234567890" * (n // 10 + 2) if "digito" in norm else "A" * (n + 2)
            if minimum:
                rows = [[base[:n], "acepta"], [base[:n + 1], "acepta"], [base[:max(n - 1, 0)], "rechaza"]]
            elif maximum:
                rows = [[base[:n], "acepta"], [base[:max(n - 1, 0)], "acepta"], [base[:n + 1], "rechaza"]]
            else:
                rows = [[base[:n], "acepta"], [base[:max(n - 1, 0)], "rechaza"], [base[:n + 1], "rechaza"]]
        elif minimum:
            v = values[0]
            rows = [[fmt(v), "acepta"], [fmt(v - step), "rechaza"]]
        else:
            v = values[0]
            rows = [[fmt(v), "acepta"], [fmt(v + step), "rechaza"]]
        return header, rows

    def _acceptance_case(self, fr: NormalizedRequirement, status: CaseStatus, reason: str | None,
                         findings: list[str]) -> TestCaseCandidate:
        """Un criterio de aceptación = un caso: su Dado/Cuando/Entonces, sin complementos inventados."""
        title = fr.title or shorten(fr.statement, 160)
        feature_step = f"el usuario se encuentra en la funcionalidad «{fr.feature}»"
        if fr.condition and fr.outcome:
            given = [_lower_first(fr.precondition.rstrip(",.")) if fr.precondition
                     else f"{self._actor(fr)} autenticado en la aplicación"]
            if not fr.precondition:
                given.append(feature_step)
            when = [conjugate_action(fr.condition)]
            then = [_clean_outcome(fr.outcome)]
        elif fr.category == RequirementCategory.ROLE_PERMISSION and self._ability(fr):
            ability = self._ability(fr)
            given = [f"{self._actor(fr)} autenticado en la aplicación", feature_step]
            when = [conjugate_action(ability)]
            then = [f"el sistema {'no ' if is_negative(fr.statement) else ''}permite {ability}"]
        else:
            # Criterio sin acción explícita: se verifica al acceder a la funcionalidad.
            given = [f"{self._actor(fr)} autenticado en la aplicación"]
            when = [f"el usuario accede a la funcionalidad «{fr.feature}»"]
            then = [self._then(fr)]
        if fr.category == RequirementCategory.BOUNDARY:
            examples = self._boundary_examples(fr)
            if examples:
                header, rows = examples
                return self._new(fr, CaseKind.BOUNDARY, title, [*given, f"rige el criterio «{shorten(fr.statement, 140)}»"],
                                 ["el usuario ingresa el valor <valor> en el dato alcanzado por el criterio y confirma"],
                                 ["el sistema <resultado> el valor ingresado"], status, reason, findings, *examples)
        negative = is_negative(fr.statement) or is_negative(" ".join(then))
        kind = {
            RequirementCategory.ROLE_PERMISSION: CaseKind.ROLE,
            RequirementCategory.STATE_TRANSITION: CaseKind.STATE_TRANSITION,
            RequirementCategory.INTEGRATION: CaseKind.INTEGRATION,
        }.get(fr.category, CaseKind.NEGATIVE if negative else CaseKind.POSITIVE)
        return self._new(fr, kind, title, given, when, then, status, reason, findings)

    def candidates_for(self, fr: NormalizedRequirement, inp: DesignInput, contested: dict[str, str],
                       doc_gaps: dict[str, str], findings_by_fr: dict[str, list[str]]) -> list[TestCaseCandidate]:
        if fr.status == RequirementStatus.EXCLUDED or fr.category == RequirementCategory.OUT_OF_SCOPE:
            return []
        if fr.condition is None and fr.outcome is None and fr.category != RequirementCategory.DEFECT_REGRESSION:
            body = fr.statement.split(": ", 1)[-1] if fr.acceptance_criterion_ref else fr.statement
            pre, cond, out = split_gwt(body)
            if cond and out:
                fr = fr.model_copy(update={"precondition": fr.precondition or pre, "condition": cond,
                                           "outcome": out})
        status, reason = self._status(fr, contested, doc_gaps)
        findings = findings_by_fr.get(fr.id, [])
        if fr.acceptance_criterion_ref and fr.category != RequirementCategory.DEFECT_REGRESSION:
            return [self._acceptance_case(fr, status, reason, findings)]
        given = self._base_given(fr)
        when = self._when(fr)
        then = self._then(fr)
        short = shorten(fr.statement.split(": ", 1)[0] if fr.acceptance_criterion_ref else fr.statement, 90)
        out: list[TestCaseCandidate] = []
        cat = fr.category
        negative_rule = is_negative(fr.statement)

        if cat == RequirementCategory.DEFECT_REGRESSION:
            key = fr.defect_keys[0] if fr.defect_keys else fr.id
            steps = [s.strip() for s in (fr.condition or "").split(";") if s.strip()]
            setup = [conjugate_action(s) for s in steps[:-1]]
            action = conjugate_action(steps[-1]) if steps else f"el usuario repite el flujo reportado en {key}"
            out.append(self._new(
                fr, CaseKind.DEFECT_REGRESSION, f"Regresión del defecto {key}: {shorten(fr.statement.split(': ', 1)[-1], 70)}",
                [f"el usuario se encuentra en la funcionalidad «{fr.feature}»", *setup],
                [action],
                [_clean_outcome(fr.outcome) if fr.outcome else f"el error descrito en {key} no se reproduce"],
                status, reason, findings))
        elif cat == RequirementCategory.ROLE_PERMISSION:
            ability = self._ability(fr)
            role_when = conjugate_action(ability) if ability and not fr.condition else when
            if negative_rule:
                out.append(self._new(fr, CaseKind.ROLE, f"Acceso denegado: {short}", given, [role_when], [then],
                                     status, reason, findings))
            else:
                allowed = f"el sistema permite {ability}" if ability and not fr.outcome else then
                out.append(self._new(fr, CaseKind.ROLE, f"Rol autorizado: {short}", given, [role_when], [allowed],
                                     status, reason, findings))
                if fr.roles:
                    denied = f"un usuario autenticado sin {'el rol ' if not fr.roles[0].startswith('permiso') else ''}" \
                             f"«{fr.roles[0]}»"
                else:
                    denied = f"un usuario autenticado que no cumple: {shorten(given[0], 80)}"
                refusal = f"el sistema no permite {ability}" if ability else \
                    "el sistema no permite completar la operación"
                out.append(self._new(fr, CaseKind.ROLE, f"Rol no autorizado: {short}", [denied, given[1]],
                                     [role_when], [refusal], status, reason, findings))
        elif cat == RequirementCategory.STATE_TRANSITION:
            states = re.findall(r"\b(?:estado|a)\s+[\"“«]([^\"”»]+)[\"”»]", fr.statement, re.IGNORECASE)
            g = list(given)
            t = then
            if len(states) >= 2:
                g.insert(1, f"existe un registro en estado «{states[0]}»")
                if not fr.outcome:
                    t = f"el registro queda en estado «{states[1]}»"
            out.append(self._new(fr, CaseKind.STATE_TRANSITION, f"Transición de estado: {short}", g, [when], [t],
                                 status, reason, findings))
        elif cat == RequirementCategory.BOUNDARY:
            examples = self._boundary_examples(fr)
            if examples:
                header, rows = examples
                out.append(self._new(
                    fr, CaseKind.BOUNDARY, f"Valores límite: {short}",
                    [*given, f"rige la regla «{shorten(fr.statement, 140)}»"],
                    ["el usuario ingresa el valor <valor> en el dato alcanzado por la regla y confirma"],
                    ["el sistema <resultado> el valor ingresado"], status, reason, findings, header, rows))
            else:
                out.append(self._new(fr, CaseKind.POSITIVE if not negative_rule else CaseKind.NEGATIVE,
                                     f"Regla de límite: {short}", given, [when], [then], status, reason, findings))
        elif cat == RequirementCategory.VALIDATION:
            literal = extract_quoted(fr.statement)
            if negative_rule or fr.outcome:
                out.append(self._new(fr, CaseKind.NEGATIVE, f"Validación: {short}", given, [when], [then],
                                     status, reason, findings))
            else:
                msg = f"el sistema informa el mensaje «{literal[0]}»" if literal else \
                    "el sistema informa el incumplimiento de la validación y no continúa"
                out.append(self._new(fr, CaseKind.NEGATIVE, f"Validación incumplida: {short}", given,
                                     ["el usuario confirma la operación sin cumplir la validación"], [msg],
                                     status, reason, findings))
            neutral = [f"{self._actor(fr)} autenticado en la aplicación", given[-1],
                       f"los datos ingresados cumplen la regla «{shorten(fr.statement, 120)}»"]
            out.append(self._new(fr, CaseKind.POSITIVE, f"Validación cumplida: {short}", neutral,
                                 ["el usuario confirma la operación cumpliendo la validación"],
                                 ["el sistema acepta los datos y permite continuar"], status, reason, findings))
        elif cat == RequirementCategory.INTEGRATION:
            out.append(self._new(fr, CaseKind.INTEGRATION, f"Integración: {short}", given, [when], [then],
                                 status, reason, findings))
        elif cat == RequirementCategory.PERSISTENCE:
            out.append(self._new(fr, CaseKind.POSITIVE, f"Persistencia: {short}", given, [when],
                                 [then, "al consultar nuevamente, la información registrada se conserva"],
                                 status, reason, findings))
        else:
            kind = CaseKind.NEGATIVE if negative_rule else CaseKind.POSITIVE
            out.append(self._new(fr, kind, short, given, [when], [then], status, reason, findings))
        return out

    # ------------------------------------------------------------------ decisiones

    def _decide(self, candidates: list[TestCaseCandidate]) -> list[TestCaseCandidate]:
        result: list[TestCaseCandidate] = []
        for cand in candidates:
            if not cand.evidence:
                cand.decision, cand.decision_reason = CandidateDecision.REJECTED, "sin evidencia trazable"
                result.append(cand)
                continue
            steps = [cand.title, *cand.given, *cand.when, *cand.then]
            if any(contains_implementation_detail(s) for s in steps):
                cand.title = _sanitize(cand.title)
                cand.given = [_sanitize(s) for s in cand.given]
                cand.when = [_sanitize(s) for s in cand.when]
                cand.then = [_sanitize(s) for s in cand.then]
                cand.decision = CandidateDecision.REWRITTEN
                cand.decision_reason = "se quitaron selectores o detalles internos"
            parts = _SPLIT_MARKERS.split(cand.when[0]) if len(cand.when) == 1 else cand.when
            if len(parts) > 1 and not cand.examples_header:
                cand.decision, cand.decision_reason = CandidateDecision.SPLIT, "tenía más de una acción principal"
                result.append(cand)
                for i, part in enumerate(parts, start=1):
                    child = cand.model_copy(update={
                        "candidate_id": self._cid(), "title": shorten(f"{cand.title} (parte {i})", 110),
                        "when": [conjugate_action(part)], "decision": CandidateDecision.ACCEPTED,
                        "decision_reason": f"derivado de {cand.candidate_id}"}, deep=True)
                    result.append(child)
                continue
            if cand.status_hint == CaseStatus.BLOCKED:
                cand.decision, cand.decision_reason = CandidateDecision.BLOCKED, cand.pending_reason
            elif cand.decision is None:
                cand.decision = CandidateDecision.ACCEPTED
            result.append(cand)
        # Deduplicación: firma exacta o casi igual del mismo tipo.
        kept: list[TestCaseCandidate] = []
        for cand in result:
            if cand.decision in (CandidateDecision.REJECTED, CandidateDecision.SPLIT):
                continue
            sig = step_signature(cand.given, cand.when, cand.then)
            # Casi duplicado: mismo resultado esperado (literal) y precondición/acción casi iguales.
            twin = next((k for k in kept if k.kind == cand.kind and k.feature == cand.feature
                         and k.examples_rows == cand.examples_rows and (
                             step_signature(k.given, k.when, k.then) == sig or (
                                 [normalize(t) for t in k.then] == [normalize(t) for t in cand.then] and
                                 jaccard(" ".join(k.given + k.when), " ".join(cand.given + cand.when)) >= 0.9))),
                        None)
            if twin is not None:
                twin.requirement_ids = sorted(set(twin.requirement_ids) | set(cand.requirement_ids))
                twin.finding_ids = sorted(set(twin.finding_ids) | set(cand.finding_ids))
                twin.story_keys = sorted(set(twin.story_keys) | set(cand.story_keys))
                twin.defect_keys = sorted(set(twin.defect_keys) | set(cand.defect_keys))
                twin.evidence = [*twin.evidence, *[e for e in cand.evidence if e not in twin.evidence]]
                if cand.status_hint == CaseStatus.PENDING_BUSINESS_DECISION:
                    twin.status_hint = CaseStatus.PENDING_BUSINESS_DECISION
                    twin.pending_reason = twin.pending_reason or cand.pending_reason
                cand.decision, cand.merged_into = CandidateDecision.MERGED, twin.candidate_id
                cand.decision_reason = f"duplicado de {twin.candidate_id}"
                continue
            kept.append(cand)
        return result

    # ------------------------------------------------------------------ casos

    @staticmethod
    def stable_key(cand: TestCaseCandidate, reqs: dict[str, NormalizedRequirement]) -> str:
        statements = sorted(normalize(reqs[r].statement) for r in cand.requirement_ids if r in reqs)
        variant = "outline" if cand.examples_header else normalize(" ".join(cand.then))[:80]
        raw = "|".join([normalize(cand.feature), cand.kind.value, *statements, variant])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _dataset_for(self, cand: TestCaseCandidate, reqs: dict[str, NormalizedRequirement]) -> TestDataSet:
        values: dict[str, str] = {}
        fr_list = [reqs[r] for r in cand.requirement_ids if r in reqs]
        for fr in fr_list:
            for i, role in enumerate(fr.roles, start=1):
                values[f"rol_{i}" if i > 1 else "rol"] = role
            for i, literal in enumerate(extract_quoted(fr.statement), start=1):
                values.setdefault(f"texto_{i}", literal)
            for i, number in enumerate(extract_numbers(fr.statement), start=1):
                values.setdefault(f"valor_{i}", number)
        if cand.examples_rows:
            values["ejemplos"] = "; ".join(f"{r[0]}→{r[1]}" for r in cand.examples_rows)
        description = f"Datos para «{shorten(cand.title, 70)}»"
        if values:
            return TestDataSet(id="TD-000", description=description, provisioning=DataProvisioning.EXPLICIT,
                               values=values, requirement_ids=cand.requirement_ids)
        return TestDataSet(id="TD-000", description=description, provisioning=DataProvisioning.TO_PROVISION,
                           notes=("preparar en el ambiente de prueba un usuario con acceso a la funcionalidad "
                                  f"«{cand.feature}»"),
                           requirement_ids=cand.requirement_ids)

    def _to_case(self, cand: TestCaseCandidate, tc_id: str, dataset: TestDataSet,
                 reqs: dict[str, NormalizedRequirement]) -> GherkinTestCase:
        fr_list = [reqs[r] for r in cand.requirement_ids if r in reqs]
        status = cand.status_hint
        tags: list[Tag] = []
        kind_tags = {
            CaseKind.POSITIVE: [Tag.POSITIVE], CaseKind.NEGATIVE: [Tag.NEGATIVE], CaseKind.BOUNDARY: [Tag.BOUNDARY],
            CaseKind.ROLE: [Tag.ROLE, Tag.NEGATIVE if "no autorizado" in cand.title.lower() or
                            "denegado" in cand.title.lower() or is_negative(" ".join(cand.given + cand.then))
                            else Tag.POSITIVE],
            CaseKind.STATE_TRANSITION: [Tag.STATE_TRANSITION, Tag.POSITIVE],
            CaseKind.INTEGRATION: [Tag.INTEGRATION, Tag.POSITIVE],
            CaseKind.DEFECT_REGRESSION: [Tag.DEFECT_REGRESSION],
        }
        tags.extend(kind_tags[cand.kind])
        from_ac = any(fr.acceptance_criterion_ref for fr in fr_list)
        positive = Tag.POSITIVE in tags or cand.kind == CaseKind.STATE_TRANSITION
        critical = status == CaseStatus.CONFIRMED and from_ac and positive
        suite = Suite.CRITICAL_PATH if critical else Suite.FULL_REGRESSION
        if critical:
            tags.append(Tag.CRITICAL_PATH)
        tags.append(Tag.FULL_REGRESSION)
        concrete = all(fr.condition and fr.outcome for fr in fr_list) or cand.kind == CaseKind.BOUNDARY
        automation = Automation.CANDIDATE if (
            status == CaseStatus.CONFIRMED and dataset.provisioning == DataProvisioning.EXPLICIT and concrete
            and cand.kind != CaseKind.INTEGRATION) else Automation.MANUAL_ONLY
        tags.append(Tag.AUTOMATION_CANDIDATE if automation == Automation.CANDIDATE else Tag.MANUAL_ONLY)
        if status == CaseStatus.PENDING_BUSINESS_DECISION:
            tags.append(Tag.PENDING_BUSINESS_DECISION)
        if cand.kind in (CaseKind.DEFECT_REGRESSION, CaseKind.ROLE) or critical:
            priority = Priority.ALTA
        elif status == CaseStatus.BLOCKED:
            priority = Priority.BAJA
        else:
            priority = Priority.MEDIA
        preconditions = [*cand.given]
        if dataset.provisioning == DataProvisioning.EXPLICIT:
            preconditions.append(f"Datos de prueba {dataset.id}: "
                                 + "; ".join(f"{k}={v}" for k, v in dataset.values.items()))
        else:
            preconditions.append(f"Datos de prueba {dataset.id}: {dataset.notes}")
        return GherkinTestCase(
            id=tc_id, title=cand.title, feature=cand.feature, kind=cand.kind, tags=list(dict.fromkeys(tags)),
            given=[*cand.given, f"se dispone de los datos de prueba «{dataset.id}»"], when=cand.when,
            then=cand.then, examples_header=cand.examples_header, examples_rows=cand.examples_rows,
            preconditions=preconditions, dataset_id=dataset.id, priority=priority, suite=suite,
            automation=automation, status=status, requirement_ids=sorted(cand.requirement_ids),
            finding_ids=sorted(cand.finding_ids), defect_keys=sorted(cand.defect_keys),
            story_keys=sorted(cand.story_keys), source_ids=sorted({e.source_id for e in cand.evidence}),
            pending_reason=cand.pending_reason if status != CaseStatus.CONFIRMED else None,
            evidence=cand.evidence, candidate_ids=[cand.candidate_id])

    def _assemble(self, inp: DesignInput, candidates: list[TestCaseCandidate]) -> GherkinDesign:
        reqs = {r.id: r for r in inp.requirements}
        stable = dict(inp.stable_ids)
        used_numbers = {int(v.split("-")[1]) for v in stable.values()}
        accepted = [c for c in candidates if c.decision in (CandidateDecision.ACCEPTED, CandidateDecision.REWRITTEN,
                                                            CandidateDecision.BLOCKED)]
        datasets: OrderedDict[str, TestDataSet] = OrderedDict()
        cases: list[GherkinTestCase] = []
        assigned: set[str] = set()
        for cand in accepted:
            key = self.stable_key(cand, reqs)
            tc_id = stable.get(key)
            if tc_id is None or tc_id in assigned:
                number = max(used_numbers, default=0) + 1
                used_numbers.add(number)
                tc_id = f"TC-{number:03d}"
                stable[key] = tc_id
            assigned.add(tc_id)
            ds = self._dataset_for(cand, reqs)
            sig = repr((ds.provisioning, sorted(ds.values.items()), ds.notes))
            if sig not in datasets:
                datasets[sig] = ds.model_copy(update={"id": f"TD-{len(datasets) + 1:03d}"})
            else:
                merged = datasets[sig]
                merged.requirement_ids = sorted(set(merged.requirement_ids) | set(ds.requirement_ids))
            cases.append(self._to_case(cand, tc_id, datasets[sig], reqs))
        cases.sort(key=lambda c: c.id)
        # Smoke: el primer caso de camino crítico de cada funcionalidad.
        seen_features: set[str] = set()
        for case in cases:
            if case.suite == Suite.CRITICAL_PATH and case.feature not in seen_features:
                seen_features.add(case.feature)
                case.suite = Suite.SMOKE
                case.tags = [Tag.SMOKE, *case.tags]
        return self._with_features(inp, candidates, cases, list(datasets.values()), stable)

    def _with_features(self, inp: DesignInput, candidates: list[TestCaseCandidate], cases: list[GherkinTestCase],
                       datasets: list[TestDataSet], stable: dict[str, str]) -> GherkinDesign:
        by_feature: OrderedDict[str, list[GherkinTestCase]] = OrderedDict()
        for case in cases:
            by_feature.setdefault(case.feature, []).append(case)
        features = []
        names: set[str] = set()
        for name, group in by_feature.items():
            file_name = slugify(name, 50)
            base, n = file_name, 2
            while file_name in names:
                file_name, n = f"{base}-{n}", n + 1
            names.add(file_name)
            stories = sorted({k for c in group for k in c.story_keys})
            description = (f"Regresión funcional de {inp.epic_key} - {inp.epic_name}.\n"
                           f"Historias: {', '.join(stories) if stories else '—'}")
            features.append(GherkinFeature(name=name, file_name=f"{file_name}.feature", description=description,
                                           case_ids=[c.id for c in group],
                                           text=render_feature(name, description, group, inp.language)))
        return GherkinDesign(candidates=candidates, cases=cases, datasets=datasets, features=features,
                             stable_ids=stable)

    # ------------------------------------------------------------------ API

    def design(self, inp: DesignInput) -> GherkinDesign:
        self._cand_counter = 0
        contested = self._contested(inp)
        findings_by_fr = self._findings_by_fr(inp)
        doc_gaps = {fr: f"{g.id}: {shorten(g.description, 140)}" for g in inp.gaps
                    if g.kind == GapKind.DOC_ONLY_RULE for fr in g.related_requirements}
        candidates: list[TestCaseCandidate] = []
        for fr in inp.requirements:
            if case_source_of(fr) in inp.case_sources:
                candidates.extend(self.candidates_for(fr, inp, contested, doc_gaps, findings_by_fr))
        return self._assemble(inp, self._decide(candidates))

    def repair(self, inp: DesignInput, design: GherkinDesign, request: RepairRequest) -> GherkinDesign:
        """Aplica correcciones dirigidas por los hallazgos del auditor."""
        candidates = [c.model_copy(deep=True) for c in design.candidates]
        reqs = {r.id: r for r in inp.requirements}
        contested = self._contested(inp)
        findings_by_fr = self._findings_by_fr(inp)
        doc_gaps = {fr: f"{g.id}: {shorten(g.description, 140)}" for g in inp.gaps
                    if g.kind == GapKind.DOC_ONLY_RULE for fr in g.related_requirements}
        self._cand_counter = max((int(c.candidate_id.split("-")[1]) for c in candidates), default=0)
        case_to_cands = {c.id: c.candidate_ids for c in design.cases}
        for finding in request.findings:
            targets = finding.target_ids
            if finding.category in ("uncovered-requirement", "uncovered-acceptance-criterion", "defect-not-covered",
                                    "missing-boundary-case"):
                for fr_id in targets:
                    if fr_id in reqs:
                        new = self._decide(self.candidates_for(reqs[fr_id], inp, contested, doc_gaps, findings_by_fr))
                        candidates.extend(new)
            elif finding.category in ("missing-story-link", "pending-status-missing"):
                for tc in targets:
                    for cid in case_to_cands.get(tc, []):
                        cand = next((c for c in candidates if c.candidate_id == cid), None)
                        if cand is None:
                            continue
                        if finding.category == "missing-story-link":
                            cand.story_keys = sorted({k for r in cand.requirement_ids if r in reqs
                                                      for k in reqs[r].story_keys} | set(cand.story_keys))
                        else:
                            cand.status_hint = CaseStatus.PENDING_BUSINESS_DECISION
                            cand.pending_reason = cand.pending_reason or finding.message
            elif finding.category in ("duplicate-case", "invalid-gherkin"):
                candidates = self._decide([c.model_copy(update={"decision": None, "merged_into": None})
                                           if c.decision != CandidateDecision.SPLIT else c for c in candidates
                                           if c.decision != CandidateDecision.MERGED])
        return self._assemble(inp, candidates)

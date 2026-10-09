"""Renderizado y validación de Gherkin en español (sin dependencias externas)."""

from __future__ import annotations

import re

from qa_regresion.domain.enums import Tag
from qa_regresion.domain.models import GherkinTestCase

ALLOWED_TAGS = {t.value for t in Tag}
_TC_TAG = re.compile(r"^@TC-\d{3,}$")
GIVEN = ("Dado", "Dada", "Dados", "Dadas")
WHEN = ("Cuando",)
THEN = ("Entonces",)
AND = ("Y", "E", "Pero")
STEP_KEYWORDS = GIVEN + WHEN + THEN + AND
SCENARIO_KEYWORDS = ("Escenario:", "Esquema del escenario:")

# Detalles de implementación que no deben aparecer en los pasos.
_IMPLEMENTATION_DETAIL = re.compile(
    r"(//\w+|\bxpath\b|\bcss\s*=|data-testid|\bBy\.\w+|\.click\(|\bselector\b|"
    r"<(?:div|span|input|button|a|td|tr|table|form|select|option|label|img|ul|li|p)\b[^>]*>|"
    r"\bdocument\.query|\bgetElementBy|#[a-z][\w-]*\b(?!-))",
    re.IGNORECASE,
)


def contains_implementation_detail(text: str) -> bool:
    return bool(_IMPLEMENTATION_DETAIL.search(text or ""))


def _escape_cell(value: str) -> str:
    return str(value).replace("|", "\\|").strip()


def render_scenario(case: GherkinTestCase, indent: str = "  ") -> str:
    lines: list[str] = []
    tags = [f"@{case.id}"] + [t.value for t in case.tags]
    lines.append(indent + " ".join(tags))
    keyword = "Esquema del escenario" if case.examples_header else "Escenario"
    lines.append(f"{indent}{keyword}: {case.id} - {case.title}")
    inner = indent * 2
    trace = [", ".join(case.requirement_ids)]
    if case.finding_ids:
        trace.append(", ".join(case.finding_ids))
    lines.append(f"{inner}# Trazabilidad: {' | '.join(trace)}")
    if case.story_keys:
        lines.append(f"{inner}# Historias: {', '.join(case.story_keys)}")
    if case.defect_keys:
        lines.append(f"{inner}# Defectos: {', '.join(case.defect_keys)}")
    lines.append(f"{inner}# Datos: {case.dataset_id} | Estado: {case.status.value} | Prioridad: {case.priority.value}")
    if case.pending_reason:
        lines.append(f"{inner}# Pendiente: {case.pending_reason}")
    for i, step in enumerate(case.given):
        keyword = "Y"
        if i == 0:
            keyword = "Dado que" if re.match(r"^(el|la|los|las|existe|existen|se|hay)\b", step) else "Dado"
        lines.append(f"{inner}{keyword} {step}")
    for i, step in enumerate(case.when):
        lines.append(f"{inner}{'Cuando' if i == 0 else 'Y'} {step}")
    for i, step in enumerate(case.then):
        lines.append(f"{inner}{'Entonces' if i == 0 else 'Y'} {step}")
    if case.examples_header and case.examples_rows:
        lines.append("")
        lines.append(f"{inner}Ejemplos:")
        lines.append(f"{inner}{indent}| " + " | ".join(_escape_cell(h) for h in case.examples_header) + " |")
        for row in case.examples_rows:
            lines.append(f"{inner}{indent}| " + " | ".join(_escape_cell(c) for c in row) + " |")
    return "\n".join(lines)


def render_feature(name: str, description: str, cases: list[GherkinTestCase], language: str = "es") -> str:
    out = [f"# language: {language}", f"Característica: {name}"]
    for line in description.splitlines():
        out.append(f"  {line}".rstrip())
    for case in cases:
        out.append("")
        out.append(render_scenario(case))
    return "\n".join(out) + "\n"


def validate_feature(text: str, language: str = "es") -> list[str]:
    """Valida estructura Gherkin. Devuelve una lista de errores (vacía si es válida)."""
    errors: list[str] = []
    lines = text.splitlines()
    non_empty = [(i + 1, line.strip()) for i, line in enumerate(lines) if line.strip()]
    if not non_empty:
        return ["archivo vacío"]
    if non_empty[0][1].replace(" ", "") != f"#language:{language}":
        errors.append(f"línea {non_empty[0][0]}: falta '# language: {language}'")
    features = [n for n, line in non_empty if line.startswith("Característica:")]
    if len(features) != 1:
        errors.append(f"se esperaba exactamente una 'Característica:' y hay {len(features)}")

    scenario: dict | None = None
    pending_tags: list[str] = []
    scenarios: list[dict] = []

    def close(sc: dict | None) -> None:
        if sc is None:
            return
        scenarios.append(sc)

    for n, line in non_empty:
        if line.startswith("#"):
            continue
        if line.startswith("@"):
            pending_tags = line.split()
            for tag in pending_tags:
                if tag not in ALLOWED_TAGS and not _TC_TAG.match(tag):
                    errors.append(f"línea {n}: etiqueta no permitida {tag}")
            continue
        if line.startswith(SCENARIO_KEYWORDS):
            close(scenario)
            title = line.split(":", 1)[1].strip()
            scenario = {"line": n, "title": title, "outline": line.startswith("Esquema"), "tags": pending_tags,
                        "given": 0, "when": 0, "then": 0, "last": None, "steps": [], "examples": [],
                        "in_examples": False}
            pending_tags = []
            if not title:
                errors.append(f"línea {n}: escenario sin título")
            continue
        if line.startswith("Ejemplos:"):
            if scenario is None or not scenario["outline"]:
                errors.append(f"línea {n}: 'Ejemplos:' fuera de un esquema de escenario")
            else:
                scenario["in_examples"] = True
            continue
        if line.startswith("|"):
            if scenario is None or not scenario["in_examples"]:
                errors.append(f"línea {n}: tabla fuera de 'Ejemplos:'")
            else:
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
                scenario["examples"].append((n, cells))
            continue
        if line.startswith(("Característica:", "Antecedentes:", "Regla:")):
            continue
        keyword = line.split(" ", 1)[0]
        if keyword in STEP_KEYWORDS:
            if scenario is None:
                if features:
                    errors.append(f"línea {n}: paso fuera de un escenario")
                continue
            step_text = line.split(" ", 1)[1] if " " in line else ""
            if not step_text.strip():
                errors.append(f"línea {n}: paso vacío")
            if contains_implementation_detail(step_text):
                errors.append(f"línea {n}: el paso contiene selectores o detalles internos")
            kind = ("given" if keyword in GIVEN else "when" if keyword in WHEN else
                    "then" if keyword in THEN else scenario["last"])
            if kind is None:
                errors.append(f"línea {n}: '{keyword}' sin paso previo")
                continue
            order = {"given": 0, "when": 1, "then": 2}
            if scenario["last"] is not None and order[kind] < order[scenario["last"]]:
                errors.append(f"línea {n}: orden de pasos inválido ({scenario['last']} → {kind})")
            if keyword in WHEN or (keyword in AND and kind == "when"):
                scenario["when"] += 1
            elif kind == "given":
                scenario["given"] += 1
            elif kind == "then":
                scenario["then"] += 1
            scenario["last"] = kind
            scenario["steps"].append(step_text)
            continue
        if scenario is None:
            continue  # descripción libre de la característica
        errors.append(f"línea {n}: texto no reconocido dentro del escenario: {line[:60]}")
    close(scenario)

    if not scenarios:
        errors.append("la característica no tiene escenarios")
    for sc in scenarios:
        where = f"escenario línea {sc['line']}"
        if not any(_TC_TAG.match(t) for t in sc["tags"]):
            errors.append(f"{where}: falta la etiqueta @TC-###")
        if sc["given"] < 1:
            errors.append(f"{where}: falta 'Dado' (precondición)")
        if sc["when"] != 1:
            errors.append(f"{where}: debe tener exactamente una acción principal 'Cuando' (tiene {sc['when']})")
        if sc["then"] < 1:
            errors.append(f"{where}: falta 'Entonces' (resultado observable)")
        placeholders = {p for step in sc["steps"] for p in re.findall(r"<([^<>]+)>", step)}
        if sc["outline"]:
            if not placeholders:
                errors.append(f"{where}: el esquema no usa parámetros <...> en sus pasos")
            if len(sc["examples"]) < 2:
                errors.append(f"{where}: el esquema necesita encabezado y al menos una fila de ejemplos")
            else:
                header = sc["examples"][0][1]
                for n, row in sc["examples"][1:]:
                    if len(row) != len(header):
                        errors.append(f"línea {n}: la fila de ejemplos no coincide con el encabezado")
                missing = placeholders - set(header)
                if missing:
                    errors.append(f"{where}: parámetros sin columna en Ejemplos: {sorted(missing)}")
        elif placeholders:
            errors.append(f"{where}: parámetros <...> en un escenario que no es esquema")
    return errors


def validate_case(case: GherkinTestCase) -> list[str]:
    text = render_feature(case.feature, "", [case])
    return validate_feature(text)


def step_signature(case_given: list[str], case_when: list[str], case_then: list[str]) -> str:
    from qa_regresion.domain.text_analysis import normalize

    return "|".join(normalize(s) for s in [*case_given, "::", *case_when, "::", *case_then])

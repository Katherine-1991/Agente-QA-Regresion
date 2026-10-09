"""Utilidades deterministas de análisis de texto en español.

Son heurísticas simples y explicables: normalización, tokens, similitud, extracción de
valores y clasificación por palabras clave. No interpretan ni ejecutan el contenido.
"""

from __future__ import annotations

import re
import unicodedata

from qa_regresion.domain.enums import RequirementCategory

STOPWORDS = {
    "a", "al", "ante", "con", "contra", "de", "del", "desde", "e", "el", "en", "entre", "es", "esta", "este",
    "esto", "la", "las", "lo", "los", "o", "para", "por", "que", "se", "si", "sin", "sobre", "su", "sus", "u",
    "un", "una", "uno", "unos", "unas", "y", "ya", "le", "les", "como", "cuando", "debe", "deben", "debera",
    "deberan", "sera", "son", "ser", "hay", "the", "and", "of", "to", "in", "is", "be", "pantalla", "sistema",
    "usuario", "campo", "valor", "dato", "datos", "mismo", "misma", "cada", "todo", "toda", "todos", "todas",
    "solo", "puede", "pueden", "tiene", "tienen", "esta", "estan", "nos", "otra", "otro", "luego", "entonces",
}

_NUMBER_RE = re.compile(r"(?<![\w\-/])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)(?![\w\-/])")
_QUOTED_RE = re.compile(r"[\"“”«»]([^\"“”«»]{2,200})[\"“”«»]|'([^']{2,200})'")
_ROLE_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*_ROL[A-Z0-9_]*\b|\bROL_[A-Z0-9_]+\b")
# El nombre del rol debe empezar en mayúscula ("rol Supervisor", "rol CONT_ROL_X"); "un rol operativo" no es un rol.
_ROLE_NAMED_RE = re.compile(r"\b(?:[Rr]ol|[Pp]erfil)\s+(?:de\s+)?[\"“«']?([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+)")
_PERMISSION_RE = re.compile(r"\bpermiso\s*\(?\s*(\d+)\s*\)?", re.IGNORECASE)

ACTOR_WORDS = [
    "supervisor", "operador", "solicitante", "administrador", "analista", "cliente", "auditor", "tesorero",
    "gerente", "jefe", "empleado", "encargado", "contador", "usuario",
]

CATEGORY_KEYWORDS: list[tuple[RequirementCategory, tuple[str, ...]]] = [
    (RequirementCategory.ROLE_PERMISSION, ("rol ", "roles", "permiso", "perfil ", "autoriz", "no esta autorizado",
                                           "acceso a la pantalla", "solo el ", "unicamente el ", "solo los ",
                                           "unicamente los ")),
    (RequirementCategory.STATE_TRANSITION, ("estado", "pasa a", "cambia a", "transici", "-->")),
    (RequirementCategory.BOUNDARY, ("maximo", "minimo", "limite", "no mas de", "al menos", "hasta ", "caracteres",
                                    "longitud", "digitos", "mayor a", "menor a", "mayor que", "menor que",
                                    "superior a", "inferior a", "entre ")),
    (RequirementCategory.INTEGRATION, ("integra", "servicio externo", "servicio rest", " api ", "endpoint",
                                       "notific", "correo", "email", "interfaz con", "sistema externo",
                                       "webservice", "web service", "se envia a", "envia una", "envia un")),
    (RequirementCategory.VALIDATION, ("obligatori", "requerid", "valid", "formato", "error", "mensaje",
                                      "no permite", "no se permite", "debe ingresar", "completar", "invalid")),
    (RequirementCategory.PERSISTENCE, ("guarda", "registra", "persist", "almacena", "graba", "base de datos",
                                       "se crea", "historial", "se genera")),
]

RULE_MARKERS = (
    "debe", "deber", "obligatori", "no puede", "no se permite", "no permite", "solo ", "unicamente", "maximo",
    "minimo", "valida", "permite", "habilita", "deshabilita", "muestra", "mensaje", "requiere", "estado",
    "rol ", "permiso", "guarda", "registra", "envia", "notifica", "se genera", "se calcula", "al presionar",
    "al seleccionar", "al ingresar", "si el", "si la", "si no", "cuando el", "cuando la", "no debe",
)

NEGATIVE_MARKERS = (" no ", "nunca", "prohib", "impide", "opcional", "no obligatori", "deshabilit", "rechaz")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    text = strip_accents(text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def _stem(token: str) -> str:
    for suffix in ("ciones", "cion", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9_]+", normalize(text))
    return {_stem(w) for w in words if len(w) > 2 and w not in STOPWORDS}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def similarity(a: str, b: str) -> float:
    """Similitud combinada: prioriza la coincidencia de vocabulario sin castigar textos más largos."""
    return max(jaccard(a, b), 0.85 * overlap(a, b))


def canonical_number(raw: str) -> str:
    value = raw.replace(".", "").replace(",", ".")
    if value.endswith(".0"):
        value = value[:-2]
    return value


def extract_numbers(text: str) -> list[str]:
    return [canonical_number(m.group(1)) for m in _NUMBER_RE.finditer(text or "")]


def extract_quoted(text: str) -> list[str]:
    out: list[str] = []
    for m in _QUOTED_RE.finditer(text or ""):
        literal = (m.group(1) or m.group(2) or "").strip()
        if literal and literal not in out:
            out.append(literal)
    return out


def extract_values(text: str) -> list[str]:
    values = extract_quoted(text)
    for number in extract_numbers(text):
        if number not in values:
            values.append(number)
    return values


def is_negative(text: str) -> bool:
    padded = f" {normalize(text)} "
    return any(marker in padded for marker in NEGATIVE_MARKERS)


def opposite_polarity(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    pairs = [("obligatori", "opcional"), ("habilita", "deshabilita"), ("permite", "no permite"),
             ("visible", "oculto"), ("editable", "no editable")]
    for left, right in pairs:
        a_left = left in na and right not in na
        a_right = right in na
        b_left = left in nb and right not in nb
        b_right = right in nb
        if (a_left and b_right) or (a_right and b_left):
            return True
    return False


def classify_category(text: str) -> RequirementCategory:
    norm = f" {normalize(text)} "
    has_number = bool(extract_numbers(text))
    for category, keywords in CATEGORY_KEYWORDS:
        if category == RequirementCategory.BOUNDARY and not has_number:
            continue
        if category == RequirementCategory.ROLE_PERMISSION and not (
                detect_roles(text) or any(k in norm for k in ("permiso", "autoriz", "perfil "))):
            continue
        if any(k in norm for k in keywords):
            return category
    return RequirementCategory.BUSINESS_RULE


STRONG_RULE_MARKERS = (
    " debe ", " deben ", " debera ", " deberan ", " no debe", " no puede", " no se permite", " no permite",
    "obligatori", " solo ", " unicamente ", "maximo", "minimo", " max.", " se valida", " valida que",
    " mensaje", "no esta autorizado", " requiere", " al menos ", " si el ", " si la ", " si no ", " en caso de ",
    " dado que ", " al presionar", " al seleccionar", " al ingresar", " al hacer clic", " error ",
)


def is_rule_like(text: str) -> bool:
    norm = f" {normalize(text)} "
    if len(norm.strip()) < 15:
        return False
    return any(marker in norm for marker in RULE_MARKERS)


def is_strong_rule(text: str) -> bool:
    """Regla normativa explícita. Se exige en texto narrativo para no convertir descripciones en requisitos."""
    norm = f" {normalize(text)} "
    if len(norm.strip()) < 15:
        return False
    return any(marker in norm for marker in STRONG_RULE_MARKERS)


_MARKUP = [
    (re.compile(r"\\([*_`#>\[\]|])"), r"\1"),          # escapes de markdown
    (re.compile(r"\*{1,3}|_{2,3}|`+"), ""),             # negrita, cursiva, código
    (re.compile(r"^\s*(?:#{1,6}\s+|>\s*)"), ""),        # títulos y citas
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),      # enlaces
]


def clean_markup(text: str) -> str:
    for pattern, repl in _MARKUP:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


def detect_roles(text: str) -> list[str]:
    roles: list[str] = []
    for m in _ROLE_CODE_RE.finditer(text or ""):
        if m.group(0) not in roles:
            roles.append(m.group(0))
    for m in _ROLE_NAMED_RE.finditer(text or ""):
        name = m.group(1)
        if name.lower() not in {"de", "del", "que", "el", "la"} and name not in roles:
            roles.append(name)
    for m in _PERMISSION_RE.finditer(text or ""):
        label = f"permiso {m.group(1)}"
        if label not in roles:
            roles.append(label)
    return roles


def detect_actors(text: str) -> list[str]:
    norm = normalize(text)
    return [a for a in ACTOR_WORDS if re.search(rf"\b{a}(es|s)?\b", norm)]


_CONDITION_PATTERNS = [
    re.compile(r"^\s*dado\s+(?:que\s+)?(?P<given>.+?)\s*,?\s+cuando\s+(?P<cond>.+?)\s*,?\s+entonces\s+(?P<out>.+)$",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*(?:si|cuando|mientras)\s+(?P<cond>.+?)\s*,\s*(?:entonces\s+)?(?P<out>.+)$",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*(?P<cond>al\s+.+?)\s*,\s*(?P<out>.+)$", re.IGNORECASE | re.DOTALL),
    # "En Devolución, si el usuario ingresa X, el sistema debe Y"
    re.compile(r"^\s*(?P<given>[^,]{3,60}),\s*(?:si|cuando|al)\s+(?P<cond>.+?)\s*,\s*(?P<out>.+)$",
               re.IGNORECASE | re.DOTALL),
    # "Al seleccionar una Acción deben habilitarse todos los componentes" (sin coma)
    re.compile(r"^\s*(?P<cond>(?:al|cuando|si|mientras)\s+.+?)\s+(?P<out>(?:deben?|deber[aá]n?|el sistema|se)\b.+)$",
               re.IGNORECASE | re.DOTALL),
    # "El campo Acción debe visualizarse vacío al ingresar a la pantalla"
    re.compile(r"^\s*(?P<out>.+?\b(?:debe|deben)\b.+?)\s+(?P<cond>(?:al|cuando)\s+[a-záéíóúñ]+(?:ar|er|ir)\b.*)$",
               re.IGNORECASE | re.DOTALL),
]


def split_gwt(text: str) -> tuple[str | None, str | None, str | None]:
    """Separa 'Dado.. cuando X entonces Y' / 'Si X, Y' / 'Al X, Y' en (precondición, condición, resultado)."""
    clean = re.sub(r"\s+", " ", text or "").strip().rstrip(".")
    for pattern in _CONDITION_PATTERNS:
        m = pattern.match(clean)
        if m:
            cond = m.group("cond").strip().rstrip(",")
            out = m.group("out").strip()
            given = m.groupdict().get("given")
            if len(cond) >= 4 and len(out) >= 4:
                return (given.strip().rstrip(",") if given else None), cond, out
    return None, None, None


def split_condition(text: str) -> tuple[str | None, str | None]:
    _, cond, out = split_gwt(text)
    return cond, out


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-")
    return (slug[:max_len].rstrip("-")) or "funcionalidad"


def shorten(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_sentences(text: str) -> list[str]:
    """Divide texto en líneas/oraciones candidatas a regla, conservando viñetas."""
    chunks: list[str] = []
    for line in re.split(r"\r?\n", text or ""):
        line = re.sub(r"^\s*(?:[-*•·]|\d+[.)]|[a-z][.)])\s+", "", line).strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])", line)
        chunks.extend(p.strip() for p in parts if p.strip())
    return chunks

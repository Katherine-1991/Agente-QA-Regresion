"""Validaciones transversales: contenido no confiable, huellas de contenido y contratos entre agentes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from qa_regresion.domain.text_analysis import normalize

M = TypeVar("M", bound=BaseModel)


class SchemaViolation(RuntimeError):
    """Un agente produjo un objeto que no cumple su contrato."""


class WriteAttemptBlocked(RuntimeError):
    """Se intentó una operación remota de escritura fuera del flujo de publicación aprobado."""


# Patrones de instrucciones embebidas en fuentes no confiables (texto normalizado, sin tildes).
_INJECTION_PATTERNS = [
    r"\b(ignora|ignore|olvida|forget|omite|disregard)\w*\b.{0,40}\b(instruccion|instruction|regla|rule|indicacion|prompt)",
    r"\bpublica(r|lo|los|las)?\b.{0,40}\b(caso|casos|test|tests|todo|jira)\b",
    r"\bpublicalo\b",
    r"\b(aprob|autoriz)\w*\b.{0,30}\b(publica|publicacion|publish)",
    r"^\W*(system|sistema|assistant|asistente)\s*:",
    r"\b(approved|authorized)\b.{0,30}\bpublish",
    r"\b(system prompt|prompt del sistema|you are now|ahora eres|actua como|act as)\b",
    r"\b(ejecuta|ejecutar|run|execute)\b.{0,20}\b(comando|command|script|powershell|bash|cmd)\b",
    r"\b(rm -rf|curl\s+https?://|invoke-webrequest|del /s)\b",
    r"\b(envia|enviame|dame|comparte|share|send|reveal|revela)\w*\b.{0,40}\b(token|contrasena|password|api key|apikey|credencial|secret)",
    r"\b(cambia|modifica|amplia|reduce)\w*\b.{0,20}\b(el alcance|the scope)\b",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _INJECTION_PATTERNS]


def detect_untrusted_instructions(text: str) -> list[str]:
    """Devuelve los fragmentos de `text` que intentan dirigir al agente.

    Nunca se ejecutan: se registran y el fragmento queda en cuarentena (no genera requisitos).
    """
    hits: list[str] = []
    for line in re.split(r"\r?\n|(?<=[.!?])\s+", text or ""):
        norm = normalize(line)
        if not norm:
            continue
        if any(rx.search(norm) for rx in _INJECTION_RE):
            snippet = line.strip()[:200]
            if snippet not in hits:
                hits.append(snippet)
    return hits


def strip_untrusted_instructions(text: str, snippets: list[str]) -> str:
    for snippet in snippets:
        text = text.replace(snippet, " ")
    return text


def canonical_json(data: Any) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def validate_exchange(model: type[M], obj: Any, *, producer: str) -> M:
    """Revalida un objeto intercambiado entre agentes contra su esquema (ida y vuelta por JSON)."""
    try:
        payload = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
        return model.model_validate(payload)
    except ValidationError as exc:
        raise SchemaViolation(f"{producer} produjo un {model.__name__} inválido: {exc}") from exc

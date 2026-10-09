"""Lectura opcional de Figma (solo lectura). Si no hay conexión, se registra la limitación y se sigue."""

from __future__ import annotations

from typing import Protocol

from qa_regresion.domain.enums import ReadStatus, SourceType
from qa_regresion.domain.models import SourceRecord
from qa_regresion.domain.validation import detect_untrusted_instructions


class DesignReader(Protocol):
    def available(self) -> bool: ...

    def read(self, epic_key: str) -> tuple[list[SourceRecord], list[str]]: ...


class UnavailableFigmaAdapter:
    """Implementación por defecto: el proceso local no tiene un conector de Figma."""

    def __init__(self, reason: str = "Figma no está conectado a este proceso; se omite el diseño."):
        self.reason = reason

    def available(self) -> bool:
        return False

    def read(self, epic_key: str) -> tuple[list[SourceRecord], list[str]]:
        return [], [self.reason]


class StaticFigmaAdapter:
    """Frames de Figma ya exportados (por ejemplo, por otra herramienta de solo lectura) como texto."""

    def __init__(self, frames: list[dict[str, str]]):
        self.frames = frames

    def available(self) -> bool:
        return True

    def read(self, epic_key: str) -> tuple[list[SourceRecord], list[str]]:
        records = []
        for frame in self.frames:
            text = frame.get("text", "")
            records.append(SourceRecord(
                source_id=f"figma:{frame['id']}", source_type=SourceType.FIGMA, title=frame.get("name", frame["id"]),
                location=frame.get("url", f"figma:{frame['id']}"), read_status=ReadStatus.PARTIAL, text=text,
                limitations=["diseño: solo textos visibles"], untrusted_instructions=detect_untrusted_instructions(text),
                fields={"units": [{"location": frame.get("name", frame["id"]), "text": text, "meta": {}}]}))
        return records, []

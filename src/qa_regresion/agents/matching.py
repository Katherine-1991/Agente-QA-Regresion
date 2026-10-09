"""Comparación determinista entre afirmaciones de distintas fuentes."""

from __future__ import annotations

from dataclasses import dataclass, field

from qa_regresion.domain.models import NormalizedRequirement
from qa_regresion.domain.text_analysis import (
    extract_numbers,
    extract_quoted,
    normalize,
    opposite_polarity,
    similarity,
)

ALIGN_THRESHOLD = 0.45
LITERAL_THRESHOLD = 0.6


@dataclass
class Match:
    requirement: NormalizedRequirement | None
    score: float
    conflicts: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.requirement is not None and self.score >= ALIGN_THRESHOLD


def conflicts_between(a: str, b: str, score: float) -> list[str]:
    conflicts: list[str] = []
    nums_a, nums_b = set(extract_numbers(a)), set(extract_numbers(b))
    if nums_a and nums_b and not (nums_a & nums_b):
        conflicts.append(f"valores {sorted(nums_a)} vs {sorted(nums_b)}")
    if opposite_polarity(a, b):
        conflicts.append("polaridad opuesta (p. ej. obligatorio vs opcional)")
    if score >= LITERAL_THRESHOLD:
        lit_a = {normalize(x) for x in extract_quoted(a)}
        lit_b = {normalize(x) for x in extract_quoted(b)}
        if lit_a and lit_b and not (lit_a & lit_b):
            conflicts.append(f"textos {sorted(lit_a)} vs {sorted(lit_b)}")
    return conflicts


def best_match(text: str, requirements: list[NormalizedRequirement], story_hint: str | None = None) -> Match:
    best: NormalizedRequirement | None = None
    best_score = 0.0
    for req in requirements:
        score = similarity(text, req.statement)
        if story_hint and story_hint in req.story_keys:
            score += 0.1
        if score > best_score:
            best, best_score = req, score
    if best is None:
        return Match(None, 0.0)
    return Match(best, min(best_score, 1.0), conflicts_between(text, best.statement, best_score))

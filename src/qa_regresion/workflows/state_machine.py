"""Transiciones permitidas del flujo. Cualquier salto no declarado es un error de programación."""

from __future__ import annotations

from qa_regresion.domain.enums import WorkflowPhase as P
from qa_regresion.domain.models import PhaseTransition, WorkflowState

TRANSITIONS: dict[P, set[P]] = {
    P.RECEIVED: {P.RESOLVING_EPIC, P.FAILED},
    P.RESOLVING_EPIC: {P.AWAITING_EPIC_SELECTION, P.COLLECTING_SOURCES, P.FAILED},
    P.AWAITING_EPIC_SELECTION: {P.RESOLVING_EPIC, P.COLLECTING_SOURCES, P.FAILED},
    P.COLLECTING_SOURCES: {P.VALIDATING_FUNCTIONAL, P.FAILED},
    P.VALIDATING_FUNCTIONAL: {P.VALIDATING_DOCUMENTATION, P.FAILED},
    P.VALIDATING_DOCUMENTATION: {P.CONSOLIDATING, P.FAILED},
    P.CONSOLIDATING: {P.DESIGNING_CASES, P.FAILED},
    P.DESIGNING_CASES: {P.AUDITING, P.FAILED},
    P.AUDITING: {P.REPAIRING, P.GENERATING_DRAFT, P.FAILED},
    P.REPAIRING: {P.AUDITING, P.FAILED},
    P.GENERATING_DRAFT: {P.AWAITING_PUBLICATION_APPROVAL, P.BLOCKED, P.FAILED},
    P.AWAITING_PUBLICATION_APPROVAL: {P.PUBLISHING, P.FAILED},
    P.PUBLISHING: {P.PUBLISHED, P.PARTIALLY_PUBLISHED, P.FAILED},
    P.PARTIALLY_PUBLISHED: {P.AWAITING_PUBLICATION_APPROVAL},
    P.PUBLISHED: set(),
    P.BLOCKED: set(),
    P.FAILED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class WorkflowStateMachine:
    def __init__(self, state: WorkflowState):
        self.state = state
        if not state.history:
            state.history = [PhaseTransition(from_phase=None, to_phase=state.phase, note="inicio")]

    def go(self, target: P, note: str = "") -> None:
        current = self.state.phase
        if target not in TRANSITIONS[current]:
            raise InvalidTransition(f"transición no permitida: {current.value} → {target.value}")
        self.state.phase = target
        self.state.history = [*self.state.history, PhaseTransition(from_phase=current, to_phase=target, note=note)]

    def fail(self, message: str) -> None:
        self.state.message = message
        if P.FAILED in TRANSITIONS[self.state.phase]:
            self.go(P.FAILED, message)

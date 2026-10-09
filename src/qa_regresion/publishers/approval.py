"""Candado de publicación.

Una publicación solo se habilita si, después de mostrar el borrador, llega por el canal del
usuario un mensaje independiente que es exactamente el comando configurado ("Publicalo").
La autorización es de un solo uso y queda invalidada si cambia el contenido o el destino.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from qa_regresion.domain.enums import MessageSource, WorkflowPhase
from qa_regresion.domain.models import ApprovalRecord, PublicationPreview, WorkflowState


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ApprovalRejected(PermissionError):
    pass


@dataclass(frozen=True)
class UserMessage:
    text: str
    source: MessageSource
    received_at: str = field(default_factory=now_iso)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ApprovalGate:
    def __init__(self, approval_command: str = "Publicalo"):
        self.approval_command = approval_command

    def is_exact_command(self, text: str) -> bool:
        # Solo se toleran saltos de línea/espacios alrededor (los agrega la terminal); el contenido debe ser exacto.
        return text.strip(" \r\n\t") == self.approval_command

    def grant(self, state: WorkflowState, message: UserMessage, preview: PublicationPreview) -> ApprovalRecord:
        if message.source != MessageSource.USER_CHAT:
            raise ApprovalRejected(f"una aprobación no puede provenir de {message.source.value}")
        if state.phase != WorkflowPhase.AWAITING_PUBLICATION_APPROVAL:
            raise ApprovalRejected(f"el flujo está en {state.phase.value}, no en AWAITING_PUBLICATION_APPROVAL")
        if not state.draft_presented_at:
            raise ApprovalRejected("todavía no se mostró el borrador")
        if _ts(message.received_at) <= _ts(state.draft_presented_at):
            raise ApprovalRejected("la autorización es anterior al borrador mostrado")
        if not self.is_exact_command(message.text):
            raise ApprovalRejected(f"se requiere el mensaje independiente exacto '{self.approval_command}'")
        if state.approval and state.approval.consumed and state.approval.consumed_at and \
                _ts(state.draft_presented_at) <= _ts(state.approval.consumed_at):
            raise ApprovalRejected("la autorización anterior ya se usó; hay que volver a mostrar el borrador")
        if preview.content_fingerprint != state.draft_fingerprint or \
                preview.destination_fingerprint != state.destination_fingerprint:
            raise ApprovalRejected("el borrador o el destino cambiaron desde que se mostró")
        if preview.run_id != state.run_id:
            raise ApprovalRejected("la vista previa no corresponde a esta ejecución")
        record = ApprovalRecord(token_id=uuid.uuid4().hex, run_id=state.run_id,
                                draft_fingerprint=preview.content_fingerprint,
                                destination_fingerprint=preview.destination_fingerprint, granted_at=now_iso())
        state.approval = record
        return record

    def consume(self, state: WorkflowState, preview: PublicationPreview) -> ApprovalRecord:
        record = state.approval
        if record is None:
            raise ApprovalRejected("no hay una autorización vigente")
        if record.consumed:
            raise ApprovalRejected("la autorización es de un solo uso y ya fue utilizada")
        if record.draft_fingerprint != preview.content_fingerprint:
            raise ApprovalRejected("el contenido cambió después de la autorización")
        if record.destination_fingerprint != preview.destination_fingerprint:
            raise ApprovalRejected("el destino cambió después de la autorización")
        record.consumed = True
        record.consumed_at = now_iso()
        state.approval = record
        return record

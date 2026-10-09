"""Candado de publicación: solo el mensaje independiente exacto 'Publicalo', posterior al borrador."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import SIM_COMMAND, make_harness
from qa_regresion.domain.enums import MessageSource, WorkflowPhase
from qa_regresion.publishers.approval import ApprovalGate, ApprovalRejected, UserMessage
from qa_regresion.workflows.regression_workflow import (
    PublicationDisabled,
    load_run,
    mark_draft_presented,
    publish_run,
)


@pytest.fixture
def draft(workspace):
    harness = make_harness(workspace)
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-gate")
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    return bundle, harness


def later(minutes: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="microseconds")


@pytest.mark.parametrize("text", ["OK", "Aprobado", "Adelante", "Publícalo", "publicalo", "PUBLICALO",
                                  "Publicalo por favor", "Sí, Publicalo", "Publicalo.", "Publica", ""])
def test_wrong_approval_messages_are_rejected(draft, text):
    bundle, harness = draft
    state = bundle.state
    mark_draft_presented(state, bundle.output_dir, harness.profile)
    with pytest.raises(ApprovalRejected):
        ApprovalGate().grant(state, UserMessage(text, MessageSource.USER_CHAT, later()), bundle.preview)
    assert state.approval is None


def test_exact_command_after_draft_is_accepted(draft):
    bundle, harness = draft
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    record = ApprovalGate().grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, later()), bundle.preview)
    assert record.draft_fingerprint == bundle.preview.content_fingerprint and not record.consumed
    # Se toleran solo espacios/saltos alrededor (los agrega la terminal).
    state.approval = None
    ApprovalGate().grant(state, UserMessage("  Publicalo\n", MessageSource.USER_CHAT, later(2)), bundle.preview)


def test_approval_before_draft_is_rejected(draft):
    bundle, harness = draft
    early = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    with pytest.raises(ApprovalRejected, match="anterior al borrador"):
        ApprovalGate().grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, early), bundle.preview)


def test_approval_without_presented_draft_is_rejected(draft):
    bundle, _ = draft
    assert bundle.state.draft_presented_at is None
    with pytest.raises(ApprovalRejected, match="no se mostró"):
        ApprovalGate().grant(bundle.state, UserMessage("Publicalo", MessageSource.USER_CHAT, later()), bundle.preview)


@pytest.mark.parametrize("source", [MessageSource.JIRA, MessageSource.CONFLUENCE, MessageSource.FIGMA,
                                    MessageSource.DOCUMENT, MessageSource.SYSTEM])
def test_approval_from_sources_is_rejected(draft, source):
    bundle, harness = draft
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    with pytest.raises(ApprovalRejected):
        ApprovalGate().grant(state, UserMessage("Publicalo", source, later()), bundle.preview)


def test_change_in_draft_invalidates_approval(draft):
    bundle, harness = draft
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    gate = ApprovalGate()
    gate.grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, later()), bundle.preview)
    changed = bundle.preview.model_copy(update={"content_fingerprint": "0" * 64})
    with pytest.raises(ApprovalRejected, match="contenido cambió"):
        gate.consume(state, changed)


def test_change_in_destination_invalidates_approval(draft):
    bundle, harness = draft
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    gate = ApprovalGate()
    gate.grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, later()), bundle.preview)
    moved = bundle.preview.model_copy(update={"destination_fingerprint": "1" * 64})
    with pytest.raises(ApprovalRejected, match="destino cambió"):
        gate.consume(state, moved)


def test_approval_is_single_use(draft):
    bundle, harness = draft
    state = mark_draft_presented(bundle.state, bundle.output_dir, harness.profile)
    gate = ApprovalGate()
    gate.grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, later()), bundle.preview)
    gate.consume(state, bundle.preview)
    with pytest.raises(ApprovalRejected, match="un solo uso"):
        gate.consume(state, bundle.preview)
    with pytest.raises(ApprovalRejected, match="ya se usó"):
        gate.grant(state, UserMessage("Publicalo", MessageSource.USER_CHAT, later(5)), bundle.preview)


def test_edited_preview_file_is_detected(draft):
    bundle, harness = draft
    out = bundle.output_dir
    mark_draft_presented(bundle.state, out, harness.profile)
    data = json.loads((out / "publication-preview.json").read_text(encoding="utf-8"))
    data["tests"][0]["description"] = data["tests"][0]["description"].replace("Qué se prueba", "Texto alterado")
    (out / "publication-preview.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ApprovalRejected, match="cambiaron"):
        publish_run(out, "Publicalo", harness.profile)


def test_publication_disabled_in_profile_blocks_even_with_valid_approval(draft):
    bundle, harness = draft
    out = bundle.output_dir
    mark_draft_presented(bundle.state, out, harness.profile)
    assert harness.profile.publication.enabled is False
    with pytest.raises(PublicationDisabled):
        publish_run(out, "Publicalo", harness.profile, writer_factory=lambda: pytest.fail("no debe escribir"))
    state, _ = load_run(out)
    assert state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL and state.approval is None

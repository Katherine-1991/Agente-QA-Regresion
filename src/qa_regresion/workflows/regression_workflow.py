"""Cableado del flujo: construcción de dependencias, ejecución en dry-run, presentación del borrador
y publicación detrás del candado de aprobación."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from qa_regresion.adapters.atlassian_mcp import AtlassianReader, McpStdioGateway, ReadOnlyGuard
from qa_regresion.adapters.fixture_gateway import FixtureGateway
from qa_regresion.agents.orchestrator import QALeadOrchestrator, RunBundle
from qa_regresion.config import ProjectProfile, load_mcp_server_config
from qa_regresion.domain.enums import MessageSource, WorkflowPhase
from qa_regresion.domain.models import PublicationPreview, WorkflowState
from qa_regresion.publishers.approval import ApprovalGate, ApprovalRejected, UserMessage, now_iso
from qa_regresion.publishers.jira_publisher import (
    JiraPublisher,
    JiraWriter,
    PublicationJournal,
    PublicationResult,
    recompute_content_fingerprint,
)
from qa_regresion.workflows.artifacts import ATTACHMENTS, ArtifactWriter
from qa_regresion.workflows.state_machine import WorkflowStateMachine


class PublicationDisabled(PermissionError):
    pass


@dataclass
class Session:
    orchestrator: QALeadOrchestrator
    guard: ReadOnlyGuard
    gateway: Any

    def close(self) -> None:
        if hasattr(self.gateway, "close"):
            self.gateway.close()


def open_read_session(profile: ProjectProfile, project_root: Path, *, mode: str,
                      fixtures_dir: Path | None = None, sleep: Callable[[float], None] | None = None) -> Session:
    """Abre una sesión de SOLO LECTURA. En modo MCP fuerza READ_ONLY_MODE en el servidor."""
    if mode == "fixtures":
        if fixtures_dir is None:
            raise ValueError("el modo fixtures requiere --fixtures")
        gateway: Any = FixtureGateway.from_dir(fixtures_dir)
    elif mode == "mcp":
        server = load_mcp_server_config(profile.atlassian.mcp)
        env = {**server["env"], **profile.atlassian.mcp.read_only_env}
        gateway = McpStdioGateway(server["command"], server["args"], env,
                                  profile.atlassian.mcp.timeout_seconds).start()
    else:
        raise ValueError(f"modo Atlassian desconocido: {mode}")
    guard = ReadOnlyGuard(gateway, profile.atlassian.mcp.read_tools)
    kwargs = {"sleep": sleep} if sleep else {}
    reader = AtlassianReader(guard, profile, base_url=profile.atlassian.base_url, **kwargs)
    orchestrator = QALeadOrchestrator(profile, project_root, reader, read_guard=guard)
    return Session(orchestrator, guard, gateway)


def run_regression(profile: ProjectProfile, project_root: Path, command: str, *, mode: str,
                   fixtures_dir: Path | None = None, selected_key: str | None = None,
                   run_id: str | None = None, pilot_size: int | None = None) -> RunBundle:
    session = open_read_session(profile, project_root, mode=mode, fixtures_dir=fixtures_dir)
    try:
        return session.orchestrator.run(command, run_id=run_id, selected_key=selected_key, pilot_size=pilot_size)
    finally:
        session.close()


# ---------------------------------------------------------------------- borrador y aprobación


def load_run(output_dir: Path) -> tuple[WorkflowState, PublicationPreview]:
    state = WorkflowState.model_validate_json((output_dir / "workflow-state.json").read_text(encoding="utf-8"))
    preview = PublicationPreview.model_validate_json(
        (output_dir / "publication-preview.json").read_text(encoding="utf-8"))
    return state, preview


def mark_draft_presented(state: WorkflowState, output_dir: Path, profile: ProjectProfile) -> WorkflowState:
    """Registra que el borrador se mostró. Solo después puede llegar un 'Publicalo' válido."""
    if state.phase == WorkflowPhase.PARTIALLY_PUBLISHED:
        WorkflowStateMachine(state).go(WorkflowPhase.AWAITING_PUBLICATION_APPROVAL, "reintento tras publicación parcial")
    if state.phase != WorkflowPhase.AWAITING_PUBLICATION_APPROVAL:
        raise ApprovalRejected(f"no hay borrador esperando aprobación (estado {state.phase.value})")
    state.draft_presented_at = now_iso()
    ArtifactWriter(profile).save_state(state, output_dir)
    return state


def publish_run(output_dir: Path, message_text: str, profile: ProjectProfile, *,
                writer_factory: Callable[[], JiraWriter] | None = None,
                source: MessageSource = MessageSource.USER_CHAT) -> PublicationResult:
    """Valida el mensaje de aprobación y, si la publicación está habilitada, publica sin duplicar."""
    state, preview = load_run(output_dir)
    gate = ApprovalGate(profile.publication.approval_command)
    recomputed = recompute_content_fingerprint(preview)
    checked = preview.model_copy(update={"content_fingerprint": recomputed})
    message = UserMessage(text=message_text, source=source)
    gate.grant(state, message, checked)  # lanza ApprovalRejected si algo no corresponde
    if not profile.publication.enabled:
        state.approval = None
        ArtifactWriter(profile).save_state(state, output_dir)
        raise PublicationDisabled("Aprobación recibida, pero publication.enabled=false en el perfil: no se escribió "
                                  "nada en Jira.")
    if writer_factory is None:
        raise PublicationDisabled("No hay un JiraWriter configurado para publicar.")
    sm = WorkflowStateMachine(state)
    sm.go(WorkflowPhase.PUBLISHING, "aprobación válida")
    journal = PublicationJournal.load(output_dir / "publication-journal.json")
    writer = writer_factory()
    publisher = JiraPublisher(writer, profile, journal)
    attachments = [str((output_dir / name).resolve()) for name in ATTACHMENTS if (output_dir / name).exists()]
    try:
        result = publisher.publish(checked, state, gate, attachments)
    finally:
        gateway = getattr(writer, "gateway", None)
        if gateway is not None and hasattr(gateway, "close"):
            gateway.close()
    sm.go(result.phase, result.error or "")
    state.remote_write_calls += len(result.created) + len(result.updated) + result.links_created
    ArtifactWriter(profile).save_state(state, output_dir)
    (output_dir / "publication-result.json").write_text(json.dumps({
        "phase": result.phase.value, "created": result.created, "updated": result.updated,
        "unchanged": result.unchanged, "links_created": result.links_created, "error": result.error,
        "plan": result.plan.model_dump(mode="json")}, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def mcp_writer_factory(profile: ProjectProfile, cwd: Path | None = None) -> Callable[[], JiraWriter]:
    """Writer real (sin READ_ONLY_MODE). Solo se usa tras una aprobación válida y con publication.enabled."""
    from qa_regresion.publishers.jira_publisher import McpJiraWriter

    def factory() -> JiraWriter:
        server = load_mcp_server_config(profile.atlassian.mcp)
        gateway = McpStdioGateway(server["command"], server["args"], server["env"],
                                  profile.atlassian.mcp.timeout_seconds,
                                  cwd=str(cwd or Path.cwd())).start()
        return McpJiraWriter(gateway, profile)

    return factory

from __future__ import annotations

import pytest

from conftest import load_confluence, load_jira, make_harness
from qa_regresion.adapters.atlassian_mcp import (
    AtlassianReader,
    GatewayError,
    RateLimitError,
    ReadOnlyGuard,
    looks_like_write,
)
from qa_regresion.adapters.fixture_gateway import FixtureGateway
from qa_regresion.domain.enums import SourceType, WorkflowPhase
from qa_regresion.domain.validation import WriteAttemptBlocked


def reader_for(profile, gateway, sleeps=None):
    guard = ReadOnlyGuard(gateway, profile.atlassian.mcp.read_tools)
    return AtlassianReader(guard, profile, sleep=(sleeps.append if sleeps is not None else (lambda s: None))), guard


# ------------------------------------------------------------------ resolución de épicas


def test_epic_resolution_unique_by_name(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    found = reader.resolve_epic("Portal de Devoluciones")
    assert [c.key for c in found] == ["QAS-100"]


def test_epic_resolution_by_key_returns_canonical(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    found = reader.resolve_epic("qas-100")
    assert [(c.key, c.summary) for c in found] == [("QAS-100", "Portal de Devoluciones")]


def test_epic_resolution_key_that_is_not_an_epic(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    assert reader.resolve_epic("QAS-101") == []


def test_epic_resolution_ambiguous_requires_selection(workspace):
    jira = load_jira()
    jira["issues"].append({"key": "QAS-950", "summary": "Portal de Devoluciones - Legado",
                           "issue_type": {"name": "Epic"}, "status": {"name": "Cerrado"},
                           "project": {"key": "QAS"}, "components": [], "issuelinks": []})
    harness = make_harness(workspace, jira=jira)
    bundle = harness.orchestrator.run("/QA-Regresión Portal de Devoluciones", run_id="r-amb")
    assert bundle.state.phase == WorkflowPhase.AWAITING_EPIC_SELECTION
    assert {c.key for c in bundle.state.epic_candidates} == {"QAS-100", "QAS-950"}
    assert bundle.design is None  # no avanza sin selección

    harness = make_harness(workspace, jira=jira)
    chosen = harness.orchestrator.run("/QA-Regresión Portal de Devoluciones", run_id="r-sel",
                                      selected_key="QAS-100")
    assert chosen.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    assert chosen.state.epic_key == "QAS-100"


def test_epic_not_found_fails_cleanly(workspace):
    harness = make_harness(workspace)
    bundle = harness.orchestrator.run("/QA-Regresión Épica Inexistente", run_id="r-nf")
    assert bundle.state.phase == WorkflowPhase.FAILED
    assert "No se encontró" in bundle.state.message


def test_invalid_command_is_rejected(workspace):
    harness = make_harness(workspace)
    bundle = harness.orchestrator.run("/OtroComando QAS-100", run_id="r-bad")
    assert bundle.state.phase == WorkflowPhase.FAILED


# ------------------------------------------------------------------ paginación, reintentos, dedupe


def test_pagination_collects_all_pages(profile):
    gateway = FixtureGateway(load_jira(), load_confluence(), server_page_size=2)
    reader, _ = reader_for(profile, gateway)
    issues, complete = reader.search_all("parent = QAS-100", "summary")
    assert complete
    assert sorted(i["key"] for i in issues) == ["QAS-101", "QAS-102", "QAS-103", "QAS-104", "QAS-200"]
    starts = [args["start_at"] for tool, args in gateway.calls if tool == "jira_search"]
    assert starts == [0, 2, 4]  # 5 hijas en páginas de 2


def test_pagination_respects_max_pages(profile):
    profile.atlassian.max_pages = 1
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence(), server_page_size=2))
    issues, complete = reader.search_all("parent = QAS-100", "summary")
    assert not complete and len(issues) == 2
    assert any("truncada" in x for x in reader.limitations)


def test_bounded_retries_on_rate_limit(profile):
    sleeps: list[float] = []
    gateway = FixtureGateway(load_jira(), load_confluence(), fail_first=2)
    reader, _ = reader_for(profile, gateway, sleeps)
    assert reader.resolve_epic("QAS-100")[0].key == "QAS-100"
    assert len(sleeps) == 2


def test_retries_exhausted_raise(profile):
    gateway = FixtureGateway(load_jira(), load_confluence(), fail_first=10)
    reader, _ = reader_for(profile, gateway)
    with pytest.raises(RateLimitError):
        reader.resolve_epic("QAS-100")
    assert len([c for c in gateway.calls if c[0] == "jira_get_issue"]) == profile.atlassian.retry.max_attempts


def test_collect_relations_dedup_and_custom_fields(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    collection = reader.collect("QAS-100")
    keys = [r.source_id for r in collection.issues]
    assert len(keys) == len(set(keys))  # deduplicado (dos consultas de hijas devuelven lo mismo)
    types = {r.source_id: r.source_type for r in collection.issues}
    assert types["QAS-101"] == SourceType.JIRA_STORY
    assert types["QAS-103"] == SourceType.JIRA_IMPROVEMENT
    assert types["QAS-104"] == SourceType.JIRA_OUT_OF_SCOPE
    assert types["QAS-105"] == SourceType.JIRA_INCIDENT  # encontrada por enlace y por texto
    assert "QAS-900" not in types
    story = next(r for r in collection.issues if r.source_id == "QAS-102")
    assert "monto máximo" in story.fields["acceptance_criteria"]
    assert collection.epic.fields["components"] == ["Backoffice Devoluciones", "Portal Contribuyentes"]
    assert any(c.source_id == "QAS-101#comment-30001" for c in collection.comments)
    assert [p.source_id for p in collection.confluence_pages] == ["confluence:9001"]


def test_confluence_unavailable_is_a_limitation(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), None))
    collection = reader.collect("QAS-100")
    assert collection.confluence_pages == []
    assert any("Confluence no disponible" in x for x in collection.limitations)


def test_failed_child_query_is_recorded_and_run_continues(profile):
    profile.atlassian.epic_child_jql = ["parent = {epic_key}", "unsupportedfield = {epic_key}"]
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    collection = reader.collect("QAS-100")
    assert any("Consulta de hijas no disponible" in x for x in collection.limitations)
    assert any(r.source_id == "QAS-101" for r in collection.issues)


# ------------------------------------------------------------------ solo lectura


def test_read_only_guard_blocks_write_tools(profile):
    gateway = FixtureGateway(load_jira(), load_confluence())
    guard = ReadOnlyGuard(gateway, [*profile.atlassian.mcp.read_tools, "jira_create_issue"])
    with pytest.raises(WriteAttemptBlocked):
        guard.call("jira_create_issue", {"summary": "x"})
    with pytest.raises(WriteAttemptBlocked):
        guard.call("jira_update_issue", {})
    assert gateway.write_calls == []  # nunca llegó al servidor
    assert guard.write_calls == 2
    assert "jira_create_issue" not in guard.available_tools()


@pytest.mark.parametrize("tool,expected", [
    ("jira_search", False), ("jira_get_issue", False), ("jira_get_link_types", False),
    ("confluence_get_page", False), ("jira_create_issue", True), ("jira_update_issue", True),
    ("jira_create_issue_link", True), ("jira_add_comment", True), ("jira_delete_issue", True),
    ("jira_transition_issue", True), ("jira_link_to_epic", True), ("confluence_update_page", True),
])
def test_looks_like_write(tool, expected):
    assert looks_like_write(tool) is expected


def test_gateway_error_on_unknown_issue(profile):
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    with pytest.raises(GatewayError):
        reader.get_issue("QAS-999")


# ------------------------------------------------------------------ Test Plan de referencia


def test_reference_test_plan_relations_are_discovered(profile):
    """El Test Plan de ejemplo define las relaciones: Tarea hija de la épica, Tests subtareas, Relates a historias."""
    reader, guard = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    collection = reader.collect("QAS-100")
    mapping = collection.reference_mapping
    assert mapping["test_plan"]["parent"] == "QAS-100" and mapping["test_plan"]["parent_is_epic"]
    assert mapping["test_plan"]["components"] == ["Backoffice Devoluciones", "Portal Contribuyentes"]
    assert mapping["tests"]["parent_is_test_plan"] and mapping["tests"]["links_to_test_plan"] == 0
    assert mapping["tests"]["story_link_types"] == {"Relates": 1}
    assert mapping["recommended_profile"] == {
        "test_plan_issue_type": "Tarea", "test_case_issue_type": "Test", "epic_association": "parent",
        "test_plan_association": "parent", "story_link_type": "Relates"}
    assert mapping["profile_mismatches"] == []
    assert guard.write_calls == 0  # solo lectura


def test_profile_mismatch_with_reference_is_reported(profile):
    profile.publication.test_plan_association = "link"
    profile.publication.technical_labels = True
    reader, _ = reader_for(profile, FixtureGateway(load_jira(), load_confluence()))
    collection = reader.collect("QAS-100")
    assert any("test_plan_association" in x for x in collection.reference_mapping["profile_mismatches"])
    assert any("difiere del Test Plan de referencia QAS-200" in x for x in collection.limitations)


def test_profile_without_technical_labels_requires_parent_relations(profile):
    from pydantic import ValidationError

    from qa_regresion.config import PublicationCfg

    with pytest.raises(ValidationError):
        PublicationCfg(technical_labels=False, test_plan_association="link")
    assert PublicationCfg(technical_labels=False, epic_association="parent", test_plan_association="parent")

"""Publicación (solo con mocks): idempotencia, recuperación parcial, enlaces y componentes."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from conftest import SIM_COMMAND, make_harness
from qa_regresion.domain.enums import PublicationClassification, WorkflowPhase
from qa_regresion.publishers.jira_publisher import epic_label
from qa_regresion.workflows.regression_workflow import load_run, mark_draft_presented, publish_run

EPIC_COMPONENTS = ["Backoffice Devoluciones", "Portal Contribuyentes"]


class FakeJira:
    """Jira en memoria que implementa la interfaz JiraWriter."""

    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.links: list[tuple[str, str, str]] = []
        self.attachments: dict[str, list[str]] = {}
        self.created = 0
        self.updated: list[str] = []
        self.fail_after: int | None = None
        self.queries: list[str] = []
        self._n = 500

    def find_issues(self, jql: str) -> list[dict[str, Any]]:
        self.queries.append(jql)
        label = re.search(r'labels = "([^"]+)"', jql)
        parent = re.search(r"parent = ([A-Z]+-\d+)", jql)
        itype = re.search(r'issuetype = "([^"]+)"', jql).group(1)
        out = []
        for key, issue in self.issues.items():
            if issue["issue_type"] != itype:
                continue
            if (label and label.group(1) in issue["labels"]) or (parent and issue.get("parent") == parent.group(1)):
                links = [{"type": {"name": t}, "outward_issue": {"key": b if a == key else a}}
                         for t, a, b in self.links if key in (a, b)]
                out.append({**issue, "key": key, "issuelinks": links})
        return out

    def create_issue(self, fields: dict[str, Any]) -> str:
        if self.fail_after is not None and self.created >= self.fail_after:
            raise TimeoutError("timeout simulado de Jira")
        self._n += 1
        key = f"{fields['project']}-{self._n}"
        self.issues[key] = dict(fields)
        self.created += 1
        return key

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        self.issues[key].update(fields)
        self.updated.append(key)

    def link_issues(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self.links.append((link_type, inward_key, outward_key))

    def attach_files(self, key: str, paths: list[str]) -> None:
        self.attachments.setdefault(key, []).extend(paths)


@pytest.fixture
def enabled_draft(workspace):
    harness = make_harness(workspace)
    harness.profile.publication.enabled = True
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-pub")
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    return bundle, harness


def approve(out, profile, jira):
    state, _ = load_run(out)
    mark_draft_presented(state, out, profile)
    time.sleep(0.02)  # el mensaje del usuario siempre llega después de mostrar el borrador
    return publish_run(out, "Publicalo", profile, writer_factory=lambda: jira)


def published_tests(jira, profile):
    return {k: v for k, v in jira.issues.items() if v["issue_type"] == profile.publication.test_case_issue_type}


def test_publication_creates_task_and_individual_tests(enabled_draft):
    bundle, harness = enabled_draft
    jira = FakeJira()
    result = approve(bundle.output_dir, harness.profile, jira)
    assert result.phase == WorkflowPhase.PUBLISHED, result.error
    plans = [i for i in jira.issues.values() if i["issue_type"] == "Tarea"]
    assert len(plans) == 1
    assert plans[0]["summary"] == "Test Plan - Regresión | Portal de Devoluciones"
    assert plans[0]["parent"] == "QAS-100"  # relacionada con la épica
    assert "Identificador de ejecución: r-pub" in plans[0]["description"]
    tests = published_tests(jira, harness.profile)
    assert len(tests) == len(bundle.preview.tests)
    for issue in [*plans, *tests.values()]:
        assert sorted(issue["components"]) == EPIC_COMPONENTS  # mismos componentes que la épica
        assert issue["labels"] == ["QA"]  # como el Test Plan de referencia
    assert all(re.search(r"TC-\d{3}", t["summary"]) for t in tests.values())
    assert all("```gherkin" in t["description"] for t in tests.values())
    plan_key = next(k for k, v in jira.issues.items() if v["issue_type"] == "Tarea")
    assert jira.attachments[plan_key]
    state, _ = load_run(bundle.output_dir)
    assert state.phase == WorkflowPhase.PUBLISHED and state.approval.consumed


def test_relations_follow_reference_test_plan(enabled_draft):
    """Modelo del Test Plan de referencia: Tarea hija de la épica, Tests subtareas, Relates a cada historia."""
    bundle, harness = enabled_draft
    jira = FakeJira()
    approve(bundle.output_dir, harness.profile, jira)
    plan_key = next(k for k, v in jira.issues.items() if v["issue_type"] == "Tarea")
    assert jira.issues[plan_key]["parent"] == "QAS-100"
    tc_to_key = {re.search(r"TC-\d{3}", v["summary"]).group(0): k for k, v in published_tests(jira, harness.profile).items()}
    for test in bundle.preview.tests:
        key = tc_to_key[test.tc_id]
        assert jira.issues[key]["parent"] == plan_key  # Test → Test Plan como subtarea
        assert not [l for l in jira.links if plan_key in l[1:] and key in l[1:]]  # sin enlace plan↔test
        for story in test.story_links:
            assert (story.link_type, key, story.target) == ("Relates", key, story.target)
            assert ("Relates", key, story.target) in jira.links  # Test → cada historia cubierta
    story_links = [l for l in jira.links if l[2].startswith("QAS-10")]
    assert len(story_links) == sum(len(t.story_links) for t in bundle.preview.tests)
    assert all('parent = ' in q for q in jira.queries)  # lo existente se busca por relaciones, no por etiquetas


def test_link_mode_with_technical_labels(workspace):
    harness = make_harness(workspace)
    pub = harness.profile.publication
    pub.enabled, pub.test_plan_association, pub.technical_labels = True, "link", True
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-link")
    jira = FakeJira()
    approve(bundle.output_dir, harness.profile, jira)
    plan_key = next(k for k, v in jira.issues.items() if v["issue_type"] == "Tarea")
    tc_to_key = {re.search(r"TC-\d{3}", v["summary"]).group(0): k for k, v in published_tests(jira, harness.profile).items()}
    for test in bundle.preview.tests:
        assert ("Relates", plan_key, tc_to_key[test.tc_id]) in jira.links  # Test Plan → Test por enlace
        assert epic_label("QAS-100") in jira.issues[tc_to_key[test.tc_id]]["labels"]


def test_republish_is_idempotent(enabled_draft):
    bundle, harness = enabled_draft
    jira = FakeJira()
    approve(bundle.output_dir, harness.profile, jira)
    total, links = len(jira.issues), len(jira.links)
    (bundle.output_dir / "publication-journal.json").unlink()  # aun sin diario, se busca en Jira
    state, _ = load_run(bundle.output_dir)
    state.phase = WorkflowPhase.AWAITING_PUBLICATION_APPROVAL  # simula una nueva corrida con igual contenido
    from qa_regresion.workflows.artifacts import ArtifactWriter
    ArtifactWriter(harness.profile).save_state(state, bundle.output_dir)
    result = approve(bundle.output_dir, harness.profile, jira)
    assert result.phase == WorkflowPhase.PUBLISHED
    assert len(jira.issues) == total and result.created == []
    assert len(jira.links) == links
    classes = {i.classification for i in result.plan.items if i.kind == "test"}
    assert classes == {PublicationClassification.UNCHANGED}


def test_partial_publication_resumes_without_duplicates(enabled_draft):
    bundle, harness = enabled_draft
    jira = FakeJira()
    jira.fail_after = 3
    first = approve(bundle.output_dir, harness.profile, jira)
    assert first.phase == WorkflowPhase.PARTIALLY_PUBLISHED and "timeout" in first.error
    assert jira.created == 3
    state, _ = load_run(bundle.output_dir)
    assert state.phase == WorkflowPhase.PARTIALLY_PUBLISHED
    # La aprobación anterior ya se usó: hay que volver a mostrar el borrador y recibir 'Publicalo'.
    with pytest.raises(Exception):
        publish_run(bundle.output_dir, "Publicalo", harness.profile, writer_factory=lambda: jira)
    jira.fail_after = None
    second = approve(bundle.output_dir, harness.profile, jira)
    assert second.phase == WorkflowPhase.PUBLISHED, second.error
    assert len(jira.issues) == 1 + len(bundle.preview.tests)
    summaries = [v["summary"] for v in jira.issues.values()]
    assert len(summaries) == len(set(summaries))


def test_modified_and_potentially_obsolete_classification(enabled_draft):
    bundle, harness = enabled_draft
    jira = FakeJira()
    approve(bundle.output_dir, harness.profile, jira)
    tests = published_tests(jira, harness.profile)
    first_key = sorted(tests)[0]
    jira.issues[first_key]["description"] = "editado a mano en Jira"
    plan_key = next(k for k, v in jira.issues.items() if v["issue_type"] == "Tarea")
    jira.issues["QAS-999"] = {"project": "QAS", "summary": "TC-999 | caso viejo", "issue_type": "Test",
                              "description": "", "components": EPIC_COMPONENTS, "labels": ["QA"],
                              "parent": plan_key}
    # Un Test de otro Test Plan (p. ej. el manual previo) no se considera ni se toca.
    jira.issues["QAS-998"] = {"project": "QAS", "summary": "TC-998 | de otro plan", "issue_type": "Test",
                              "description": "", "components": EPIC_COMPONENTS, "labels": ["QA"],
                              "parent": "QAS-200"}
    state, _ = load_run(bundle.output_dir)
    state.phase = WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    from qa_regresion.workflows.artifacts import ArtifactWriter
    ArtifactWriter(harness.profile).save_state(state, bundle.output_dir)
    result = approve(bundle.output_dir, harness.profile, jira)
    by_ref = {i.ref: i for i in result.plan.items}
    assert by_ref["TC-999"].classification == PublicationClassification.POTENTIALLY_OBSOLETE
    assert "TC-998" not in by_ref
    assert "QAS-999" in jira.issues  # nunca se elimina ni se cierra
    assert first_key in jira.updated
    modified = [i for i in result.plan.items if i.existing_key == first_key][0]
    assert modified.classification in (PublicationClassification.MODIFIED, PublicationClassification.PENDING)


def test_blocked_cases_are_not_published(enabled_draft):
    bundle, harness = enabled_draft
    from qa_regresion.publishers.jira_publisher import JiraPublisher, PublicationJournal

    preview = bundle.preview.model_copy(update={"not_published": [{"tc_id": "TC-500", "status": "blocked",
                                                                   "reason": "evidencia inaccesible"}]})
    publisher = JiraPublisher(FakeJira(), harness.profile, PublicationJournal.load(bundle.output_dir / "j.json"))
    plan = publisher.plan(preview)
    blocked = [i for i in plan.items if i.ref == "TC-500"][0]
    assert blocked.classification == PublicationClassification.BLOCKED and blocked.action.value == "report-only"
    pending = [i for i in plan.items if i.classification == PublicationClassification.PENDING]
    assert pending  # los casos pendientes de negocio se clasifican aparte


def test_dry_run_never_builds_a_writer(workspace):
    harness = make_harness(workspace)
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-dry")
    assert bundle.state.dry_run and bundle.state.remote_write_calls == 0
    assert not (bundle.output_dir / "publication-journal.json").exists()
    assert harness.gateway.write_calls == []


# ------------------------------------------------------------------ piloto y escritor MCP


def test_pilot_preview_limits_tests_and_covers_distinct_stories(workspace):
    harness = make_harness(workspace)
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="r-pilot", pilot_size=3)
    preview = bundle.preview
    assert len(preview.tests) == 3
    stories = [l.target for t in preview.tests for l in t.story_links]
    assert len(set(stories)) == 3  # un caso por historia distinta
    excluded = [x for x in preview.not_published if x["status"] == "fuera-del-piloto"]
    assert len(excluded) == len([c for c in bundle.design.cases if c.status.value != "blocked"]) - 3
    assert "Piloto de publicación: 3 de" in preview.test_plan.description
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL


def test_pilot_then_full_publication_does_not_duplicate(workspace):
    harness = make_harness(workspace)
    harness.profile.publication.enabled = True
    pilot = harness.orchestrator.run(SIM_COMMAND, run_id="r-p1", pilot_size=3)
    jira = FakeJira()
    first = approve(pilot.output_dir, harness.profile, jira)
    assert first.phase == WorkflowPhase.PUBLISHED, first.error
    assert len(jira.issues) == 1 + 3
    assert not [i for i in first.plan.items if i.classification == PublicationClassification.BLOCKED]

    full = make_harness(workspace, profile=harness.profile).orchestrator.run(SIM_COMMAND, run_id="r-p2")
    second = approve(full.output_dir, harness.profile, jira)
    assert second.phase == WorkflowPhase.PUBLISHED, second.error
    assert len(jira.issues) == 1 + len(full.preview.tests)  # misma Tarea, sin Tests duplicados
    by_class = {i.ref: i.classification for i in second.plan.items}
    assert by_class["test-plan"] == PublicationClassification.MODIFIED  # se actualiza la lista de casos
    for test in pilot.preview.tests:
        assert by_class[test.tc_id] == PublicationClassification.UNCHANGED
    summaries = [v["summary"] for v in jira.issues.values()]
    assert len(summaries) == len(set(summaries))


def test_descriptions_are_markdown(simulated):
    bundle, _ = simulated
    test = bundle.preview.tests[0]
    assert "```gherkin" in test.description and "{code" not in test.description
    assert bundle.preview.test_plan.description.startswith("## Test Plan de regresión")
    assert "h2." not in bundle.preview.test_plan.description


class RecordingGateway:
    def __init__(self, attachment_results=None):
        self.calls = []
        self.attachment_results = attachment_results

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "jira_create_issue":
            return {"message": "ok", "issue": {"key": "QAS-777"}}
        if tool == "jira_update_issue" and "attachments" in arguments:
            return {"issue": {"key": arguments["issue_key"], "attachment_results": self.attachment_results}}
        return {}


def test_mcp_writer_uses_tool_contract(profile):
    import json as _json

    from qa_regresion.publishers.jira_publisher import McpJiraWriter

    gateway = RecordingGateway()
    writer = McpJiraWriter(gateway, profile)
    key = writer.create_issue({"project": "QAS", "summary": "TC-001 | x", "issue_type": "Test", "description": "d",
                               "components": EPIC_COMPONENTS, "labels": ["QA"], "parent": "QAS-500"})
    assert key == "QAS-777"
    _, args = gateway.calls[-1]
    assert args["components"] == "Backoffice Devoluciones,Portal Contribuyentes"
    assert _json.loads(args["additional_fields"]) == {"labels": ["QA"], "parent": "QAS-500"}
    writer.update_issue("QAS-777", {"summary": "s", "description": "d", "components": EPIC_COMPONENTS,
                                    "labels": ["QA"]})
    _, args = gateway.calls[-1]
    assert args["components"] == "Backoffice Devoluciones,Portal Contribuyentes"
    assert "components" not in _json.loads(args["fields"])
    writer.link_issues("Relates", "QAS-777", "QAS-101")
    assert gateway.calls[-1] == ("jira_create_issue_link", {"link_type": "Relates", "inward_issue_key": "QAS-777",
                                                            "outward_issue_key": "QAS-101"})


def test_mcp_writer_rejects_unconfirmed_attachments(profile):
    from qa_regresion.publishers.jira_publisher import McpJiraWriter

    ok = RecordingGateway({"success": True, "uploaded": [{"filename": "a"}], "failed": []})
    McpJiraWriter(ok, profile).attach_files("QAS-1", ["a"])
    for results in (None, {"success": True, "uploaded": [], "failed": [{"filename": "a", "error": "fuera de CWD"}]}):
        with pytest.raises(RuntimeError):
            McpJiraWriter(RecordingGateway(results), profile).attach_files("QAS-1", ["a"])


def test_epic_fields_are_inherited_by_task_and_tests(enabled_draft):
    bundle, harness = enabled_draft
    assert bundle.state.epic_fields["customfield_19999"]["value"] == "Gerencia Simulada"
    preview = bundle.preview
    expected = {"customfield_19999": [{"value": "Gerencia Simulada"}]}
    assert preview.inherited_fields == {"Area Solicitante": "Gerencia Simulada"}
    assert preview.test_plan.extra_fields == expected
    assert all(t.extra_fields == expected for t in preview.tests)
    jira = FakeJira()
    approve(bundle.output_dir, harness.profile, jira)
    assert all(issue["extra_fields"] == expected for issue in jira.issues.values())
    review = (bundle.output_dir / "borrador-casos-a-publicar.md").read_text(encoding="utf-8")
    assert "Area Solicitante: Gerencia Simulada (heredado de la épica)" in review


def test_mcp_writer_sends_inherited_fields(profile):
    import json as _json

    from qa_regresion.publishers.jira_publisher import McpJiraWriter

    gateway = RecordingGateway()
    extra = {"customfield_19999": [{"value": "Gerencia Simulada"}]}
    McpJiraWriter(gateway, profile).create_issue({"project": "QAS", "summary": "s", "issue_type": "Tarea",
                                                  "labels": ["QA"], "extra_fields": extra, "parent": "QAS-100"})
    assert _json.loads(gateway.calls[-1][1]["additional_fields"]) == {"labels": ["QA"], **extra, "parent": "QAS-100"}
    McpJiraWriter(gateway, profile).update_issue("QAS-1", {"summary": "s", "extra_fields": extra})
    assert _json.loads(gateway.calls[-1][1]["additional_fields"]) == extra


def test_internal_ids_are_code_so_jira_does_not_autolink(simulated):
    bundle, _ = simulated
    description = bundle.preview.tests[0].description
    body = description.split("```gherkin")[0] + description.split("```")[-1]
    assert "`TD-" in body and "`FR-" in body
    import re as _re
    assert not _re.search(r"(?<!`)\b(?:TD|FR)-\d{3}\b(?!`)", body)

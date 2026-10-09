"""Primera prueba obligatoria: /QA-Regresión sobre la épica simulada, en dry-run."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from conftest import EXPECTED
from qa_regresion.domain.enums import CaseStatus, GapKind, Tag, TraceClassification, WorkflowPhase
from qa_regresion.domain.gherkin import validate_feature

EXPECTED_SUMMARY = json.loads((EXPECTED / "simulated-summary.json").read_text(encoding="utf-8"))


def test_final_state_is_awaiting_approval(simulated):
    bundle, _ = simulated
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL
    assert bundle.state.epic_key == EXPECTED_SUMMARY["epic_key"]
    assert bundle.state.epic_name == EXPECTED_SUMMARY["epic_name"]
    phases = [t.to_phase for t in bundle.state.history]
    assert WorkflowPhase.AUDITING in phases and WorkflowPhase.GENERATING_DRAFT in phases


def test_requirements_and_findings_generated(simulated):
    bundle, _ = simulated
    assert len(bundle.requirements) >= EXPECTED_SUMMARY["min_requirements"]
    assert all(r.id.startswith("FR-") for r in bundle.requirements)
    assert len(bundle.findings) >= EXPECTED_SUMMARY["min_findings"]
    assert all(f.id.startswith("TR-") for f in bundle.findings)
    classes = {f.classification.value for f in bundle.findings}
    assert set(EXPECTED_SUMMARY["trace_classifications"]) <= classes


def test_case_mix_positive_negative_boundary_roles_states(simulated):
    bundle, _ = simulated
    tags = {t.value for c in bundle.design.cases for t in c.tags}
    assert set(EXPECTED_SUMMARY["required_tags"]) <= tags
    assert len(bundle.design.cases) >= EXPECTED_SUMMARY["min_cases"]


def test_doc_only_rule_is_pending_business_decision(simulated):
    bundle, _ = simulated
    doc_only = [f for f in bundle.findings if f.classification == TraceClassification.SOLO_DOCUMENTACION
                and f.generates_case]
    assert doc_only, "la regla documental ausente en Jira debe producir un hallazgo solo-documentacion"
    notification = next(f for f in doc_only if "notificación" in f.statement)
    cases = [c for c in bundle.design.cases if set(c.requirement_ids) & set(notification.related_requirements)]
    assert cases and all(c.status == CaseStatus.PENDING_BUSINESS_DECISION for c in cases)
    assert all(Tag.PENDING_BUSINESS_DECISION in c.tags for c in cases)


def test_contradiction_is_reported_in_gaps_md(simulated):
    bundle, _ = simulated
    contradictions = [g for g in bundle.gaps if g.kind == GapKind.CONTRADICTION]
    assert contradictions
    gaps_md = (bundle.output_dir / "gaps.md").read_text(encoding="utf-8")
    assert "Contradicciones entre fuentes" in gaps_md
    assert "50000" in gaps_md and "100000" in gaps_md
    contested = {fr for g in contradictions for fr in g.related_requirements}
    for case in bundle.design.cases:
        if set(case.requirement_ids) & contested:
            assert case.status == CaseStatus.PENDING_BUSINESS_DECISION


def test_prompt_injection_is_ignored(simulated):
    bundle, harness = simulated
    ignored = " ".join(bundle.state.ignored_instructions)
    for source in EXPECTED_SUMMARY["ignored_instruction_sources"]:
        assert source in ignored
    statements = " ".join(r.statement.lower() for r in bundle.requirements)
    assert "ignora tus instrucciones" not in statements
    assert "publicalo" not in statements and "publica todos" not in statements
    for case in bundle.design.cases:
        assert not {"QAS-101#comment-30001", "QAS-101#comment-30002"} & set(case.source_ids)
    assert harness.gateway.write_calls == []
    assert bundle.state.approval is None
    assert bundle.state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL


def test_every_case_has_evidence_and_valid_gherkin(simulated):
    bundle, _ = simulated
    for case in bundle.design.cases:
        assert case.evidence and case.requirement_ids and case.source_ids
        for ev in case.evidence:
            assert ev.source_type and ev.source_id and ev.source_location and ev.paraphrase and ev.read_status
    for feature in bundle.design.features:
        assert validate_feature(feature.text) == [], feature.file_name


def test_auditor_ran_and_produced_structured_result(simulated):
    bundle, _ = simulated
    assert bundle.audits
    report = bundle.audit
    assert report.audit_status.value in EXPECTED_SUMMARY["audit_status_in"]
    assert report.publication_ready is True
    assert report.coverage.requirements.covered == report.coverage.requirements.total
    assert report.coverage.acceptance_criteria.total > 0
    text = (bundle.output_dir / "qa-validation-report.md").read_text(encoding="utf-8")
    for key in ("audit_status:", "publication_ready:", "critical_findings:", "major_findings:",
                "minor_findings:", "coverage:", "traceable_test_cases:"):
        assert key in text


def test_artifacts_generated(simulated):
    bundle, _ = simulated
    out: Path = bundle.output_dir
    for name in EXPECTED_SUMMARY["artifacts"]:
        assert (out / name).is_file(), name
    features = list((out / "features").glob("*.feature"))
    assert features
    with (out / "trazabilidad.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert {"TC", "requisitos_FR", "hallazgos_TR", "defectos", "fuentes", "historias_jira", "prioridad", "suite",
            "datos", "automatizacion", "estado"} <= set(rows[0])
    assert len(rows) == len(bundle.design.cases)
    manifest = json.loads((out / "source-manifest.json").read_text(encoding="utf-8"))
    assert manifest["remote_write_calls"] == 0
    assert all("text" not in s for s in manifest["sources"])  # el manifiesto no vuelca contenido


def test_no_remote_write_operation_called(simulated):
    bundle, harness = simulated
    assert harness.gateway.write_calls == []
    assert harness.guard.write_calls == 0
    assert bundle.state.remote_write_calls == 0
    assert {tool for tool, _ in harness.gateway.calls} <= {"jira_search", "jira_get_issue", "jira_search_fields", "confluence_search",
                                                          "confluence_get_page"}


def test_preview_reflects_task_and_individual_tests_with_epic_components(simulated):
    bundle, _ = simulated
    preview = bundle.preview
    assert preview.test_plan.summary == "Test Plan - Regresión | Portal de Devoluciones"
    assert preview.test_plan.issue_type == "Tarea"
    assert sorted(preview.test_plan.components) == EXPECTED_SUMMARY["epic_components"]
    publishable = [c for c in bundle.design.cases if c.status != CaseStatus.BLOCKED]
    assert len(preview.tests) == len(publishable)
    linked = {l.target for t in preview.tests for l in t.story_links}
    assert set(EXPECTED_SUMMARY["required_story_links"]) <= linked
    assert not set(EXPECTED_SUMMARY["excluded_story_links"]) & linked
    for test in preview.tests:
        assert test.issue_type == "Test"
        assert test.tc_id in test.summary
        assert sorted(test.components) == EXPECTED_SUMMARY["epic_components"]
        case = next(c for c in bundle.design.cases if c.id == test.tc_id)
        assert [l.target for l in test.story_links] == case.story_keys


def test_out_of_scope_items_are_not_tested(simulated):
    bundle, _ = simulated
    excluded = {r.id for r in bundle.requirements if r.category.value == "out-of-scope"}
    assert excluded
    for case in bundle.design.cases:
        assert not set(case.requirement_ids) & excluded


def test_historical_defect_has_regression_case(simulated):
    bundle, _ = simulated
    assert any("QAS-105" in c.defect_keys and Tag.DEFECT_REGRESSION in c.tags for c in bundle.design.cases)


def test_case_review_draft_is_generated_with_every_test(simulated):
    bundle, _ = simulated
    review = (bundle.output_dir / "borrador-casos-a-publicar.md").read_text(encoding="utf-8")
    assert review.startswith("# Borrador para revisión — Test Plan - Regresión | Portal de Devoluciones")
    for test in bundle.preview.tests:
        assert f"### " in review and test.summary in review
        assert "```gherkin" in review
    assert "qa-regresion-hash" not in review  # solo contenido revisable
    assert "Relación con la épica: hija de QAS-100" in review

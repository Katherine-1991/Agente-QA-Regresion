from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from qa_regresion.adapters.atlassian_mcp import AtlassianReader, ReadOnlyGuard
from qa_regresion.adapters.fixture_gateway import FixtureGateway
from qa_regresion.agents.orchestrator import QALeadOrchestrator, RunBundle
from qa_regresion.config import ProjectProfile, load_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "simulated"
EXPECTED = ROOT / "tests" / "expected"
SIM_PROFILE = ROOT / "config" / "simulated-profile.yaml"
SIM_COMMAND = "/QA-Regresión Portal de Devoluciones"


def load_jira() -> dict[str, Any]:
    return json.loads((FIXTURES / "jira.json").read_text(encoding="utf-8"))


def load_confluence() -> dict[str, Any]:
    return json.loads((FIXTURES / "confluence.json").read_text(encoding="utf-8"))


def make_workspace(root: Path) -> Path:
    target = root / ".simulacion" / "Formularios"
    shutil.copytree(FIXTURES / "Formularios", target, dirs_exist_ok=True)
    return root


@dataclass
class Harness:
    orchestrator: QALeadOrchestrator
    guard: ReadOnlyGuard
    gateway: FixtureGateway
    profile: ProjectProfile
    root: Path


def make_harness(root: Path, profile: ProjectProfile | None = None, jira: dict | None = None,
                 confluence: dict | None | str = "default", **orchestrator_kwargs: Any) -> Harness:
    profile = profile or load_profile(SIM_PROFILE)
    conf = load_confluence() if confluence == "default" else confluence
    gateway = FixtureGateway(copy.deepcopy(jira or load_jira()), conf)
    guard = ReadOnlyGuard(gateway, profile.atlassian.mcp.read_tools)
    reader = AtlassianReader(guard, profile, sleep=lambda _s: None)
    orchestrator = QALeadOrchestrator(profile, root, reader, read_guard=guard, **orchestrator_kwargs)
    return Harness(orchestrator, guard, gateway, profile, root)


@pytest.fixture
def profile() -> ProjectProfile:
    return load_profile(SIM_PROFILE)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return make_workspace(tmp_path)


@pytest.fixture(scope="session")
def simulated(tmp_path_factory: pytest.TempPathFactory) -> tuple[RunBundle, Harness]:
    """Una única corrida simulada completa, compartida por las pruebas de integración."""
    root = make_workspace(tmp_path_factory.mktemp("sim"))
    harness = make_harness(root)
    bundle = harness.orchestrator.run(SIM_COMMAND, run_id="run-test-sim")
    return bundle, harness

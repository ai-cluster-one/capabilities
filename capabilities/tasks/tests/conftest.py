"""Every test starts with no project bound by the environment.

A process that resolves a project's id hands it to what it starts through
CAPABILITIES_PROJECT_ID and CAPABILITIES_PROJECT_ID_ROOT, and a folder bound to
no id of its own resolves to the folder that hand-down names. In one pytest
process an earlier test's hand-down would otherwise bind the next test."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402


@pytest.fixture(autouse=True)
def _no_inherited_project_binding(monkeypatch):
    for key in ("CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _on_the_registered_node(monkeypatch):
    """Every test runs as the node `_cli.register` puts in each store, so the
    node a run records is one the store holds."""
    monkeypatch.setenv("AGENT_NODE_ID", _cli.TEST_NODE)

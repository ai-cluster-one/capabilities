"""Every test starts with no project bound by the environment.

A process that resolves a project's id hands it to what it starts through
CAPABILITIES_PROJECT_ID and CAPABILITIES_PROJECT_ID_ROOT, and a folder bound to
no id of its own resolves to the folder that hand-down names. In one pytest
process an earlier test's hand-down would otherwise bind the next test."""

import pytest


@pytest.fixture(autouse=True)
def _no_inherited_project_binding(monkeypatch):
    for key in ("CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT"):
        monkeypatch.delenv(key, raising=False)

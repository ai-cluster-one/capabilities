#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0"]
# ///
"""Which store a process resolves, and how a turn is handed its daemon's.

A command resolves the store for its project: the project's .env.local / .env,
then the process environment, then the machine's store setting. A turn the
service starts, and everything a turn starts under its raise, resolves only the
environment level, which whoever started it filled with the store the raise
lives in. Nothing here reaches a store: the settings are resolved and compared.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract==0.4.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

PROJECT_URL = "postgresql://project@127.0.0.1:5432/project_db?sslmode=disable"
PROCESS_URL = "postgresql://process@127.0.0.1:5432/process_db?sslmode=disable"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose .env names one store while the process environment names
    another, and no machine setting."""
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".env").write_text(f"AGENTKIT_DB_URL={PROJECT_URL}\nAGENTKIT_DB_SCHEMA=from_file\n"
                               "PROJECT_ONLY=1\n")
    for key in (*_cli.STORE_OVERRIDES, "TASKS_EXECUTION", mod._RECEIPT_ENV, mod._EXCLUDE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("AGENTKIT_DB_URL", PROCESS_URL)
    return root


def test_a_command_resolves_its_projects_env_files_first(project):
    setting = mod._store_setting()
    assert (setting.level, setting.url, setting.schema) == ("project", PROJECT_URL, "from_file")


@pytest.mark.parametrize("bound", ["receipt", "execution"])
def test_a_turn_and_what_it_starts_resolve_only_what_they_were_handed(project, monkeypatch,
                                                                      bound):
    if bound == "receipt":
        monkeypatch.setenv(mod._RECEIPT_ENV, str(project / "claim.json"))
    else:
        monkeypatch.setenv("TASKS_EXECUTION", "00000000-0000-0000-0000-000000000001")
    setting = mod._store_setting()
    assert (setting.level, setting.url, setting.schema) == (
        "environment", PROCESS_URL, "agentkit")


def test_the_service_hands_a_turn_its_store_and_nothing_else(project):
    resolved = mod._store_setting()
    start = {"PATH": "/bin", "AGENTKIT_DB_HOST": "stale.example", "AGENTKIT_DB_URL": "stale",
             "TASKS_EXECUTION": "old", "CLAUDE_PROJECT_DIR": str(project)}
    env = mod._ServiceHost.turn_env(SimpleNamespace(setting=resolved), start)
    assert {key: value for key, value in env.items() if key.startswith("AGENTKIT_DB_")} == {
        "AGENTKIT_DB_URL": PROJECT_URL, "AGENTKIT_DB_SCHEMA": "from_file"}
    assert "TASKS_EXECUTION" not in env and "CLAUDE_PROJECT_DIR" not in env
    # The turn, carrying its receipt, resolves exactly what it was handed.
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env, **{mod._RECEIPT_ENV: str(project / "claim.json")})
        handed = mod._store_setting()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert (handed.url, handed.schema) == (resolved.url, resolved.schema)


def test_separate_fields_are_handed_as_separate_fields():
    from capabilities_contract import db

    setting = db.Setting(schema="s1", level="machine", host="db.internal", port=6543,
                         database="d1", user="u1", sslmode="verify-full", password="pw")
    assert mod._setting_env(setting) == {
        "AGENTKIT_DB_SCHEMA": "s1", "AGENTKIT_DB_HOST": "db.internal",
        "AGENTKIT_DB_PORT": "6543", "AGENTKIT_DB_NAME": "d1", "AGENTKIT_DB_USER": "u1",
        "AGENTKIT_DB_SSLMODE": "verify-full", "AGENTKIT_DB_PASSWORD": "pw"}


def test_a_turn_reads_its_projects_env_files_but_not_their_store(project, monkeypatch):
    monkeypatch.setenv(mod._RECEIPT_ENV, str(project / "claim.json"))
    monkeypatch.delenv("PROJECT_ONLY", raising=False)
    mod._turn_reads_project_env()
    assert os.environ["PROJECT_ONLY"] == "1"
    assert os.environ["AGENTKIT_DB_URL"] == PROCESS_URL
    assert "AGENTKIT_DB_SCHEMA" not in os.environ

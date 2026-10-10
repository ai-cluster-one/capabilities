#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0",
#                 "callva-harness-runner==0.8.0", "pyyaml>=6"]
# ///
"""A project's workers kept in the store as versions.

A save inserts a version and moves `active` to it, guarded on the version the
writer read; an identical save creates none; a project reads and writes only
its own; the document is judged whole before it is saved; worker files are
refused until `workers import` moves them in; and every raise records the
version it was claimed under. The verbs are driven as a consuming project
drives them, through the CLI, against a store in TASKS_TEST_DSN, and the
checks skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.4.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "claude"

PROFILE = f"""harness = "claude"
model = "claude-opus-5"
timeout_seconds = 600
permission_mode = "bypassPermissions"
cli_path = "{FAKE_CLAUDE}"
"""

ALPHA = """---
description: Takes alpha.
takes: [alpha]
profile: plain
---

Do the alpha work.
"""


# --- What a document is, with no store ----------------------------------------

def test_a_document_that_does_not_parse_is_refused_before_anything_is_saved():
    for text in ("no front matter", "---\ntakes: [a\n---\nbody", "---\n- a list\n---\nbody"):
        with pytest.raises(mod.Refusal) as refused:
            mod._parse_worker(text, "doc")
        assert refused.value.code == "worker_invalid"


def test_a_document_is_judged_whole_and_an_unknown_key_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    spec, body = mod._parse_worker(ALPHA.replace("takes:", "taks: [x]\ntakes:"), "doc")
    with pytest.raises(mod.Refusal) as refused:
        mod._judged("alpha", spec, body, "doc")
    assert "'taks', which nothing reads" in refused.value.message
    # A profile this machine lacks is only a warning: another machine may have it.
    spec, body = mod._parse_worker(ALPHA, "doc")
    assert any("profile 'plain'" in one for one in mod._judged("alpha", spec, body, "doc"))


def test_a_turn_cannot_save_remove_or_import_a_worker(monkeypatch, capsys):
    monkeypatch.setenv("TASKS_EXECUTION", str(uuid.uuid4()))
    for args in (["save", "alpha", "-"], ["remove", "alpha"], ["import", "--apply"]):
        with pytest.raises(SystemExit) as stopped:
            mod.cmd_workers({"allow_write": True}, args)
        assert stopped.value.code == 4
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "policy"


# --- Through the CLI, against a real store -----------------------------------

def _project(root: Path, project_id: str) -> Path:
    (root / ".git").mkdir(parents=True)
    envelope = root / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "profiles").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local", "connections": {"local": {"allow_write": True}}}))
    (tasks / "profiles" / "plain.toml").write_text(PROFILE)
    return root


@pytest.fixture()
def lab(tmp_path):
    import psycopg
    schema = "tasks_test_" + secrets.token_hex(4)
    here = "prj_" + uuid.uuid4().hex[:12]
    there = "prj_" + uuid.uuid4().hex[:12]
    _cli.write_store_setting(tmp_path / "config", schema)
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH)})
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                   "CLAUDE_PROJECT_DIR", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    lab = {"here": _project(tmp_path / "here", here), "there": _project(tmp_path / "there", there),
           "ids": {"here": here, "there": there}, "env": env, "schema": schema, "tmp": tmp_path}
    assert cli(lab, "migrate").returncode == 0
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def cli(lab, *args: str, where: str = "here", stdin: str | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    project = lab[where]
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=project,
                          env={**lab["env"], "CLAUDE_PROJECT_DIR": str(project),
                               "CAPABILITIES_PROJECT_ENVELOPE": str(project / "capabilities"),
                               **(env or {})},
                          input=stdin, text=True, capture_output=True, timeout=180)


def ok(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def refused(proc: subprocess.CompletedProcess, exit_code: int) -> dict:
    assert proc.returncode == exit_code, proc.stdout + proc.stderr
    return json.loads(proc.stderr.strip().splitlines()[-1])["error"]


def rows(lab, name: str) -> list[tuple]:
    import psycopg
    with psycopg.connect(DSN) as conn:
        return conn.execute(f"""select project_id, active, id::text from {lab['schema']}.tasks_workers
                                 where name = %s order by created_at, id""", (name,)).fetchall()


def save(lab, name: str, text: str, *extra: str, where: str = "here"):
    return cli(lab, "workers", "save", name, "-", *extra, where=where, stdin=text)


@needs_store
def test_a_save_is_a_version_and_an_identical_save_creates_none(lab):
    first = ok(save(lab, "alpha", ALPHA))
    assert first["created"] is True and first["active"] is True
    again = ok(save(lab, "alpha", ALPHA + "\n\n"))
    assert again["created"] is False and again["version"] == first["version"]
    second = ok(save(lab, "alpha", ALPHA.replace("Do the alpha work.", "Do it better.")))
    assert second["created"] is True and second["version"] != first["version"]
    stored = rows(lab, "alpha")
    assert [(active, version) for _p, active, version in stored] == [
        (False, first["version"]), (True, second["version"])]
    history = ok(cli(lab, "workers", "history", "alpha"))["versions"]
    assert [one["version"] for one in history] == [first["version"], second["version"]]
    shown = ok(cli(lab, "workers", "show", "alpha"))
    assert shown["version"] == second["version"] and shown["body"] == "Do it better."
    assert shown["spec"] == {"description": "Takes alpha.", "takes": ["alpha"], "profile": "plain"}
    # `text` is the document a save takes back, unchanged.
    assert ok(save(lab, "alpha", shown["text"]))["created"] is False
    older = ok(cli(lab, "workers", "show", "alpha", "--version", first["version"]))
    assert older["active"] is False and older["body"] == "Do the alpha work."


@needs_store
def test_a_save_over_a_version_already_replaced_is_refused(lab):
    first = ok(save(lab, "alpha", ALPHA))
    second = ok(save(lab, "alpha", ALPHA + "More.\n", "--base", first["version"]))
    error = refused(save(lab, "alpha", ALPHA + "Other.\n", "--base", first["version"]), 6)
    assert error["code"] == "conflict" and second["version"] in error["message"]
    assert [active for _p, active, _v in rows(lab, "alpha")] == [False, True]
    error = refused(cli(lab, "workers", "remove", "alpha", "--base", first["version"]), 6)
    assert error["code"] == "conflict"


@needs_store
def test_a_worker_the_capability_would_refuse_is_never_saved(lab):
    error = refused(save(lab, "alpha", ALPHA.replace("profile: plain", "profile: plain\nfence: x")), 6)
    assert error["code"] == "worker_invalid" and "'fence', which nothing reads" in error["message"]
    assert refused(save(lab, "alpha", "not a worker"), 6)["code"] == "worker_invalid"
    assert refused(save(lab, "Not-A-Name", ALPHA), 6)["code"] == "input"
    assert rows(lab, "alpha") == []


@needs_store
def test_a_project_reads_and_writes_only_its_own_workers(lab):
    mine = ok(save(lab, "alpha", ALPHA))
    theirs = ok(save(lab, "alpha", ALPHA + "Theirs.\n", where="there"))
    assert theirs["created"] is True and theirs["version"] != mine["version"]
    assert sorted((p, a) for p, a, _v in rows(lab, "alpha")) == sorted(
        [(lab["ids"]["here"], True), (lab["ids"]["there"], True)])
    assert ok(cli(lab, "workers", "show", "alpha"))["version"] == mine["version"]
    assert ok(cli(lab, "workers", "remove", "alpha", where="there"))["removed"] == theirs["version"]
    assert ok(cli(lab, "workers", "show", "alpha"))["version"] == mine["version"]
    listed = ok(cli(lab, "workers", "list", where="there"))["workers"]
    assert [one["source"] for one in listed if one["worker"] == "alpha"] == []


@needs_store
def test_removing_a_worker_keeps_its_history_and_brings_the_shipped_one_back(lab):
    off = ok(save(lab, "default", "---\nenabled: false\n---\n"))
    listed = {one["worker"]: one for one in ok(cli(lab, "workers", "list"))["workers"]}
    assert listed["default"]["source"] == "store" and listed["default"]["enabled"] is False
    assert listed["default"]["shadows"].endswith("workers/default.md")
    assert ok(cli(lab, "workers", "remove", "default"))["removed"] == off["version"]
    listed = {one["worker"]: one for one in ok(cli(lab, "workers", "list"))["workers"]}
    assert listed["default"]["source"] == "shipped"
    [kept] = ok(cli(lab, "workers", "history", "default"))["versions"]
    assert kept["version"] == off["version"] and kept["active"] is False
    assert refused(cli(lab, "workers", "remove", "default"), 3)["code"] == "not_found"
    shipped = ok(cli(lab, "workers", "show", "supervisor"))
    assert shipped["source"] == "shipped" and shipped["spec"]["enabled"] is False


@needs_store
def test_worker_files_are_refused_until_imported_and_the_import_moves_them(lab):
    folder = lab["here"] / "capabilities" / "tasks" / "workers"
    folder.mkdir()
    (folder / "alpha.md").write_text(ALPHA)
    (folder / "default.md").write_text("---\nenabled: false\n---\n")
    for args in (("doctor",), ("run", "alpha")):
        error = refused(cli(lab, *args), 6)
        assert error["code"] == "workers_not_imported" and "alpha.md" in error["message"]
    dry = ok(cli(lab, "workers", "import"))
    assert dry["applied"] is False
    assert {one["worker"]: one["outcome"] for one in dry["workers"]} == {
        "alpha": "would_be_imported", "default": "would_be_imported"}
    assert (folder / "alpha.md").is_file() and rows(lab, "alpha") == []
    done = ok(cli(lab, "workers", "import", "--apply"))
    assert {one["worker"]: one["outcome"] for one in done["workers"]} == {
        "alpha": "imported", "default": "imported"}
    assert not folder.exists()
    doctor = ok(cli(lab, "doctor"))
    rows_by = {one["worker"]: one for one in doctor["workers"]}
    assert rows_by["alpha"]["source"] == "store" and rows_by["alpha"]["ok"] is True
    assert rows_by["alpha"]["version"] == ok(cli(lab, "workers", "show", "alpha"))["version"]


@needs_store
def test_the_import_never_saves_a_file_over_a_different_version(lab):
    held = ok(save(lab, "alpha", ALPHA))
    folder = lab["here"] / "capabilities" / "tasks" / "workers"
    folder.mkdir()
    (folder / "alpha.md").write_text(ALPHA.replace("alpha work", "stale work"))
    (folder / "beta.md").write_text(ALPHA.replace("[alpha]", "[beta]"))
    error = refused(cli(lab, "workers", "import", "--apply"), 6)
    assert "differs from version" in error["message"] and held["version"] in error["message"]
    # All or none: beta waits with alpha, and nothing moved.
    assert (folder / "alpha.md").is_file() and (folder / "beta.md").is_file()
    assert rows(lab, "beta") == [] and len(rows(lab, "alpha")) == 1
    # The same document the store holds - another machine's copy - is already there.
    (folder / "alpha.md").write_text(ALPHA)
    done = ok(cli(lab, "workers", "import", "--apply"))
    assert {one["worker"]: one["outcome"] for one in done["workers"]} == {
        "alpha": "already", "beta": "imported"}
    assert len(rows(lab, "alpha")) == 1 and not folder.exists()


@needs_store
def test_a_raise_records_the_worker_version_it_was_claimed_under(lab):
    ok(save(lab, "default", "---\nenabled: false\n---\n"))
    first = ok(save(lab, "alpha", ALPHA))
    ok(cli(lab, "add", "--type", "alpha", "--title", "one", "--key", "t-1", "--status", "todo"))
    ran = ok(cli(lab, "run", "alpha", "--apply"))
    assert ran["claimed"]
    [raised] = ok(cli(lab, "runs", "t-1"))["executions"]
    assert raised["worker"] == "alpha" and raised["worker_id"] == first["version"]
    second = ok(save(lab, "alpha", ALPHA + "Now differently.\n"))
    ok(cli(lab, "add", "--type", "alpha", "--title", "two", "--key", "t-2", "--status", "todo"))
    ok(cli(lab, "run", "alpha", "--apply"))
    [raised] = ok(cli(lab, "runs", "t-2"))["executions"]
    assert raised["worker_id"] == second["version"]
    # A claim by hand names no stored worker and records none.
    ok(cli(lab, "add", "--type", "alpha", "--title", "three", "--key", "t-3", "--status", "todo"))
    held = ok(cli(lab, "claim", "--key", "t-3", "--worker", "alpha"))
    assert held["execution"]["worker_id"] is None


@needs_store
def test_the_service_fingerprint_moves_with_the_active_version(lab, monkeypatch):
    project = lab["here"]
    monkeypatch.setattr(mod, "_project_root", lambda: project)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(lab["tmp"] / "config"))
    _cli.bind_store(mod, monkeypatch, lab["schema"])
    monkeypatch.setattr(mod, "PROJECT", lab["ids"]["here"])
    before = mod._service_fingerprint()
    ok(save(lab, "alpha", ALPHA))
    saved = mod._service_fingerprint()
    assert saved != before
    ok(save(lab, "alpha", ALPHA))
    assert mod._service_fingerprint() == saved
    ok(save(lab, "alpha", ALPHA + "Changed.\n"))
    assert mod._service_fingerprint() not in (before, saved)


@needs_store
def test_the_service_runs_the_stored_workers_as_its_lanes(lab):
    ok(save(lab, "default", "---\nenabled: false\n---\n"))
    ok(save(lab, "alpha", ALPHA))
    assert ok(cli(lab, "service", "init"))["written"]
    doctor = ok(cli(lab, "service", "doctor"))
    assert doctor["lanes"] == [{"worker": "alpha", "max_parallel": 1}]
    assert {one["worker"] for one in doctor["idle"]} == {"default", "supervisor"}

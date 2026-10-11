#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.1",
#                 "callva-harness-runner==0.9.0", "pyyaml>=6"]
# ///
"""A project's harness profiles kept in the store as versions.

The table and every rule of the versions are callva-harness-runner's: a save
inserts a version and moves `active` to it, guarded on the version the writer
read; an identical save creates none; a name resolves to the project's
version, then the global one, then the shipped profile. What tasks adds is
checked here: a project writes only its own project-scope profiles and the
global ones, nothing under a raise writes any, the file is judged whole before
it is saved, profile files are refused until `profiles import` moves them in,
and every raise records the profile version it ran on. The verbs are driven as
a consuming project drives them, through the CLI, against a store in
TASKS_TEST_DSN, and the checks skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.4.1' \\
        --with 'callva-harness-runner==0.9.0' python -m pytest capabilities/tasks/tests -q
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

PLAIN = f"""harness = "claude"
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


# --- What a profile file is, with no store ------------------------------------

def test_a_profile_file_is_judged_whole_before_anything_is_saved():
    for text, words in (('harness = "claude"\nmodle = "x"\n', "unknown key 'modle'"),
                        ('model = "x"\n', "no top-level `harness`"),
                        ('harness = "claude"\napproval_policy = "never"\n', "codex knob"),
                        ('harness = "claude"\nmodel = "${M}"\n', "carries `${`"),
                        ("harness = ", "not valid TOML")):
        with pytest.raises(mod.Refusal) as refused:
            mod._parse_profile(text, "plain", "plain.toml")
        assert refused.value.code == "profile_invalid" and words in refused.value.message
    assert mod._parse_profile(PLAIN, "plain", "plain.toml")["model"] == "claude-opus-5"


def test_a_turn_cannot_save_remove_or_import_a_profile(monkeypatch, capsys):
    monkeypatch.setenv("TASKS_EXECUTION", str(uuid.uuid4()))
    for args in (["save", "plain", "-"], ["remove", "plain"], ["remove", "plain", "--global"],
                 ["import", "--apply"]):
        with pytest.raises(SystemExit) as stopped:
            mod.cmd_profiles({"allow_write": True}, args)
        assert stopped.value.code == 4
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "policy"


# --- Through the CLI, against a real store -----------------------------------

def _project(root: Path, project_id: str) -> Path:
    (root / ".git").mkdir(parents=True)
    envelope = root / "capabilities"
    tasks = envelope / "tasks"
    tasks.mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local", "connections": {"local": {"allow_write": True}}}))
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
        return conn.execute(f"""select scope, scope_id, active, id::text
                                  from {lab['schema']}.harness_profiles
                                 where name = %s order by created_at, id""", (name,)).fetchall()


def save(lab, name: str, text: str, *extra: str, where: str = "here"):
    return cli(lab, "profiles", "save", name, "-", *extra, where=where, stdin=text)


@needs_store
def test_the_librarys_table_is_migrated_first_under_its_own_ledger(lab):
    import psycopg
    with psycopg.connect(DSN) as conn:
        ledger = conn.execute(f"""select owner, step from {lab['schema']}.schema_ledger
                                   order by applied_at, owner, step""").fetchall()
    assert ("harness", "0001-profiles") in ledger and ("tasks", "0004-profiles") in ledger
    assert ledger.index(("harness", "0001-profiles")) < ledger.index(("tasks", "0004-profiles"))
    doctor = ok(cli(lab, "doctor"))
    assert doctor["profile_tables"]["owner"] == "harness"
    assert doctor["profile_tables"]["state"] == "current"
    assert doctor["tables"]["code"] == {"major": 1, "minor": 3}


@needs_store
def test_a_save_is_a_version_and_an_identical_save_creates_none(lab):
    first = ok(save(lab, "plain", PLAIN))
    assert first["created"] is True and first["active"] is True
    assert first["scope"] == "project" and first["harness"] == "claude"
    again = ok(save(lab, "plain", "# a comment changes nothing\n" + PLAIN))
    assert again["created"] is False and again["version"] == first["version"]
    second = ok(save(lab, "plain", PLAIN.replace("600", "900")))
    assert second["created"] is True and second["version"] != first["version"]
    assert [(scope, active, version) for scope, _s, active, version in rows(lab, "plain")] == [
        ("project", False, first["version"]), ("project", True, second["version"])]
    history = ok(cli(lab, "profiles", "history", "plain"))["versions"]
    assert [one["version"] for one in history] == [first["version"], second["version"]]
    shown = ok(cli(lab, "profiles", "show", "plain"))
    assert (shown["layer"], shown["version"]) == ("project", second["version"])
    assert shown["spec"]["timeout_seconds"] == 900
    # `text` is the file a save takes back, unchanged.
    assert ok(save(lab, "plain", shown["text"]))["created"] is False
    older = ok(cli(lab, "profiles", "show", "plain", "--version", first["version"]))
    assert older["active"] is False and older["spec"]["timeout_seconds"] == 600


@needs_store
def test_a_save_over_a_version_already_replaced_is_refused(lab):
    first = ok(save(lab, "plain", PLAIN))
    second = ok(save(lab, "plain", PLAIN.replace("600", "900"), "--base", first["version"]))
    error = refused(save(lab, "plain", PLAIN.replace("600", "60"), "--base",
                         first["version"]), 6)
    assert error["code"] == "conflict" and second["version"] in error["message"]
    assert [active for _sc, _s, active, _v in rows(lab, "plain")] == [False, True]
    error = refused(cli(lab, "profiles", "remove", "plain", "--base", first["version"]), 6)
    assert error["code"] == "conflict"


@needs_store
def test_a_profile_the_library_would_refuse_is_never_saved(lab):
    error = refused(save(lab, "plain", PLAIN + 'fence = "act"\n'), 6)
    assert error["code"] == "profile_invalid" and "'fence' was removed" in error["message"]
    error = refused(save(lab, "plain", PLAIN + 'modle = "x"\n'), 6)
    assert error["code"] == "profile_invalid" and "unknown key 'modle'" in error["message"]
    error = refused(save(lab, "plain", PLAIN.replace("claude-opus-5", "${MODEL}")), 6)
    assert error["code"] == "profile_invalid" and "carries `${`" in error["message"]
    assert refused(save(lab, "../plain", PLAIN), 6)["code"] == "input"
    assert rows(lab, "plain") == []


@needs_store
def test_a_project_reads_and_writes_only_its_own_project_profiles(lab):
    mine = ok(save(lab, "plain", PLAIN))
    theirs = ok(save(lab, "plain", PLAIN.replace("600", "900"), where="there"))
    assert theirs["version"] != mine["version"]
    assert sorted((scope_id, active) for _sc, scope_id, active, _v in rows(lab, "plain")) == \
        sorted([(lab["ids"]["here"], True), (lab["ids"]["there"], True)])
    assert ok(cli(lab, "profiles", "show", "plain"))["version"] == mine["version"]
    assert ok(cli(lab, "profiles", "remove", "plain", where="there"))["removed"] == \
        theirs["version"]
    assert ok(cli(lab, "profiles", "show", "plain"))["version"] == mine["version"]
    # Another project's version is not this project's to read.
    error = refused(cli(lab, "profiles", "show", "plain", "--version", theirs["version"]), 3)
    assert error["code"] == "not_found"
    listed = {one["profile"]: one for one in ok(cli(lab, "profiles", "list",
                                                    where="there"))["profiles"]}
    assert "plain" not in listed and listed["claude-act"]["layer"] == "shipped"


@needs_store
def test_a_name_resolves_to_the_project_then_the_global_then_the_shipped_profile(lab):
    everyone = ok(save(lab, "claude-act", PLAIN.replace("claude-opus-5", "everyone"),
                       "--global"))
    assert everyone["scope"] == "global"
    mine = ok(save(lab, "claude-act", PLAIN.replace("claude-opus-5", "mine")))
    shown = ok(cli(lab, "profiles", "show", "claude-act"))
    assert (shown["layer"], shown["version"], shown["shadows"]) == (
        "project", mine["version"], ["global", "shipped"])
    assert ok(cli(lab, "profiles", "show", "claude-act", where="there"))["version"] == \
        everyone["version"]
    assert ok(cli(lab, "profiles", "show", "claude-act", "--global"))["spec"]["model"] == \
        "everyone"
    listed = {one["profile"]: one for one in ok(cli(lab, "profiles", "list"))["profiles"]}
    assert (listed["claude-act"]["layer"], listed["claude-act"]["version"]) == (
        "project", mine["version"])
    ok(cli(lab, "profiles", "remove", "claude-act"))
    shown = ok(cli(lab, "profiles", "show", "claude-act"))
    assert (shown["layer"], shown["version"]) == ("global", everyone["version"])
    ok(cli(lab, "profiles", "remove", "claude-act", "--global"))
    shown = ok(cli(lab, "profiles", "show", "claude-act"))
    assert (shown["layer"], shown["version"]) == ("shipped", None)
    assert shown["text"].startswith("# Profile")
    history = ok(cli(lab, "profiles", "history", "claude-act", "--global"))["versions"]
    assert [one["active"] for one in history] == [False]


@needs_store
def test_profile_files_are_refused_until_imported_and_the_import_moves_them(lab):
    folder = lab["here"] / "capabilities" / "tasks" / "profiles"
    folder.mkdir()
    (folder / "plain.toml").write_text(PLAIN)
    (folder / "slow.toml").write_text(PLAIN.replace("600", "3600"))
    ok(cli(lab, "workers", "save", "alpha", "-", stdin=ALPHA))
    for args in (("doctor",), ("run", "alpha")):
        error = refused(cli(lab, *args), 6)
        assert error["code"] == "profiles_not_imported" and "plain.toml" in error["message"]
        assert "profiles import --apply" in error.get("hint", "")
    dry = ok(cli(lab, "profiles", "import"))
    assert dry["applied"] is False
    assert {one["profile"]: one["outcome"] for one in dry["profiles"]} == {
        "plain": "would_be_imported", "slow": "would_be_imported"}
    assert (folder / "plain.toml").is_file() and rows(lab, "plain") == []
    listed = ok(cli(lab, "profiles", "list"))
    assert listed["unimported"] and "no longer read" in listed["warning"]
    done = ok(cli(lab, "profiles", "import", "--apply"))
    assert {one["profile"]: one["outcome"] for one in done["profiles"]} == {
        "plain": "imported", "slow": "imported"}
    assert not folder.exists() and len(done["removed"]) == 2
    doctor = ok(cli(lab, "doctor"))
    [alpha] = [one for one in doctor["workers"] if one["worker"] == "alpha"]
    assert alpha["ok"] is True and alpha["profile_source"]["layer"] == "project"
    assert alpha["profile_source"]["version"] == ok(cli(lab, "profiles", "show",
                                                        "plain"))["version"]


@needs_store
def test_the_import_never_saves_a_file_over_a_different_version(lab):
    held = ok(save(lab, "plain", PLAIN))
    folder = lab["here"] / "capabilities" / "tasks" / "profiles"
    folder.mkdir()
    (folder / "plain.toml").write_text(PLAIN.replace("600", "900"))
    (folder / "slow.toml").write_text(PLAIN.replace("600", "3600"))
    error = refused(cli(lab, "profiles", "import", "--apply"), 6)
    assert "differs from version" in error["message"] and held["version"] in error["message"]
    # All or none: slow waits with plain, and nothing moved.
    assert (folder / "plain.toml").is_file() and (folder / "slow.toml").is_file()
    assert rows(lab, "slow") == [] and len(rows(lab, "plain")) == 1
    # A file that is no profile refuses the import too, naming it.
    (folder / "bad.toml").write_text('harness = "claude"\nmodle = "x"\n')
    (folder / "plain.toml").write_text(PLAIN)
    error = refused(cli(lab, "profiles", "import", "--apply"), 6)
    assert "unknown key 'modle'" in error["message"] and rows(lab, "slow") == []
    (folder / "bad.toml").unlink()
    # The same profile the store holds - another machine's copy - is already there.
    done = ok(cli(lab, "profiles", "import", "--apply"))
    assert {one["profile"]: one["outcome"] for one in done["profiles"]} == {
        "plain": "already", "slow": "imported"}
    assert len(rows(lab, "plain")) == 1 and not folder.exists()


@needs_store
def test_a_raise_records_the_profile_version_it_ran_on(lab):
    ok(cli(lab, "workers", "save", "default", "-", stdin="---\nenabled: false\n---\n"))
    ok(cli(lab, "workers", "save", "alpha", "-", stdin=ALPHA))
    first = ok(save(lab, "plain", PLAIN))
    ok(cli(lab, "add", "--type", "alpha", "--title", "one", "--key", "t-1", "--status", "todo"))
    assert ok(cli(lab, "run", "alpha", "--apply"))["claimed"]
    [raised] = ok(cli(lab, "runs", "t-1"))["executions"]
    assert raised["worker"] == "alpha" and raised["profile_id"] == first["version"]
    assert raised["worker_id"]
    # The project's version gone, the global one is what the next raise runs on.
    everyone = ok(save(lab, "plain", PLAIN.replace("600", "900"), "--global"))
    ok(cli(lab, "profiles", "remove", "plain"))
    ok(cli(lab, "add", "--type", "alpha", "--title", "two", "--key", "t-2", "--status", "todo"))
    ok(cli(lab, "run", "alpha", "--apply"))
    [raised] = ok(cli(lab, "runs", "t-2"))["executions"]
    assert raised["profile_id"] == everyone["version"]
    # A claim by hand names no profile and records none.
    ok(cli(lab, "add", "--type", "alpha", "--title", "three", "--key", "t-3", "--status", "todo"))
    held = ok(cli(lab, "claim", "--key", "t-3", "--worker", "alpha"))
    assert held["execution"]["profile_id"] is None


@needs_store
def test_the_service_fingerprint_moves_with_the_version_a_profile_resolves_to(lab, monkeypatch):
    project = lab["here"]
    monkeypatch.setattr(mod, "_project_root", lambda: project)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(lab["tmp"] / "config"))
    _cli.bind_store(mod, monkeypatch, lab["schema"])
    monkeypatch.setattr(mod, "PROJECT", lab["ids"]["here"])
    ok(cli(lab, "workers", "save", "alpha", "-", stdin=ALPHA))
    before = mod._service_fingerprint()
    ok(save(lab, "plain", PLAIN))
    saved = mod._service_fingerprint()
    assert saved != before
    ok(save(lab, "plain", PLAIN))
    assert mod._service_fingerprint() == saved
    ok(save(lab, "plain", PLAIN.replace("600", "900"), "--global"))
    # A global version under the project's own changes nothing it resolves to.
    assert mod._service_fingerprint() == saved
    ok(cli(lab, "profiles", "remove", "plain"))
    assert mod._service_fingerprint() not in (before, saved)

"""An agent whose records moved into the store runs with no record or settings
file at all.

One machine is built for the module, as `test_manager_migrate` builds one: a
config home, a registry of this checkout's capabilities, a throwaway
PostgreSQL holding the one store, and an agent carrying every kind of file the
transition moves - the gate, connections, identifiers, every service's
settings, automations' `[agents]`, askproject's profiles folder, tasks'
conveyor and slack's allow-list. The global rows are written, the agent is
migrated, and from then on every reader and writer of those records and
settings is exercised with no such file on disk and every retired environment
override set to a path that does not exist. A second agent stays in files, the
way an agent not yet moved does, to show the same code still reads its files.

Set CAPABILITIES_TEST_PG_PORT to the loopback port of a running cluster
(superuser `pgadmin`, trust) to reuse it; otherwise the module starts its own.
The module skips without `initdb`, `pg_ctl`, git or psycopg.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from test_manager_migrate import (MASTER, MANAGER, REPO, SCHEMA, World, _say,  # noqa: F401
                                  answers_file, gate)


NAMES = ("agentrooms", "askproject", "automations", "coolify", "deployment", "memory",
         "slack", "tasks", "telegram", "whatsapp")
IDS = {"moved": "prj_dbonly00001", "files": "prj_dbonly00002", "fresh": "prj_dbonly00003"}
# Every environment override that once named a record or settings file, each
# set to a path that is not there.
GARBAGE = {key: "/nonexistent/garbage-override" for key in (
    "AUTOMATIONS_CONFIG", "TELEGRAM_SERVICE_SETTINGS", "SLACK_SERVICE_SETTINGS",
    "TELEGRAM_SERVICE_CONNECTIONS_FILE", "TG_CONNECTIONS_FILE", "TELEGRAM_CONNECTIONS_FILE")}
PROFILE = ('harness = "claude"\nmodel = "opus"\npermission_mode = "bypassPermissions"\n'
           'timeout_seconds = 900\n')
AGENTS = (
    'version = 1\n\n[engine]\ntick_seconds = 1.0\nmax_parallel = 3\n'
    'shutdown_grace_seconds = 5\n\n'
    '[agents]\ndefault = "researcher"\n\n'
    '[agents.workers.researcher]\nengine = "claude"\nmodel = "opus"\neffort = "high"\n'
    'mode = "write"\ntimeout_seconds = 3000\n\n'
    '[agents.workers.operator]\nengine = "claude"\nmodel = "opus"\neffort = "high"\n'
    'mode = "act"\ntimeout_seconds = 2700\n\n'
    '[agents.workers.terra]\nengine = "codex"\nmodel = "gpt-x-terra"\neffort = "high"\n'
    'mode = "read"\nservice_tier = "priority"\ntimeout_seconds = 900\n\n'
    '[agents.workers.sonnet]\neffort = "low"\n')
WANTED_AGENTS = {
    "researcher": {"engine": "claude", "model": "opus", "effort": "high", "mode": "write",
                   "timeout_seconds": 3000.0, "service_tier": None},
    "operator": {"engine": "claude", "model": "opus", "effort": "high", "mode": "act",
                 "timeout_seconds": 2700.0, "service_tier": None},
    "terra": {"engine": "codex", "model": "gpt-x-terra", "effort": "high", "mode": "read",
              "timeout_seconds": 900.0, "service_tier": "priority"},
    "sonnet": {"engine": "claude", "model": "sonnet", "effort": "low", "mode": "read",
               "timeout_seconds": 600.0, "service_tier": None},
}
# An agent-box runtime as `deployment init --profile agent-box` writes it.
RUNTIME = {
    "schema": "capabilities.deployment.runtime.v1", "name": "dbonly", "profile": "agent-box",
    "capabilities": {}, "image_env": "AGENT_IMAGE", "optional_env": [], "required_env": [],
    "compiler": {"build_args": {}, "compose_overlays": [],
                 "container": {"agent_home": "/home/agent", "project_root": "/app"}},
    "service_policy": {"auto_include": True, "capabilities": {}, "default_mode": "embedded"},
    "services": {"agent": {
        "compose_service": "agent", "description": "The agent box.",
        "optional_env": ["AGENT_IMAGE"], "required_env": [], "role": "agent-runtime",
        "state": ["claude_state", "codex_state"]}},
    "volumes": {"claude_state": {"description": "Claude state.", "mount": "/home/agent/.claude"},
                "codex_state": {"description": "Codex state.", "mount": "/home/agent/.codex"}},
}
RECORD_FILES = ("settings.json", "connections.json", "identifiers.json", "config.toml",
                "conveyor.toml", "policy.json")


class Machine(World):
    """The migration test's machine, on a cluster given or one of its own."""

    def files_left(self, root: Path) -> list[str]:
        """Every record or settings file left under the agent's envelope."""
        envelope = root / "capabilities"
        found = [p for p in envelope.rglob("*") if p.is_file() and (
            p.name in RECORD_FILES and (p.parent == envelope or p.parent.parent == envelope
                                        or p.parent.name == "service")
            or p.parent.name in ("profiles", "workers"))]
        return sorted(str(p.relative_to(root)) for p in found)


@pytest.fixture(scope="module")
def world():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="capabilities-dbonly-")).resolve()
    found = Machine(tmp)
    try:
        found.start()
        for name in NAMES:
            found.install(found.registry, name)
        build(found)
        yield found
    finally:
        found.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def build(w: Machine) -> None:
    w.write(w.config / "capabilities" / "settings.json", gate("memory"))
    files = {
        "{env}/settings.json": gate(*[n for n in NAMES if n != "coolify"]),
        "{env}/automations/service/config.toml": AGENTS,
        "{env}/askproject/profiles/peer-read.toml": PROFILE,
        "{env}/tasks/service/config.toml": "version = 1\npoll_seconds = 30\n",
        "{env}/tasks/conveyor.toml": 'escalate_to = ["Owner"]\nstall_after = "6h"\n',
        "{env}/tasks/connections.json": {"default": "local",
                                         "connections": {"local": {"allow_write": True}}},
        "{env}/slack/policy.json": {"allowed_users": {"U01": {}},
                                    "allowed_channels": {"C01": {}}},
        "{env}/slack/connections.json": {"default": "workspace",
                                         "connections": {"workspace": {"allow_write": False}}},
        "{env}/memory/service/settings.json": {"version": 1},
        "{env}/coolify/identifiers.json": {"app": {"value": "app-uuid", "note": ""}},
        "{env}/telegram/connections.json": {
            "default": "main", "connections": {"main": {"session": "main",
                                                        "api_id_env": "TG_API_ID",
                                                        "api_hash_env": "TG_API_HASH"}}},
        "{env}/telegram/service/settings.json": {
            "connection": "main", "assistant_name": "Relay",
            "direct_messages": {"mode": "allowlist", "default_role": "direct_user"},
            "allowed_users": {"1001": {"name": "Owner", "role": "supervisor"}},
            "control": {"roles": {"supervisor": {"commands": ["*"]}, "direct_user": {}}}},
        "{env}/whatsapp/connections.json": {"connections": {"clinic": {
            "engine": "whatsmeow", "number": "15551234567"}}},
        "{env}/whatsapp/service/settings.json": {"connection": "clinic",
                                                 "dialogue": {"profile": "clinic-claude"}},
        "{env}/whatsapp/service/profiles/clinic-claude.toml": PROFILE,
        "{env}/agentrooms/connections.json": {"connections": {"bot": {
            "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
            "token_env": "BOT_TOKEN"}}},
        "{env}/agentrooms/service/settings.json": {
            "version": 1, "agents": {"bot": {"profile": "claude-act", "turn_timeout": 600,
                                             "history": 20}}},
        "deployment/runtime.json": RUNTIME,
        "deployment/targets/production/target.json": {
            "schema": "capabilities.deployment.target.v1", "name": "production",
            "environment": "production", "provider": "manual", "connection": None,
            "node": "srv-dbonly",
            "resource": {"compose_file": "deployment/targets/production/docker-compose.yaml"}},
        ".env.local": "SLACK_BOT_TOKEN=xoxb-test\nSLACK_APP_TOKEN=xapp-test\n"
                      "TG_API_ID=1\nTG_API_HASH=x\nWAHA_API_KEY=x\n",
    }
    for name in ("moved", "files"):
        w.agent(name, IDS[name], files, ignore=(".env.local",))
    w.agent("fresh", IDS["fresh"], {"{env}/settings.json": gate(
        "automations", "memory", "tasks", "slack")})
    for name in ("tasks", "automations", "memory", "agentrooms", "whatsapp", "telegram"):
        result = w.cap(w.roots["moved"], name, "migrate")
        assert result.returncode == 0, (name, result.stderr)
    w.agentkit_list()
    w.answer(w.manager(w.roots["moved"], "migrate", "--global", "--apply"))
    answers = {"moved": answers_file(w, "moved", {"nodes": {"srv-dbonly": {"register": True}}}),
               "fresh": answers_file(w, "fresh", {})}
    for name in ("moved", "fresh"):
        applied = w.answer(w.manager(w.roots[name], "migrate", "--apply", "--answers",
                                     answers[name]))
        assert applied["state"] == "migrated", applied
        assert not applied["verify"]["differences"], applied["verify"]


def run(w: Machine, name: str, capability: str, *args: str, **extra):
    """One command of a capability in an agent, every retired override set."""
    return w.cap(w.roots[name], capability, *args, **{**GARBAGE, **extra})


# --- 1. no file is read or written for a moved agent --------------------------------------

def test_a_moved_agent_keeps_no_record_or_settings_file(world):
    _say("moved-files-left", world.files_left(world.roots["moved"]))
    assert world.files_left(world.roots["moved"]) == []
    assert json.loads((world.roots["moved"] / "capabilities" / "project.json").read_text())[
        "records"] == "store"
    # The agent not moved keeps every one of them.
    assert "capabilities/tasks/conveyor.toml" in world.files_left(world.roots["files"])


def test_automations_reads_and_writes_its_settings_as_rows(world):
    w = world
    out = {}
    out["agents"] = w.answer(run(w, "moved", "automations", "agents"))
    out["declaration"] = w.answer(run(w, "moved", "automations", "service", "declaration"))
    out["init"] = w.answer(run(w, "moved", "automations", "service", "init"))
    out["status"] = w.answer(run(w, "moved", "automations", "service", "status"))
    out["doctor"] = w.answer(run(w, "moved", "automations", "doctor"))
    out["inventory"] = w.answer(run(w, "moved", "automations", "inventory"))
    out["list"] = w.answer(run(w, "moved", "automations", "list"))
    _say("automations-moved", out)
    assert out["status"]["initialized"] is True
    assert out["status"]["config"].startswith("store:")
    # The settings document is there already; only the example script, a
    # file of the project's own, is written.
    assert [p for p in out["init"]["written"] if not p.endswith("example.py")] == []
    assert out["init"]["skipped"][0].startswith("store:")
    assert out["doctor"]["ok"] is True
    assert out["inventory"]["service"]["ok"] is True
    assert out["declaration"]["engine"]["max_parallel"] == 3
    assert "workers" not in out["declaration"].get("agents", {}) or \
        set(out["declaration"]["agents"]["workers"]) == set(WANTED_AGENTS)
    stderr = run(w, "moved", "automations", "agents").stderr
    assert "AUTOMATIONS_CONFIG is ignored" in stderr
    assert w.files_left(w.roots["moved"]) == []


def test_automations_agent_profiles_are_harness_profiles(world):
    w = world
    rows = dict(w.sql("SELECT name, spec FROM harness_profiles WHERE scope = 'project' "
                      "AND scope_id = %s AND active AND name LIKE 'automations-%%'",
                      (IDS["moved"],)))
    _say("automations-harness-profiles", rows)
    assert set(rows) == {f"automations-{n}" for n in WANTED_AGENTS}
    assert rows["automations-researcher"]["permission_mode"] == "acceptEdits"
    assert rows["automations-operator"]["permission_mode"] == "bypassPermissions"
    assert rows["automations-terra"]["codex_config"] == {"sandbox_mode": "read-only"}
    assert rows["automations-terra"]["service_tier"] == "priority"
    assert rows["automations-sonnet"]["tools"] == ["Read", "Glob", "Grep"]
    # The default stays a key of the settings document: a profile row has no
    # place for it.
    assert w.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                 "capability = 'automations' AND path = '{agents,default}'",
                 (IDS["moved"],)) == [("researcher",)]
    moved = w.answer(run(w, "moved", "automations", "agents"))
    files = w.answer(w.cap(w.roots["files"], "automations", "agents"))
    _say("automations-agents", {"moved": moved, "files": files})
    assert moved == files
    assert moved["default"] == "researcher"
    for name, profile in WANTED_AGENTS.items():
        assert moved["workers"][name] == profile, name
    # A profile saved in the store later is the one a run resolves.
    w.sql("UPDATE harness_profiles SET spec = jsonb_set(spec, '{model}', '\"haiku\"') "
          "WHERE scope_id = %s AND name = 'automations-terra' AND active", (IDS["moved"],))
    assert w.answer(run(w, "moved", "automations", "agents"))["workers"]["terra"]["model"] \
        == "haiku"
    w.sql("UPDATE harness_profiles SET spec = jsonb_set(spec, '{model}', '\"gpt-x-terra\"') "
          "WHERE scope_id = %s AND name = 'automations-terra' AND active", (IDS["moved"],))


def test_the_automations_daemon_starts_and_reloads_without_a_config_file(world):
    w = world
    root = w.roots["moved"]
    started = w.answer(run(w, "moved", "automations", "service", "start"))
    try:
        _say("automations-daemon-started", started)
        assert started["running"] is True
        changed = w.answer(run(w, "moved", "automations", "settings", "set",
                               "engine.max_parallel", "2"))
        assert changed
        reloaded = w.answer(run(w, "moved", "automations", "service", "reload"))
        _say("automations-daemon-reloaded", reloaded)
        assert reloaded.get("reloaded") is True
        doctor = w.answer(run(w, "moved", "automations", "doctor"))
        assert doctor["ok"] is True and "config_stale" not in doctor
    finally:
        stopped = run(w, "moved", "automations", "service", "stop")
        _say("automations-daemon-stopped", {"stdout": stopped.stdout,
                                             "stderr": stopped.stderr})
    log = Path(started["log_file"]).read_text()
    assert "AUTOMATIONS_CONFIG" not in log.replace("AUTOMATIONS_CONFIG is ignored", "")
    assert w.files_left(root) == []


def test_askproject_resolves_its_profiles_from_the_store(world):
    w = world
    listed = w.answer(run(w, "moved", "askproject", "profiles"))
    shown = run(w, "moved", "askproject", "profiles", "show", "peer-read")
    _say("askproject-profiles", {"listed": listed, "shown": shown.stdout})
    entry = {e["name"]: e for e in listed["profiles"]}["peer-read"]
    assert entry["source"] == "store" and entry["harness"] == "claude"
    assert listed["lookup"][0]["source"] == "store"
    assert shown.returncode == 0 and shown.stdout.startswith("# store: profile 'peer-read'")
    assert 'model = "opus"' in shown.stdout
    files = w.answer(w.cap(w.roots["files"], "askproject", "profiles"))
    assert {e["name"]: e for e in files["profiles"]}["peer-read"]["source"] == "folder"


def test_tasks_reads_its_conveyor_and_settings_as_rows(world):
    w = world
    out = {"status": w.answer(run(w, "moved", "tasks", "service", "status")),
           "doctor": run(w, "moved", "tasks", "service", "doctor"),
           "init": w.answer(run(w, "moved", "tasks", "service", "init")),
           "inventory": w.answer(run(w, "moved", "tasks", "inventory")),
           "settings": w.answer(run(w, "moved", "tasks", "settings"))}
    out["doctor"] = json.loads(out["doctor"].stdout or out["doctor"].stderr)
    _say("tasks-moved", out)
    assert out["status"]["initialized"] is True
    assert out["status"]["config"].startswith("store:")
    assert out["init"]["written"] == [] and out["init"]["skipped"][0].startswith("store:")
    assert out["doctor"].get("settings", {}).get("poll_seconds") == 30, out["doctor"]
    # The conveyor is read from its document: the chain refuses a name that
    # is no worker before its last, and the doctor says so naming the store.
    w.sql("UPDATE capabilities_settings SET value = '[\"nobody\", \"Owner\"]' "
          "WHERE agent_id = %s AND capability = 'tasks.conveyor' AND path = '{escalate_to}'",
          (IDS["moved"],))
    doctor = run(w, "moved", "tasks", "doctor")
    _say("tasks-doctor-conveyor", {"stdout": doctor.stdout, "stderr": doctor.stderr})
    assert "tasks.conveyor" in doctor.stdout + doctor.stderr
    w.sql("UPDATE capabilities_settings SET value = '[\"Owner\"]' WHERE agent_id = %s AND "
          "capability = 'tasks.conveyor' AND path = '{escalate_to}'", (IDS["moved"],))
    assert w.files_left(w.roots["moved"]) == []


def test_a_stray_worker_file_is_neither_read_nor_removed(world):
    w = world
    stray = w.roots["moved"] / "capabilities" / "tasks" / "workers" / "stray.md"
    w.write(stray, "---\nenabled: true\ntakes: [change]\n---\n\nDo it.\n")
    try:
        doctor = run(w, "moved", "tasks", "service", "doctor")
        _say("tasks-stray-worker", {"stdout": doctor.stdout, "stderr": doctor.stderr})
        assert "workers_not_imported" not in doctor.stdout + doctor.stderr
        assert stray.exists()
    finally:
        shutil.rmtree(stray.parent)


def test_slack_reads_its_allow_list_as_rows(world):
    w = world
    refused = run(w, "moved", "slack", "read", "C99")
    _say("slack-policy-refusal", {"stderr": refused.stderr, "exit": refused.returncode})
    error = w.error(refused)
    assert error["code"] == "policy_refusal" and refused.returncode == 4
    assert "slack.policy settings document" in error["hint"]
    allowed = run(w, "moved", "slack", "read", "C01")
    _say("slack-policy-allowed", {"stderr": allowed.stderr, "exit": allowed.returncode})
    assert w.error(allowed)["code"] != "policy_refusal"
    # Through the contract's reader of named documents, as slack reads it.
    assert w.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                 "capability = 'slack.policy' AND path = '{allowed_users,U01}'",
                 (IDS["moved"],)) == [({},)]


def test_memory_and_the_service_capabilities_read_as_rows(world):
    w = world
    out = {}
    for capability, argv in (("memory", ("settings",)), ("memory", ("service", "init")),
                             ("telegram", ("settings",)), ("telegram", ("service", "status")),
                             ("whatsapp", ("settings",)), ("whatsapp", ("service", "status")),
                             ("agentrooms", ("settings",)),
                             ("agentrooms", ("service", "status")),
                             ("slack", ("settings",)), ("deployment", ("doctor",))):
        result = run(w, "moved", capability, *argv)
        out[f"{capability} {' '.join(argv)}"] = {
            "exit": result.returncode, "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:]}
    _say("service-capabilities-moved", out)
    # memory's settings exist as rows, so init refuses naming the store.
    init = out["memory service init"]
    assert init["exit"] == 6 and "settings_exist" in init["stderr"] and "store:" in init["stderr"]
    for key, result in out.items():
        said = result["stdout"] + result["stderr"]
        for path in GARBAGE.values():
            assert path not in said.replace("is ignored", ""), key
        assert "settings.json" not in result["stderr"] or "ignored" in result["stderr"], key
    assert w.files_left(w.roots["moved"]) == []


def test_service_init_in_a_moved_agent_writes_rows_and_no_file(world):
    w = world
    root = w.roots["fresh"]
    before = w.files_left(root)
    out = {
        "automations": w.answer(run(w, "fresh", "automations", "service", "init")),
        "tasks": w.answer(run(w, "fresh", "tasks", "service", "init")),
        "memory": w.answer(run(w, "fresh", "memory", "service", "init")),
        "slack": w.answer(run(w, "fresh", "slack", "service", "init")),
    }
    _say("service-init-fresh", out)
    assert before == [] and w.files_left(root) == []
    docs = {d for (d,) in w.sql("SELECT capability FROM capabilities_settings_documents "
                                "WHERE agent_id = %s", (IDS["fresh"],))}
    assert {"automations", "tasks", "memory", "slack"} <= docs
    again = w.answer(run(w, "fresh", "automations", "service", "init"))
    assert again["skipped"][0].startswith("store:")
    assert not [p for p in again["written"] if p.startswith("store:")]


def test_the_service_capabilities_answer_from_rows(world):
    w = world
    out = {}
    for capability, argv in (("telegram", ("service", "status")),
                             ("whatsapp", ("service", "status")),
                             ("whatsapp", ("profiles", "list")),
                             ("slack", ("service", "status")),
                             ("agentrooms", ("workers", "list"))):
        result = run(w, "moved", capability, *argv)
        out[f"{capability} {' '.join(argv)}"] = {
            "exit": result.returncode, "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:]}
    _say("service-status-moved", out)
    for key, result in out.items():
        assert result["exit"] == 0, (key, result["stderr"])


def test_deployment_counts_settings_rows_and_builds_without_a_database(world):
    w = world
    doctor = run(w, "moved", "deployment", "doctor")
    assert doctor.stdout.strip(), doctor.stderr
    found = json.loads(doctor.stdout)
    _say("deployment-doctor-moved", found)
    unconfigured = [f for f in found["findings"] if "is not configured" in f["message"]]
    # The settings documents the store holds count as configured; slack keeps
    # its allow-list and no service settings, so it alone is named, in the store.
    assert unconfigured == [{"severity": "error", "path": "store: slack settings",
                             "message": "active service 'slack' is not configured; run "
                                        "`slack service init`"}], unconfigured
    out = {}
    for name in ("moved", "files"):
        synced = w.cap(w.roots[name], "deployment", "sync", **GARBAGE)
        assert synced.returncode == 0, synced.stderr
        bundle = w.roots[name] / "deployment" / "targets" / "production"
        out[name] = {path.name: path.read_text() for path in sorted(bundle.iterdir())
                     if path.is_file() and path.name != "target.json"}
    _say("deployment-artifacts", out)
    moved, files = out["moved"], out["files"]
    dockerfile = next(v for k, v in moved.items() if k == "Dockerfile")
    assert "capabilities install" in dockerfile and "--allow" in dockerfile
    assert "capabilities doctor" not in dockerfile
    assert "capabilities doctor" in next(v for k, v in files.items() if k == "Dockerfile")
    entrypoint = next(v for k, v in moved.items() if k.startswith("entrypoint"))
    assert "capabilities doctor" in entrypoint
    ignore = next(v for k, v in moved.items() if k.endswith("dockerignore"))
    for pattern in ("capabilities/settings.json", "capabilities/*/connections.json",
                    "capabilities/*/identifiers.json", "capabilities/*/service/settings.json",
                    "capabilities/*/service/config.toml", "capabilities/tasks/conveyor.toml",
                    "capabilities/slack/policy.json"):
        assert pattern in ignore.splitlines(), pattern
        assert pattern not in next(v for k, v in files.items()
                                   if k.endswith("dockerignore")).splitlines()


# --- the migration's own carry-overs ---------------------------------------------------------

@pytest.fixture(scope="module")
def blank():
    """A second machine with its own store, whose global rows are not written."""
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="capabilities-dbonly-blank-")).resolve()
    found = Machine(tmp)
    try:
        found.start()
        for name in ("coolify", "memory"):
            found.install(found.registry, name)
        found.write(found.config / "capabilities" / "settings.json", gate("coolify", "memory"))
        found.write(found.config / "coolify" / "connections.json", {
            "default": "ops", "connections": {"ops": {
                "base_url": "https://coolify.example.org", "token_env": "COOLIFY_TOKEN",
                "allow_write": True}}})
        found.write(found.config / "coolify" / "identifiers.json",
                    {"team": {"value": "0", "note": "the default team"}})
        found.write(found.config / "memory" / "service" / "settings.json", {"max_entries": 500})
        found.agent("first", "prj_blank0000001", {
            "{env}/settings.json": gate("coolify"),
            "{env}/memory/service/settings.json": {"window_days": 9}})
        found.agentkit_list()
        yield found
    finally:
        found.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_global_marks_the_rows_written_only_once_every_document_is(blank):
    w = blank
    root = w.roots["first"]
    w.answer(w.manager(root, "agent", "show"))  # brings the store's tables up
    # A store that refuses the global settings document stops the run after
    # the records rows and before the marker.
    w.sql("CREATE FUNCTION refuse_settings() RETURNS trigger LANGUAGE plpgsql AS "
          "$$ BEGIN RAISE EXCEPTION 'refused for the test'; END $$")
    w.sql("CREATE TRIGGER refuse_settings BEFORE INSERT ON capabilities_settings "
          "FOR EACH ROW EXECUTE FUNCTION refuse_settings()")
    failed = w.manager(root, "migrate", "--global", "--apply")
    _say("global-interrupted", {"exit": failed.returncode, "stderr": failed.stderr})
    assert failed.returncode != 0
    assert w.sql("SELECT global_migrated_at FROM capabilities_global") == [(None,)]
    assert w.sql("SELECT capability FROM capabilities_policy WHERE agent_id IS NULL "
                 "ORDER BY capability") == [("coolify",), ("memory",)]
    w.sql("DROP TRIGGER refuse_settings ON capabilities_settings")
    w.sql("DROP FUNCTION refuse_settings()")
    again = w.answer(w.manager(root, "migrate", "--global", "--apply"))
    _say("global-resumed", again)
    assert again["global_migrated_at"] and again["settings_written"] == {"memory": again[
        "settings_written"]["memory"]}
    assert w.sql("SELECT path, value FROM capabilities_settings WHERE agent_id IS NULL") == [
        (["max_entries"], 500)]


def test_an_agent_made_after_discard_migrates_without_the_config_home(blank):
    w = blank
    first = w.answer(w.manager(w.roots["first"], "migrate", "--apply"))
    assert first["state"] == "migrated" and not first["verify"]["differences"]
    discarded = w.answer(w.manager(w.roots["first"], "migrate", "--discard"))
    assert discarded["removed"] and not (w.config / "capabilities" / "settings.json").exists()
    # A new agent, using the global connection by its own grant and inheriting
    # the global gate, identifiers and default.
    later = w.agent("later", "prj_blank0000002", {
        "{env}/settings.json": gate("memory"),
        "{env}/coolify/connections.json": {"connections": {"ops": {"enabled": True}}},
        "{env}/memory/service/settings.json": {"window_days": 4}})
    w.agentkit_list()
    dry = w.answer(w.manager(later, "migrate"))
    applied = w.answer(w.manager(later, "migrate", "--apply"))
    _say("migrate-after-discard", {"dry": dry, "applied": applied})
    assert applied["state"] == "migrated", applied
    assert not applied["verify"]["differences"], applied["verify"]
    assert w.sql("SELECT connection, enabled FROM capabilities_connection_grants WHERE "
                 "agent_id = 'prj_blank0000002'") == [("ops", True)]


def test_a_refused_write_while_an_agent_moves_creates_no_file(blank):
    w = blank
    root = w.agent("moving", "prj_blank0000003", {"{env}/settings.json": gate("memory")})
    w.answer(w.manager(root, "agent", "show"))
    w.answer(w.manager(root, "node", "add", MASTER, "--kind", "mac", "--master"))
    w.sql("UPDATE capabilities_agents SET migrating_since = now() WHERE id = 'prj_blank0000003'")
    before = w.files(root)
    refused = w.error(w.manager(root, "enable", "coolify", "--project"))
    _say("frozen-enable", refused)
    assert refused["code"] == "agent_migrating"
    assert w.files(root) == before and not (root / "capabilities" / ".gitignore").exists()
    w.sql("UPDATE capabilities_agents SET migrating_since = NULL "
          "WHERE id = 'prj_blank0000003'")


def test_init_names_the_store_gate_of_a_store_or_moved_agent(blank):
    w = blank
    out = {}
    for name in ("first", "later"):
        out[name] = w.answer(w.manager(w.roots[name], "init"))
    # An agent in store mode that has not moved: its records are rows by its
    # project.json, its settings files.
    store_mode = w.agent("stored", "prj_blank0000004", {"{env}/project.json": {
        "schema": "capabilities.project.v1", "slug": "stored", "id": "prj_blank0000004",
        "records": "store"}})
    out["stored"] = w.answer(w.manager(store_mode, "init"))
    files_mode = w.agent("plain", "prj_blank0000005", {"{env}/settings.json": gate("memory")})
    out["plain"] = w.answer(w.manager(files_mode, "init"))
    _say("init-gate", out)
    for name in ("first", "later", "stored"):
        assert out[name]["gate"].startswith("store:"), out[name]
        assert not (w.roots[name] / "capabilities" / "settings.json").exists()
    assert out["plain"]["gate"] == str(files_mode / "capabilities" / "settings.json")

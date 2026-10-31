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
                                  answers_file, gate, nid, node_env)


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
    answers = {"moved": answers_file(w, "moved", {
                   "nodes": {"srv-dbonly": {"register": True}},
                   # policy.json's entries, which the slack service would newly admit.
                   "settings": {"slack": {"allowed_users": {"U01": "keep"},
                                          "allowed_channels": {"C01": "keep"}}}}),
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
    assert shown.returncode == 0 and shown.stdout.startswith("# store: profile 'askproject-peer-read'")
    assert 'model = "opus"' in shown.stdout
    # Only its own: the other capabilities' harness profiles at the agent's
    # scope - automations' agents, whatsapp's dialogue profile - are neither
    # listed nor resolved, as its folder never held them.
    held = {name for (name,) in w.sql("SELECT name FROM harness_profiles WHERE scope_id = %s "
                                      "AND active", (IDS["moved"],))}
    assert "askproject-peer-read" in held and "automations-researcher" in held
    assert "clinic-claude" in held
    assert {e["name"] for e in listed["profiles"] if e["source"] == "store"} == {"peer-read"}
    assert not {"automations-researcher", "clinic-claude", "askproject-peer-read"} & {
        e["name"] for e in listed["profiles"]}
    for foreign in ("automations-researcher", "clinic-claude"):
        missing = run(w, "moved", "askproject", "profiles", "show", foreign)
        assert missing.returncode == 3 and w.error(missing)["code"] == "not_found", foreign
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
    # The escalation is the `escalation` table of the tasks settings: the chain
    # refuses a name that is no worker before its last, and the doctor says so
    # naming the store.
    assert out["settings"]["homes"][0]["document"]["escalation"] == {
        "escalate_to": ["Owner"], "stall_after": "6h"}
    w.sql("UPDATE capabilities_settings SET value = '[\"nobody\", \"Owner\"]' WHERE agent_id "
          "= %s AND capability = 'tasks' AND path = '{escalation,escalate_to}'", (IDS["moved"],))
    doctor = run(w, "moved", "tasks", "doctor")
    _say("tasks-doctor-conveyor", {"stdout": doctor.stdout, "stderr": doctor.stderr})
    assert "store:" in doctor.stdout + doctor.stderr and "[escalation]" in doctor.stdout + \
        doctor.stderr and "'nobody', which is no worker here" in doctor.stdout + doctor.stderr
    # Edited with the settings verb, checked as the claim reads it.
    refused = run(w, "moved", "tasks", "settings", "set", "escalation.stall_after", "soon")
    assert w.error(refused)["code"] == "settings_invalid", refused.stderr
    edits = {"chain": w.answer(run(w, "moved", "tasks", "settings", "set",
                                   "escalation.escalate_to", "Owner")),
             "ceiling": w.answer(run(w, "moved", "tasks", "settings", "set",
                                     "escalation.stall_after", "2h"))}
    _say("tasks-settings-escalation", edits)
    assert edits["chain"]["value"] == ["Owner"] and edits["ceiling"]["value"] == "2h"
    assert w.sql("SELECT path, value FROM capabilities_settings WHERE agent_id = %s AND "
                 "capability = 'tasks' AND path[1] = 'escalation' ORDER BY path",
                 (IDS["moved"],)) == [(["escalation", "escalate_to"], ["Owner"]),
                                      (["escalation", "stall_after"], "2h")]
    doctor = run(w, "moved", "tasks", "doctor")
    assert "conveyor_invalid" not in doctor.stdout + doctor.stderr, doctor.stderr
    assert json.loads(doctor.stdout)["escalation"]["stall_after"] == 7200, doctor.stdout
    assert run(w, "moved", "tasks", "settings", "set", "escalation.stall_after",
               "6h").returncode == 0
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
    assert "slack settings set allowed_channels" in error["hint"]
    allowed = run(w, "moved", "slack", "read", "C01")
    _say("slack-policy-allowed", {"stderr": allowed.stderr, "exit": allowed.returncode})
    assert w.error(allowed)["code"] != "policy_refusal"
    # policy.json is the slack settings' own allow-list now.
    assert w.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                 "capability = 'slack' AND path = '{allowed_users,U01}'",
                 (IDS["moved"],)) == [({},)]
    # Edited with the settings verb, and the gate reads the edit.
    added = w.answer(run(w, "moved", "slack", "settings", "set", "allowed_channels.C99",
                         '"#ninety-nine"', "--scope", "project"))
    opened = run(w, "moved", "slack", "read", "C99")
    _say("slack-settings-edit", {"set": added, "stderr": opened.stderr})
    assert w.error(opened)["code"] != "policy_refusal"
    w.answer(run(w, "moved", "slack", "settings", "unset", "allowed_channels.C99", "--scope",
                 "project"))
    assert w.error(run(w, "moved", "slack", "read", "C99"))["code"] == "policy_refusal"
    # Holding only the allow-list, the service is not yet initialized.
    assert w.answer(run(w, "moved", "slack", "service", "status"))["initialized"] is False


def test_policy_and_service_settings_move_as_one_allow_list(world):
    """The move folds policy.json into the slack settings: an entry both held
    keeps the service's own, with its role; every user or channel only one of
    them held - which the one allow-list would newly let the service admit, or
    `slack read` and `post` reach - is asked, and so is a mode they disagree
    on; nothing is written until each is answered, and the answer keeps the
    entry for both or drops it from both."""
    w = world
    root = w.agent("chat", "prj_dbonly00004", {
        "{env}/settings.json": gate("slack"),
        "{env}/slack/connections.json": {"default": "workspace",
                                         "connections": {"workspace": {"allow_write": False}}},
        "{env}/slack/policy.json": {"allowed_users": {"U01": "operator"},
                                    "allowed_channels": {"C01": "#ops", "C02": "#team"},
                                    "default_channel_policy": "open"},
        "{env}/slack/service/settings.json": {
            "allowed_users": {"U05": {"name": "lead", "role": "supervisor"}},
            "allowed_channels": {"C02": {"name": "team", "default_role": "default"},
                                 "C03": {"name": "watch", "default_role": "default"}},
            "auto_answer": {"users": [], "channels": ["C02"]},
            "defaults": {"worker": "stub"}},
        ".env.local": "SLACK_BOT_TOKEN=xoxb-test\n"}, ignore=(".env.local",))
    w.agentkit_list()
    dry = w.answer(w.manager(root, "migrate"))
    asked = {q["id"]: q for q in dry["questions"] if q["id"].startswith("settings.slack.")}
    before = w.everything()
    refused = w.error(w.manager(root, "migrate", "--apply", "--answers",
                                answers_file(w, "chat", {})))
    unwritten = w.everything() == before
    applied = w.answer(w.manager(root, "migrate", "--apply", "--answers", answers_file(
        w, "chat", {"settings": {"slack": {
            "default_channel_policy": "service",
            "allowed_users": {"U01": "drop", "U05": "keep"},
            "allowed_channels": {"C01": "keep", "C03": "drop"}}}})))
    document = {tuple(path): value for path, value in w.sql(
        "SELECT path, value FROM capabilities_settings WHERE agent_id = %s AND "
        "capability = 'slack'", ("prj_dbonly00004",))}
    _say("slack-one-allow-list", {"questions": asked, "refused": refused,
                                  "verify": applied["verify"], "document": {
                                      ".".join(k): v for k, v in document.items()}})
    assert set(asked) == {"settings.slack.default_channel_policy",
                          "settings.slack.allowed_users.U01",
                          "settings.slack.allowed_users.U05",
                          "settings.slack.allowed_channels.C01",
                          "settings.slack.allowed_channels.C03"}
    assert asked["settings.slack.allowed_users.U01"]["choices"] == ["keep", "drop"]
    assert "newly admit its direct messages" in asked[
        "settings.slack.allowed_users.U01"]["question"]
    assert "newly admit its mentions" in asked[
        "settings.slack.allowed_channels.C01"]["question"]
    assert "`slack read` and `post` would newly reach this channel" in asked[
        "settings.slack.allowed_channels.C03"]["question"]
    assert refused["code"] == "question_open" and unwritten
    assert applied["state"] == "migrated" and not applied["verify"]["differences"]
    assert document[("allowed_channels", "C01")] == "#ops"
    assert document[("allowed_channels", "C02", "default_role")] == "default"
    assert document[("allowed_users", "U05", "role")] == "supervisor"
    assert not [k for k in document if k[:2] in (("allowed_users", "U01"),
                                                 ("allowed_channels", "C03"))]
    assert ("default_channel_policy",) not in document
    assert not (root / "capabilities" / "slack" / "policy.json").exists()
    out = {target: w.cap(root, "slack", "read", target, **GARBAGE)
           for target in ("C01", "C02", "C03", "U01")}
    for target in ("C03", "U01"):
        assert w.error(out[target])["code"] == "policy_refusal", target
    for target in ("C01", "C02"):
        assert w.error(out[target])["code"] != "policy_refusal", target
    assert w.answer(w.cap(root, "slack", "service", "status"))["initialized"] is True


def test_conveyor_moves_into_the_tasks_settings_unless_they_carry_one(world):
    w = world
    root = w.agent("queue", "prj_dbonly00005", {
        "{env}/settings.json": gate("tasks"),
        "{env}/tasks/conveyor.toml": 'escalate_to = ["Owner"]\n'})
    w.agentkit_list()
    applied = w.answer(w.manager(root, "migrate", "--apply", "--answers",
                                 answers_file(w, "queue", {})))
    rows = w.sql("SELECT path, value FROM capabilities_settings WHERE agent_id = %s AND "
                 "capability = 'tasks' ORDER BY path", ("prj_dbonly00005",))
    _say("tasks-conveyor-folded", {"rows": rows, "verify": applied["verify"]})
    assert applied["state"] == "migrated" and not applied["verify"]["differences"]
    # No service settings: the home's seed, with the escalation table.
    assert rows == [(["escalation", "escalate_to"], ["Owner"]), (["version"], 1)]
    status = w.answer(w.cap(root, "tasks", "service", "status"))
    assert status["initialized"] is False
    init = w.answer(w.cap(root, "tasks", "service", "init"))
    assert init["written"] and w.sql(
        "SELECT value FROM capabilities_settings WHERE agent_id = %s AND capability = 'tasks' "
        "AND path = '{escalation,escalate_to}'", ("prj_dbonly00005",)) == [(["Owner"],)]
    # Settings that carry the table already keep it; the file is not moved.
    kept = w.agent("queue-kept", "prj_dbonly00006", {
        "{env}/settings.json": gate("tasks"),
        "{env}/tasks/service/config.toml": 'version = 1\n\n[escalation]\n'
                                           'escalate_to = ["Lead"]\n',
        "{env}/tasks/conveyor.toml": 'escalate_to = ["Owner"]\n'})
    w.agentkit_list()
    dry = w.answer(w.manager(kept, "migrate"))
    assert any("conveyor.toml is not moved" in line for line in dry["plan"]["warnings"]), dry
    w.answer(w.manager(kept, "migrate", "--apply", "--answers",
                       answers_file(w, "queue-kept", {})))
    assert w.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                 "path = '{escalation,escalate_to}'", ("prj_dbonly00006",)) == [(["Lead"],)]


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
    # The settings documents the store holds count as configured; slack's
    # allow-list is its settings document now, so none is named.
    assert unconfigured == [], unconfigured
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
                    "capabilities/slack/policy.json", "capabilities/askproject/profiles/",
                    "capabilities/*/service/profiles/", "capabilities/tasks/workers/",
                    "capabilities/tasks/profiles/"):
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
    # Discarded on a node that is no agent's master, as a server's config home is.
    discarded = w.answer(w.manager(w.roots["first"], "migrate", "--discard",
                                   **node_env("srv-discard")))
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
    w.answer(w.manager(root, "node", "add", nid(MASTER), "--kind", "mac", "--master"))
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


# --- install, images and the dev lane ----------------------------------------------------------

def test_a_core_arrival_is_a_global_row_where_a_database_resolves(blank, tmp_path):
    from test_manager_core_tier import CORE, _catalogue, _machine_env, _outside
    w = blank
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        env.update({"AGENTKIT_DB_URL": w.url(), "AGENTKIT_DB_SCHEMA": SCHEMA})
        installed = subprocess.run([str(MANAGER), "install", CORE], cwd=_outside(tmp_path),
                                   env=env, text=True, capture_output=True, timeout=300)
        assert installed.returncode == 0, installed.stderr
        first = json.loads(installed.stdout)
        # A row the store holds already is the user's, and is kept.
        w.sql("UPDATE capabilities_policy SET enabled = false WHERE agent_id IS NULL AND "
              "capability = %s", (CORE,))
        subprocess.run([str(MANAGER), "uninstall", CORE], cwd=_outside(tmp_path), env=env,
                       text=True, capture_output=True, timeout=120, check=True)
        again = json.loads(subprocess.run(
            [str(MANAGER), "install", CORE], cwd=_outside(tmp_path), env=env, text=True,
            capture_output=True, timeout=300, check=True).stdout)
    _say("install-arrival-with-database", {"first": first, "again": again})
    assert first["global_policy"] == {"enabled": True,
                                      "gate": f"store:{SCHEMA}.capabilities_policy"}
    assert "global_policy" not in again
    assert w.sql("SELECT enabled FROM capabilities_policy WHERE agent_id IS NULL AND "
                 "capability = %s AND node IS NULL", (CORE,)) == [(False,)]
    assert not (tmp_path / "config" / "capabilities" / "settings.json").exists()
    w.sql("DELETE FROM capabilities_policy WHERE agent_id IS NULL AND capability = %s",
          (CORE,))


def test_core_arrivals_made_before_a_store_are_written_by_the_global_migration(
        blank, tmp_path):
    from test_manager_core_tier import M, _catalogue, _machine_env, _outside
    w = blank
    schema = "arrivals"
    first, second = sorted(M.CORE_CAPABILITIES)[:2]
    with _catalogue(tmp_path / "catalogue", first, second) as source:
        env = _machine_env(tmp_path, source)
        here = _outside(tmp_path)

        def manager(*args: str) -> dict:
            done = subprocess.run([str(MANAGER), *args], cwd=here, env=env, text=True,
                                  capture_output=True, timeout=300)
            assert done.returncode == 0, (args, done.stderr)
            return json.loads(done.stdout)

        installed = {name: manager("install", name) for name in (first, second)}
        # The user's own choice, made before any store, is kept over the arrival.
        manager("disable", second, "--global")
        env.update({"AGENTKIT_DB_URL": w.url(), "AGENTKIT_DB_SCHEMA": schema})
        migrated = manager("migrate", "--global", "--apply")
    _say("arrivals-before-a-store", {"installed": installed, "migrated": migrated})
    assert all(i["global_policy"]["pending"] for i in installed.values())
    arrivals = [r for r in migrated["plan"]["policy"] if r.get("arrival")]
    assert [r["capability"] for r in arrivals] == [first]
    with w.admin(w.database) as conn:
        rows = conn.execute(f"SELECT capability, enabled FROM {schema}.capabilities_policy "
                            "WHERE agent_id IS NULL AND node IS NULL ORDER BY capability"
                            ).fetchall()
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
    assert rows == [(first, True), (second, False)]
    assert not (tmp_path / "config" / "capabilities" / "arrival-pending.json").exists()


def test_a_dev_session_keeps_its_records_in_a_schema_of_its_own(world, tmp_path):
    from test_manager_dev_sessions import _commit_all, _source_repo
    w = world
    source = _source_repo(tmp_path)
    shutil.copytree(REPO / "capabilities" / "memory", source / "capabilities" / "memory",
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    _commit_all(source, "memory")
    outer = {k: v for k, v in os.environ.items()
             if not k.startswith(("AGENTKIT_DB_", "CAPABILITIES_")) and k not in (
                 "CLAUDE_PROJECT_DIR", "VIRTUAL_ENV")}
    outer.update({"HOME": str(tmp_path / "outer-home"),
                  "CAPABILITIES_HOME": str(tmp_path / "outer-registry"),
                  "CAPABILITIES_BIN": str(tmp_path / "outer-bin"),
                  "XDG_CONFIG_HOME": str(tmp_path / "outer-config"),
                  "XDG_STATE_HOME": str(tmp_path / "outer-state"),
                  "XDG_DATA_HOME": str(tmp_path / "outer-data"),
                  "XDG_CACHE_HOME": str(tmp_path / "outer-cache"),
                  **node_env(MASTER), "USER": "tester"})
    # The machine's database, as `capabilities store set` leaves it.
    w.write(tmp_path / "outer-config" / "agentkit" / "store.json", {
        "schema": "agentkit.store.v1", "host": "127.0.0.1", "port": w.port,
        "database": w.database, "user": "pgadmin", "sslmode": "disable",
        "db_schema": SCHEMA})

    def manager(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess:
        return subprocess.run([str(MANAGER), *args], cwd=cwd, env=outer, text=True,
                              capture_output=True, timeout=900)

    started = manager("dev", "start", "memory", "--source", str(source), "--project",
                      str(w.roots["moved"]), "--session", "dbonly-lane", "--allow-parallel")
    assert started.returncode == 0, started.stderr
    session = json.loads(started.stdout)
    try:
        # One uv cache for the machine, also inside the lane.
        lane_cache = Path(session["isolated"]["cache"]) / "uv"
        if os.environ.get("UV_CACHE_DIR") and not lane_cache.exists():
            lane_cache.parent.mkdir(parents=True, exist_ok=True)
            lane_cache.symlink_to(os.environ["UV_CACHE_DIR"])
        installed = manager("dev", "install", "dbonly-lane", "memory")
        assert installed.returncode == 0, installed.stderr
        probe = manager("dev", "exec", "dbonly-lane", "--", "sh", "-c",
                        "echo $AGENTKIT_DB_SCHEMA $AGENT_NODE_NAME")
        ran = manager("dev", "run", "dbonly-lane", "memory", "--", "settings")
        direct = w.cap(w.roots["moved"], "memory", "settings")
        _say("dev-session-schema", {"probe": probe.stdout, "run_stdout": ran.stdout,
                                    "run_stderr": ran.stderr[-3000:],
                                    "direct": direct.stdout})
        assert probe.returncode == 0 and probe.stdout.split() == ["dev_dbonly_lane", MASTER]
        assert ran.returncode == 0, ran.stderr
        diagnostic = next(json.loads(line)["dev_run"] for line in ran.stderr.splitlines()
                          if line.startswith('{"dev_run"'))
        assert diagnostic["database_schema"] == "dev_dbonly_lane"
        assert diagnostic["projected_rows"]["capabilities_agents"] == 1
        # The payload read the projected rows of its own schema, as the agent's
        # own command reads the store's.
        lane_homes = json.loads(ran.stdout)["homes"]
        real_homes = json.loads(direct.stdout)["homes"]
        assert [h["document"] for h in lane_homes] == [h["document"] for h in real_homes]
        assert lane_homes[0]["path"] == "store:dev_dbonly_lane.capabilities_settings"
        assert real_homes[0]["path"] == f"store:{SCHEMA}.capabilities_settings"
        held = w.sql("SELECT id, migrated_at IS NOT NULL FROM dev_dbonly_lane."
                     "capabilities_agents")
        assert held == [(IDS["moved"], True)]
    finally:
        manager("dev", "stop", "dbonly-lane", "--discard-work")


def test_a_dev_session_drops_the_schema_it_made_and_no_other(world, tmp_path):
    """A session's schema goes when the session ends - by finish, stop or gc -
    and a schema the session found already there, or whose mark is not the
    session's, stays."""
    from test_manager_dev_sessions import _commit_all, _consumer_repo, _release_source_repo
    w = world
    source, _remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTKIT_DB_", "CAPABILITIES_")) and k not in (
               "CLAUDE_PROJECT_DIR", "VIRTUAL_ENV")}
    env.update({"HOME": str(tmp_path / "outer-home"),
                "CAPABILITIES_HOME": str(tmp_path / "outer-registry"),
                "CAPABILITIES_BIN": str(tmp_path / "outer-bin"),
                "XDG_CONFIG_HOME": str(tmp_path / "outer-config"),
                "XDG_STATE_HOME": str(tmp_path / "outer-state"),
                "XDG_DATA_HOME": str(tmp_path / "outer-data"),
                "XDG_CACHE_HOME": str(tmp_path / "outer-cache"),
                "AGENT_NODE_NAME": MASTER, "USER": "tester"})
    w.write(tmp_path / "outer-config" / "agentkit" / "store.json", {
        "schema": "agentkit.store.v1", "host": "127.0.0.1", "port": w.port,
        "database": w.database, "user": "pgadmin", "sslmode": "disable",
        "db_schema": SCHEMA})

    def manager(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(MANAGER), *args], cwd=REPO, env=env, text=True,
                              capture_output=True, timeout=900)

    def schema(name: str):
        return w.sql("SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
                     "WHERE nspname = %s", (f"dev_{name.replace('-', '_')}",))

    # A schema of a session's name that was there before the session.
    w.sql("CREATE SCHEMA dev_found_lane")
    ended = {}
    for name in ("found-lane", "marked-lane", "stop-lane", "gc-lane", "finish-lane"):
        started = manager("dev", "start", "askproject", "--source", str(source), "--project",
                          str(consumer), "--session", name, "--allow-parallel")
        assert started.returncode == 0, started.stderr
        probe = manager("dev", "exec", name, "--", "sh", "-c", "echo $AGENTKIT_DB_SCHEMA")
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.split() == [f"dev_{name.replace('-', '_')}"]
    assert schema("found-lane") == [(None,)]
    for name in ("marked-lane", "stop-lane", "gc-lane", "finish-lane"):
        [(mark,)] = schema(name)
        assert mark.startswith(f"capabilities dev session {name} "), mark
    w.sql("COMMENT ON SCHEMA dev_marked_lane IS 'someone else''s'")
    for name in ("found-lane", "marked-lane"):
        ended[name] = json.loads(manager("dev", "stop", name, "--discard-work").stdout)
    ended["stop-lane"] = json.loads(manager("dev", "stop", "stop-lane",
                                            "--discard-work").stdout)
    listed = json.loads(manager("dev", "list").stdout)
    finish = next(one for one in listed["sessions"] if one["id"] == "finish-lane")
    worktree = Path(finish["source_worktree"])
    (worktree / "README.md").write_text("released from the dev worktree\n")
    _commit_all(worktree, "Prepare release candidate")
    finished = manager("dev", "finish", "finish-lane")
    assert finished.returncode == 0, finished.stderr
    ended["finish-lane"] = json.loads(finished.stdout)["cleanup"]
    time.sleep(2)
    collected = json.loads(manager("dev", "gc", "--older-than", "1s").stdout)
    ended["gc-lane"] = next(one for one in collected["removed"] if one["session"] == "gc-lane")
    _say("dev-schema-ends", ended)
    assert "database_schema" not in ended["found-lane"] and schema("found-lane") == [(None,)]
    assert ended["marked-lane"]["database_schema"] == {
        "schema": "dev_marked_lane", "dropped": False,
        "reason": "it does not carry this session's mark"}
    assert schema("marked-lane") == [("someone else's",)]
    for name in ("stop-lane", "gc-lane", "finish-lane"):
        assert ended[name]["database_schema"] == {
            "schema": f"dev_{name.replace('-', '_')}", "dropped": True}, name
        assert schema(name) == [], name
    # The store's own schema was never touched.
    assert w.sql("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (SCHEMA,)) == [(1,)]
    w.sql("DROP SCHEMA dev_found_lane")
    w.sql("DROP SCHEMA dev_marked_lane")


def test_the_machine_scheduler_serves_a_moved_agent(world):
    w = world
    # On its list as the node manager puts an agent placed on the node in
    # mode shared there (`node supervise automations --machine`).
    root = w.roots["moved"]
    [(slug,)] = w.sql("SELECT slug FROM capabilities_agents WHERE id = %s", (IDS["moved"],))
    slug = slug or json.loads((root / "capabilities" / "project.json").read_text()).get(
        "slug") or IDS["moved"]
    listing = w.config / "automations" / "service" / "projects.toml"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(f'version = 1\n\n[projects.{IDS["moved"]}]\nroot = {json.dumps(str(root))}\n'
                       f'slug = {json.dumps(slug)}\njoined_at = 2026-10-29T10:00:00Z\n'
                       'joined_by = "capabilities node supervise automations --machine"\n')
    started = w.answer(run(w, "moved", "automations", "service", "start", "--machine"))
    try:
        deadline, served = time.monotonic() + 60, None
        while time.monotonic() < deadline:
            status = w.answer(run(w, "moved", "automations", "service", "status", "--machine"))
            served = {row["project_id"]: row for row in status["projects"]}.get(IDS["moved"])
            if served and served.get("state") in ("served", "error", "refused"):
                break
            time.sleep(0.5)
        _say("automations-machine-moved", {"started": started, "row": served})
        assert served and served["state"] == "served", served
    finally:
        run(w, "moved", "automations", "service", "stop", "--machine")
        listing.unlink(missing_ok=True)


@pytest.mark.parametrize("capability", ["tasks", "memory"])
def test_the_service_starts_and_stops_with_no_settings_file(world, capability):
    w = world
    started = run(w, "moved", capability, "service", "start")
    try:
        _say(f"{capability}-service-start-moved", {"exit": started.returncode,
                                                   "stdout": started.stdout[-3000:],
                                                   "stderr": started.stderr[-3000:]})
        assert started.returncode == 0, started.stderr
        status = w.answer(run(w, "moved", capability, "service", "status"))
        _say(f"{capability}-service-status-moved", status)
        assert status["running"] is True, status
    finally:
        stopped = run(w, "moved", capability, "service", "stop")
        assert stopped.returncode == 0, stopped.stderr
    assert w.files_left(w.roots["moved"]) == []


def _build_context(root: Path, ignore: str, dest: Path) -> Path:
    """What `COPY . <project_root>` puts in the image: the agent's tracked files
    less what the compiled .dockerignore excludes."""
    import fnmatch
    patterns = [line.strip() for line in ignore.splitlines()
                if line.strip() and not line.startswith("#")]
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True,
                             text=True, check=True).stdout.split()
    for rel in tracked:
        if any(fnmatch.fnmatch(rel, p.rstrip("/")) or fnmatch.fnmatch(rel, p.rstrip("/") + "/*")
               or rel.startswith(p.rstrip("/") + "/") for p in patterns):
            continue
        if (root / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, dest / rel)
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    return dest


@pytest.mark.parametrize("which", ["moved", "store-mode"])
def test_the_image_build_steps_run_without_a_database(world, tmp_path, which):
    """The compiled Dockerfile's build steps - every catalogue capability
    installed --allow, then `capabilities init --codex --claude` - run in a
    fresh box holding the agent's tracked files and no database: they succeed,
    write no gate, and leave the store's reads to container start."""
    from test_manager_core_tier import CORE, _catalogue, _machine_env
    w = world
    root = w.roots["moved"]
    synced = w.cap(root, "deployment", "sync")
    assert synced.returncode == 0, synced.stderr
    bundle = root / "deployment" / "targets" / "production"
    dockerfile = (bundle / "Dockerfile").read_text()
    ignore = next(p for p in bundle.iterdir() if p.name.endswith("dockerignore")).read_text()
    assert 'capabilities install "$name" --allow' in dockerfile
    assert "&& capabilities init --codex --claude || {" in dockerfile
    # The id is not among the agent's files: the image carries it, bound to the
    # image's project root, which is the box here.
    binding = f'ENV CAPABILITIES_PROJECT_ID="{IDS["moved"]}" CAPABILITIES_PROJECT_ID_ROOT="/app"'
    assert binding in dockerfile.splitlines()
    assert dockerfile.index(binding) < dockerfile.index("capabilities install")
    box = _build_context(root, ignore, tmp_path / "app")
    if which == "store-mode":
        # A store-mode agent that has not moved builds the same way.
        identity = box / "capabilities" / "project.json"
        identity.write_text(json.dumps({"schema": "capabilities.project.v1", "slug": "sm",
                                        "id": "prj_storemode001", "records": "store"}))
    assert not list(box.rglob("connections.json")) and not (
        box / "capabilities" / "settings.json").exists()
    with _catalogue(tmp_path / "catalogue", CORE, "slack") as source:
        env = _machine_env(tmp_path / "machine", source)
        env.pop("AGENT_NODE_NAME", None)
        env.update(CAPABILITIES_PROJECT_ID=IDS["moved"], CAPABILITIES_PROJECT_ID_ROOT=str(box))
        steps = {}
        for name in (CORE, "slack"):
            steps[f"install {name}"] = subprocess.run(
                [str(MANAGER), "install", name, "--allow"], cwd=box, env=env, text=True,
                capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        steps["init"] = subprocess.run(
            [str(MANAGER), "init", "--codex", "--claude"], cwd=box, env=env, text=True,
            capture_output=True, timeout=300)
    _say(f"image-build-steps-{which}", {k: {"exit": r.returncode, "stdout": r.stdout[-2000:],
                                            "stderr": r.stderr[-2000:]}
                                        for k, r in steps.items()})
    for key, result in steps.items():
        assert result.returncode == 0, (key, result.stderr)
    installed = json.loads(steps[f"install {CORE}"].stdout)
    assert installed["global_policy"]["pending"] is True
    init = json.loads(steps["init"].stdout)
    assert init["agent"]["registered"] is False
    assert init["gate"].startswith("store:")
    assert not (box / "capabilities" / "settings.json").exists()
    assert not (tmp_path / "machine" / "config" / "capabilities" / "settings.json").exists()
    assert (box / ".claude" / "settings.json").exists()
    assert "not reachable" in (box / ".claude" / "rules" / "CAPABILITIES.md").read_text()


@pytest.mark.parametrize("identity, code", [
    ({"store": "other"}, "bad_store_mode"),
    ({"records": "weird"}, "bad_records_mode"),
    ({"records": "store"}, None),
])
def test_init_passes_over_only_an_absent_store(tmp_path, identity, code):
    """Init's offline part goes on where no store is configured or reachable,
    and stops, as it always has, on any other refusal of the project's records."""
    from test_manager_core_tier import _machine_env
    root = tmp_path / "agent"
    (root / "capabilities").mkdir(parents=True)
    (root / "capabilities" / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "slug": "agent", "id": "prj_initcheck001",
        **identity}))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    env = _machine_env(tmp_path / "machine", "")
    env.pop("CAPABILITIES_SOURCE")
    env.pop("AGENT_NODE_NAME", None)
    ran = subprocess.run([str(MANAGER), "init"], cwd=root, env=env, text=True,
                         capture_output=True, timeout=300)
    _say(f"init-{code}", {"exit": ran.returncode, "stderr": ran.stderr[-1500:]})
    if code is None:
        assert ran.returncode == 0, ran.stderr
        assert json.loads(ran.stdout)["gate"].startswith("store:")
    else:
        assert ran.returncode == 6
        assert json.loads(ran.stderr.strip().splitlines()[-1])["error"]["code"] == code

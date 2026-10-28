"""The agent's vault and its key: `capabilities key generate|set|change` and
`capabilities secrets list|show|set|rm|raw`, against the store of a project that
exists only for the test.

These tests drive the real manager against the throwaway PostgreSQL of
test_manager_agents, which skips without `initdb`, `pg_ctl` or psycopg.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest
from test_manager_agents import MANAGER, PROJECT_ID, SCHEMA, Lab, cluster  # noqa: F401

KEY_ENV = "AGENT_ENCRYPTION_KEY"


@pytest.fixture()
def lab(tmp_path, cluster):  # noqa: F811
    found = Lab(tmp_path, cluster)
    found.ok("init")
    return found


def _feed(lab: Lab, stdin: str, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MANAGER), *args], cwd=lab.project,
                          env=lab.env(**extra), text=True, input=stdin,
                          capture_output=True, timeout=120)


def _fed(lab: Lab, stdin: str, *args: str) -> dict:
    result = _feed(lab, stdin, *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess) -> tuple[int, dict]:
    line = [line for line in result.stderr.splitlines() if line.startswith("{")][-1]
    return result.returncode, json.loads(line)["error"]


def _key_line(lab: Lab) -> str:
    found = [line.split("=", 1)[1] for line in
             (lab.project / ".env.local").read_text().splitlines()
             if line.startswith(KEY_ENV + "=")]
    assert len(found) == 1
    return found[0]


def test_generate_writes_the_key_to_the_folder_and_only_its_fingerprint_to_the_store(lab):
    made = lab.ok("key", "generate")
    key = _key_line(lab)
    assert len(key) == 43 and key not in json.dumps(made)
    assert made["fingerprint"] == lab.sql(
        f"SELECT vault_key FROM {SCHEMA}.capabilities_agents WHERE id = %s",
        (PROJECT_ID,))[0][0]
    assert made["written"] == str(lab.project / ".env.local")
    code, error = lab.refused("key", "generate")
    assert (code, error["code"]) == (6, "key_exists")


def test_a_secret_is_set_from_stdin_listed_by_name_shown_on_request_and_removed(lab):
    lab.ok("key", "generate")
    assert _fed(lab, "tok-1\n", "secrets", "set", "API_TOKEN")["changed"] == "created"
    assert _fed(lab, "tok-2", "secrets", "set", "API_TOKEN")["changed"] == "replaced"
    listed = lab.ok("secrets", "list")
    assert [s["name"] for s in listed["secrets"]] == ["API_TOKEN"]
    assert listed["secrets"][0]["readable"] is True and listed["unreadable"] == []
    assert listed["key"]["set"] is True and listed["key"]["matches"] is True
    assert "tok-2" not in json.dumps(listed)
    stored = lab.sql(f"SELECT ciphertext FROM {SCHEMA}.capabilities_vault")
    assert b"tok-2" not in bytes(stored[0][0])
    shown = lab.run("secrets", "show", "API_TOKEN")
    assert shown.returncode == 0 and shown.stdout == "tok-2\n"
    assert lab.ok("secrets", "rm", "API_TOKEN", "NEVER") == {
        "ok": True, "agent": PROJECT_ID, "removed": ["API_TOKEN"], "absent": ["NEVER"],
        "store": {"level": "project", "schema": SCHEMA}}
    code, error = lab.refused("secrets", "show", "API_TOKEN")
    assert (code, error["code"]) == (3, "not_set")


def test_a_value_never_comes_on_argv_and_a_verb_needs_the_key(lab):
    code, error = lab.refused("secrets", "set", "API_TOKEN", "tok-1")
    assert (code, error["code"]) == (6, "input")
    code, error = _error(_feed(lab, "tok-1", "secrets", "set", "API_TOKEN"))
    assert (code, error["code"]) == (6, "key_missing")
    lab.ok("key", "generate")
    code, error = _error(_feed(lab, "", "secrets", "set", "API_TOKEN"))
    assert (code, error["code"]) == (6, "input")
    code, error = _error(_feed(lab, "x", "secrets", "set", "1BAD"))
    assert (code, error["code"]) == (6, "bad_secret_name")


def test_key_set_takes_the_agents_key_and_refuses_another(lab, tmp_path):
    lab.ok("key", "generate")
    key = _key_line(lab)
    _fed(lab, "tok-1", "secrets", "set", "API_TOKEN")
    (lab.project / ".env.local").write_text(
        "\n".join(line for line in (lab.project / ".env.local").read_text().splitlines()
                  if not line.startswith(KEY_ENV)) + "\n")
    code, error = _error(_feed(lab, "zz-wrong-zz", "key", "set"))
    assert (code, error["code"]) == (6, "bad_key")
    assert "zz-wrong-zz" not in error["message"]
    other = "A" * 43
    code, error = _error(_feed(lab, other, "key", "set"))
    assert (code, error["code"]) == (6, "key_mismatch")
    assert _fed(lab, key + "\n", "key", "set")["fingerprint"]
    assert _key_line(lab) == key
    assert lab.run("secrets", "show", "API_TOKEN").stdout == "tok-1\n"


def test_change_leaves_what_was_sealed_before_unreadable_and_names_how_many(lab):
    lab.ok("key", "generate")
    old = _key_line(lab)
    _fed(lab, "tok-1", "secrets", "set", "API_TOKEN")
    _fed(lab, "x", "secrets", "set", "OTHER")
    changed = lab.ok("key", "change")
    assert changed["unreadable"] == 2 and changed["previous_fingerprint"]
    assert _key_line(lab) != old
    listed = lab.ok("secrets", "list")
    assert listed["unreadable"] == ["API_TOKEN", "OTHER"]
    code, error = lab.refused("secrets", "show", "API_TOKEN")
    assert (code, error["code"]) == (6, "secret_unreadable")
    _fed(lab, "tok-2", "secrets", "set", "API_TOKEN")
    assert lab.ok("secrets", "list")["unreadable"] == ["OTHER"]
    assert lab.run("secrets", "show", "API_TOKEN").stdout == "tok-2\n"
    # Removing needs no key at all.
    (lab.project / ".env.local").write_text(
        "\n".join(line for line in (lab.project / ".env.local").read_text().splitlines()
                  if not line.startswith(KEY_ENV)) + "\n")
    assert lab.ok("secrets", "rm", "OTHER")["removed"] == ["OTHER"]


def test_raw_prints_the_vault_as_text_and_saves_text_whole_keeping_unreadable_rows(lab):
    lab.ok("key", "generate")
    _fed(lab, "tok-1", "secrets", "set", "API_TOKEN")
    _fed(lab, "a\nb", "secrets", "set", "MULTI")
    printed = lab.run("secrets", "raw")
    assert printed.returncode == 0
    assert printed.stdout == 'API_TOKEN=tok-1\nMULTI="a\\nb"\n'
    lab.ok("key", "change")
    _fed(lab, "kept", "secrets", "set", "KEPT")
    saved = _fed(lab, "API_TOKEN=tok-9\nNEW='n e w'\n# a comment\n", "secrets", "raw", "--set")
    assert saved["set"] == ["API_TOKEN", "NEW"] and saved["removed"] == ["KEPT"]
    assert saved["unreadable_kept"] == ["MULTI"] and saved["unchanged"] == []
    again = _fed(lab, "API_TOKEN=tok-9\nNEW='n e w'\n", "secrets", "raw", "--set")
    assert again["set"] == [] and again["unchanged"] == ["API_TOKEN", "NEW"]
    printed = lab.run("secrets", "raw")
    assert printed.stdout == "API_TOKEN=tok-9\nNEW=n e w\n"
    assert json.loads(printed.stderr)["unreadable"] == ["MULTI"]
    code, error = _error(_feed(lab, "no equals sign\n", "secrets", "raw", "--set"))
    assert (code, error["code"]) == (6, "bad_text")


def test_the_writing_verbs_are_refused_under_the_read_only_switch(lab):
    lab.ok("key", "generate")
    _fed(lab, "tok-1", "secrets", "set", "API_TOKEN")
    for args in (("key", "change"), ("secrets", "rm", "API_TOKEN"),
                 ("secrets", "raw", "--set"), ("secrets", "set", "API_TOKEN"),
                 ("secrets", "move")):
        result = _feed(lab, "X=1", *args, CAPABILITIES_READ_ONLY="1")
        code, error = _error(result)
        assert (code, error["code"]) == (4, "read_only_switch"), args
    result = lab.run("secrets", "show", "API_TOKEN", CAPABILITIES_READ_ONLY="1")
    assert result.stdout == "tok-1\n"


# A capability that reads KEYS through the cascade: the project's .env.local,
# then .env, read by the library's rule, then - where it reads the vault - the
# agent's vault, here asked through the manager's own `secrets show`. With
# PAUSE it waits, once its keys have left the files, until it is ended.
_READER = '''#!{python}
import json, subprocess, sys, time
from pathlib import Path
KEYS, VAULT, CODE, PAUSE = {keys!r}, {vault!r}, {code!r}, {pause!r}
if CODE:
    sys.stderr.write(json.dumps({{"error": {{"code": CODE, "message": "no"}}}}) + "\\n")
    sys.exit(4)
def env(path):
    out = {{}}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out
rows = []
for key in KEYS:
    found = None
    for name in (".env.local", ".env"):
        value = env(Path.cwd() / name).get(key)
        if value:
            found = (value, "project", str(Path.cwd() / name))
            break
    if found is None and VAULT:
        shown = subprocess.run([sys.executable, {manager!r}, "secrets", "show", key],
                               capture_output=True, text=True)
        if shown.returncode == 0:
            found = (shown.stdout[:-1], "vault", "vault:agent")
    if PAUSE and (found is None or found[1] != "project"):
        Path(PAUSE).touch()
        time.sleep(20)
    value, tier, source = found or (None, None, None)
    rows.append({{"key": key, "secret": True, "required": True, "set": bool(value),
                 "tier": tier, "source": source,
                 "value": (("…" + value[-4:]) if len(value) >= 8 else "****")
                 if value else None}})
print(json.dumps({{"connections": {{"main": {{"keys": rows}}}}}}))
'''


def _reader(lab: Lab, name: str, keys: list, vault: bool = True, code: str = "",
            pause: str = "") -> None:
    home = lab.tmp / "registry" / name
    home.mkdir(parents=True, exist_ok=True)
    script = home / name
    script.write_text(_READER.format(python=sys.executable, keys=keys, vault=vault,
                                     code=code, pause=pause, manager=str(MANAGER)))
    script.chmod(0o755)


def test_move_takes_only_the_keys_every_reader_reads_from_the_vault(lab):
    lab.ok("key", "generate")
    env_local = lab.project / ".env.local"
    env_local.write_text(env_local.read_text() + "# a comment\nMOVED_TOKEN='tok-moved'\n"
                         "OLD_TOKEN=tok-old\nNOBODY=x-nobody\nSPLIT=a-one\n"
                         'OPEN_QUOTE="tok-open\n')
    (lab.project / ".env").write_text("MOVED_TOKEN='tok-moved'\nSPLIT=b-two\n")
    _reader(lab, "fresh", ["MOVED_TOKEN", "OLD_TOKEN", "SPLIT", "OPEN_QUOTE"])
    _reader(lab, "stale", ["OLD_TOKEN"], vault=False)
    _reader(lab, "gated", ["NOBODY"], code="connection_not_granted")
    before_local = env_local.read_text()

    moved = lab.ok("secrets", "move")
    assert moved["moved"] == [{"name": "MOVED_TOKEN", "readers": ["fresh"],
                               "files": [str(env_local), str(lab.project / ".env")]},
                              {"name": "OPEN_QUOTE", "readers": ["fresh"],
                               "files": [str(env_local)]}]
    reasons = {k["name"]: k["reason"] for k in moved["kept"]}
    assert reasons["OLD_TOKEN"].startswith("stale could not read it from the vault")
    assert reasons["NOBODY"] == "no installed capability's connections read it here"
    assert reasons["SPLIT"] == ".env.local and .env hold different values for it"
    assert "read before the vault" in reasons[KEY_ENV]
    assert "read before the vault" in reasons["AGENTKIT_DB_URL"]
    assert moved["not_reading"] == [{"capability": "gated", "code": "connection_not_granted"}]
    assert moved["unread"] == []
    # The files keep every other line as it was; only the moved key's lines go.
    left = before_local.replace("MOVED_TOKEN='tok-moved'\n", "").replace(
        'OPEN_QUOTE="tok-open\n', "")
    assert env_local.read_text() == left
    assert (lab.project / ".env").read_text() == "SPLIT=b-two\n"
    assert lab.run("secrets", "show", "MOVED_TOKEN").stdout == "tok-moved\n"
    # The vault holds the value its reader read, not the line's text.
    assert lab.run("secrets", "show", "OPEN_QUOTE").stdout == "tok-open\n"
    # A key that stayed is not left in the vault.
    assert [s["name"] for s in lab.ok("secrets", "list")["secrets"]] == [
        "MOVED_TOKEN", "OPEN_QUOTE"]
    assert "tok-" not in json.dumps(moved)

    # Run again, nothing is left that may move, and nothing changes.
    again = lab.ok("secrets", "move")
    assert again["moved"] == [] and env_local.read_text() == left


def test_move_takes_a_capability_key_named_like_the_manager_and_keeps_the_managers_own(lab):
    lab.ok("key", "generate")
    env_local = lab.project / ".env.local"
    env_local.write_text(env_local.read_text() + "CAPABILITIES_MAIL_TOKEN=tok-mail\n"
                         "CAPABILITIES_TRACKER_TOKEN=tok-tracker\n"
                         "CAPABILITIES_PROJECT_ID_ROOT=" + str(lab.project) + "\n")
    assert "CAPABILITIES_PROJECT_ID=" in env_local.read_text()
    # Even a capability that names the manager's own settings does not move them.
    _reader(lab, "fresh", ["CAPABILITIES_MAIL_TOKEN", "CAPABILITIES_TRACKER_TOKEN",
                           "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                           "AGENTKIT_DB_URL", KEY_ENV])
    moved = lab.ok("secrets", "move")
    assert [m["name"] for m in moved["moved"]] == ["CAPABILITIES_MAIL_TOKEN",
                                                    "CAPABILITIES_TRACKER_TOKEN"]
    assert all(m["readers"] == ["fresh"] for m in moved["moved"])
    reasons = {k["name"]: k["reason"] for k in moved["kept"]}
    for name in ("CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                 "AGENTKIT_DB_URL", "AGENTKIT_DB_SCHEMA", KEY_ENV):
        assert "read before the vault" in reasons[name], name
    assert "CAPABILITIES_MAIL_TOKEN" not in env_local.read_text()
    assert lab.run("secrets", "show", "CAPABILITIES_MAIL_TOKEN").stdout == "tok-mail\n"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_move_stopped_during_its_trial_puts_the_lines_back(lab, signum):
    lab.ok("key", "generate")
    env_local = lab.project / ".env.local"
    env_local.write_text(env_local.read_text() + "OLD_TOKEN=tok-old-12345\n")
    before = env_local.read_text()
    paused = lab.tmp / "paused"
    _reader(lab, "stale", ["OLD_TOKEN"], vault=False, pause=str(paused))
    run = subprocess.Popen([sys.executable, str(MANAGER), "secrets", "move"],
                           cwd=lab.project, env=lab.env(), text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    while not paused.exists() and run.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert paused.exists() and "OLD_TOKEN" not in env_local.read_text()
    run.send_signal(signum)
    # The lines are back as soon as the stop arrives, before the run has ended.
    deadline = time.monotonic() + 10
    while env_local.read_text() != before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert env_local.read_text() == before
    run.communicate(timeout=120)
    assert run.returncode == 128 + signum


def test_move_keeps_a_key_the_vault_holds_otherwise_and_every_key_when_a_report_fails(lab):
    lab.ok("key", "generate")
    env_local = lab.project / ".env.local"
    env_local.write_text(env_local.read_text() + "API_TOKEN=tok-file\n")
    _fed(lab, "tok-vault", "secrets", "set", "API_TOKEN")
    _reader(lab, "fresh", ["API_TOKEN"])
    kept = lab.ok("secrets", "move")
    assert kept["moved"] == [] and {k["name"]: k["reason"] for k in kept["kept"]}[
        "API_TOKEN"] == "the vault already holds a different value for it"
    assert lab.run("secrets", "show", "API_TOKEN").stdout == "tok-vault\n"
    broken = lab.tmp / "registry" / "broken" / "broken"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    lab.ok("secrets", "rm", "API_TOKEN")
    blocked = lab.ok("secrets", "move")
    assert blocked["moved"] == [] and blocked["unread"] == [
        {"capability": "broken", "problem": "connections exited 1"}]
    assert "whether it is read is not known" in {
        k["name"]: k["reason"] for k in blocked["kept"]}["API_TOKEN"]
    assert "API_TOKEN=tok-file" in env_local.read_text()
    assert lab.ok("secrets", "list")["secrets"] == []

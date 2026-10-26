"""The agent's vault and its key: `capabilities key generate|set|change` and
`capabilities secrets list|show|set|rm|raw`, against the store of a project that
exists only for the test.

These tests drive the real manager against the throwaway PostgreSQL of
test_manager_agents, which skips without `initdb`, `pg_ctl` or psycopg.
"""

from __future__ import annotations

import json
import subprocess
import sys

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
                 ("secrets", "raw", "--set"), ("secrets", "set", "API_TOKEN")):
        result = _feed(lab, "X=1", *args, CAPABILITIES_READ_ONLY="1")
        code, error = _error(result)
        assert (code, error["code"]) == (4, "read_only_switch"), args
    result = lab.run("secrets", "show", "API_TOKEN", CAPABILITIES_READ_ONLY="1")
    assert result.stdout == "tok-1\n"

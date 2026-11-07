"""Progress on stderr, a clean JSON stdout, and the session map across calls.

The fake engines under fakes/ report a tool call, a command, a file change and
commentary beside the final answer, so what reaches stderr is read against
what the engine said. See _peer.py for how to run the suite.
"""

import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from _peer import SCRIPT, STORE_DSN, Lab, request, store_env


def _load_cli():
    """Load the CLI as a module so its helpers are exercised from their one home."""
    spec = importlib.util.spec_from_loader(
        "askproject_cli", SourceFileLoader("askproject_cli", str(SCRIPT)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


@pytest.fixture()
def lab(tmp_path):
    return Lab(tmp_path)


def _open_stdin_ask(lab, *extra):
    """An ask whose own stdin is a pipe nobody writes to or closes."""
    import subprocess
    import sys
    store_env(lab.env)
    read_fd, write_fd = os.pipe()
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(lab.target), "do the task", *extra],
            cwd=lab.caller, env=lab.env, text=True, capture_output=True,
            stdin=read_fd, timeout=60)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _stored(lab) -> list[tuple]:
    """The rows this lab's caller holds in askproject_sessions, read directly."""
    import psycopg
    with psycopg.connect(STORE_DSN) as conn:
        return conn.execute(
            "SELECT caller_root, node_id, target_path, engine, last_session_id, "
            "last_mode, last_profile, interactions, last_used_at, last_attempt "
            "FROM agentkit.askproject_sessions WHERE caller_root = %s AND agent_id IS NULL",
            (str(lab.caller.resolve()),)).fetchall()


def _here(lab) -> str:
    """The node id the caller's machine resolves for itself, minted in its
    config home."""
    return (Path(lab.env["XDG_CONFIG_HOME"]) / "capabilities" / "node-id").read_text().strip()


def test_a_session_is_kept_under_the_node_s_id(lab):
    declared = {"AGENT_NODE_ID": "6c61622d6e6f"}
    first, result, _ = lab.ask("--engine", "codex", env=declared)
    assert first.returncode == 0, first.stderr
    [row] = _stored(lab)
    assert row[1] == "6c61622d6e6f"
    second, again, _ = lab.ask("-c", env=declared)
    assert second.returncode == 0, second.stderr
    assert (again["resumed"], again["session_id"]) == (True, result["session_id"])


def test_a_second_question_resumes_the_session_the_first_recorded_in_the_store(lab):
    first, result, _ = lab.ask("--engine", "codex")
    assert first.returncode == 0, first.stderr
    session = result["session_id"]

    [row] = _stored(lab)
    assert row[:7] == (str(lab.caller.resolve()), _here(lab),
                       str(lab.target), "codex", session, "read", "codex-read")
    assert row[7] == 1 and row[8] is not None
    assert row[9]["status"] == "completed" and row[9]["session_id"] == session

    second, result, launch = lab.ask("-c")
    assert second.returncode == 0, second.stderr
    assert (result["resumed"], result["session_id"]) == (True, session)
    assert request(launch, "thread/resume")["threadId"] == session
    assert _stored(lab)[0][7] == 2
    # Nothing is kept beside the project any more.
    assert not (lab.caller / "capabilities" / "askproject" / "state").exists()


def test_without_a_store_an_ask_refuses_before_any_peer_starts(lab):
    proc = lab.run(str(lab.target), "what is here?", "--quiet")
    assert proc.returncode == 6, proc.stdout + proc.stderr
    error = json.loads(proc.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "store_not_configured"
    assert "capabilities store set" in error["hint"]
    assert not lab.record.exists()
    assert not (lab.caller / "capabilities" / "askproject" / "state").exists()

    listed = lab.run("targets", "--json")
    assert listed.returncode == 6
    assert json.loads(listed.stderr)["error"]["code"] == "store_not_configured"

    doctor = lab.run("doctor")
    assert doctor.returncode == 2
    report = json.loads(doctor.stdout)
    assert (report["ok"], report["store"], report["store_error"]["code"]) == (
        False, None, "store_not_configured")


def _machine_file(lab, *, port: int | None = None) -> Path:
    """The lab's machine store setting, naming the test database by its fields,
    or the same host on a port nothing listens on."""
    from urllib.parse import parse_qs, urlparse
    store_env({})  # skips without a test database
    url = urlparse(STORE_DSN)
    body = {"schema": "agentkit.store.v1", "host": url.hostname,
            "port": port or url.port or 5432, "database": url.path.lstrip("/"),
            "user": url.username,
            "sslmode": (parse_qs(url.query).get("sslmode") or ["require"])[-1]}
    if url.password:
        body["password"] = url.password
    path = lab.config / "agentkit" / "store.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


def test_without_a_project_database_the_machine_file_answers(lab):
    machine = _machine_file(lab)
    report = json.loads(lab.run("doctor").stdout)
    assert report["store_error"] is None, report
    assert (report["store"]["level"], report["store"]["sources"]) == (
        "machine", [str(machine)])


def test_a_database_in_the_project_env_wins_over_the_machine_file(lab):
    _machine_file(lab, port=1)
    lab.dotenv(".env", {"AGENTKIT_DB_URL": STORE_DSN})
    report = json.loads(lab.run("doctor").stdout)
    assert report["store_error"] is None, report
    assert (report["store"]["level"], report["store"]["sources"]) == (
        "project", [str(lab.caller.resolve() / ".env")])

    proc = lab.run(str(lab.target), "what is here?", "--engine", "codex", "--quiet")
    assert proc.returncode == 0, proc.stderr
    [row] = _stored(lab)
    assert row[3] == "codex"


def test_codex_progress_is_concise_and_stdout_stays_json(lab):
    proc, result, _ = lab.ask("--engine", "codex", quiet=False,
                              env={"FAKE_ANSWER": "FINAL ANSWER MUST NOT BE PROGRESS"})

    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert result["answer"] == "FINAL ANSWER MUST NOT BE PROGRESS"
    assert result["session_id"] == "codex-thread"

    assert "askproject[codex] starting: Launching codex peer with profile codex-read (shipped)" in proc.stderr
    assert "askproject[codex] started: Codex peer started" in proc.stderr
    assert "askproject[codex] update: I will run the focused checks now." in proc.stderr
    assert "askproject[codex] verify: Running tests" in proc.stderr
    assert "askproject[codex] edit: Updated project files" in proc.stderr
    assert "askproject[codex] completed: Codex peer finished" in proc.stderr
    assert "FINAL ANSWER MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/project/test_secret.py" not in proc.stderr
    assert "/private/changed.py" not in proc.stderr
    assert "sensitive command output" not in proc.stderr


def test_claude_progress_uses_stream_events_without_echoing_answer(lab):
    proc, result, _ = lab.ask(quiet=False,
                              env={"FAKE_ANSWER": "CLAUDE FINAL MUST NOT BE PROGRESS"})

    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert result["answer"] == "CLAUDE FINAL MUST NOT BE PROGRESS"

    assert "askproject[claude] started: Claude peer started" in proc.stderr
    assert "askproject[claude] update: I will inspect the relevant module." in proc.stderr
    assert "askproject[claude] inspect: Inspecting the project" in proc.stderr
    assert "askproject[claude] completed: Claude peer finished" in proc.stderr
    assert "CLAUDE FINAL MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/code.py" not in proc.stderr


def test_quiet_keeps_stderr_silent(lab):
    for engine in ("claude", "codex"):
        proc, result, _ = lab.ask("--engine", engine)
        assert proc.returncode == 0, proc.stderr
        assert result["answer"] == "PEER ANSWER"
        assert proc.stderr == ""


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("extra", [(), ("--quiet",)])
def test_an_inherited_open_stdin_does_not_hold_the_peer(lab, engine, extra):
    proc = _open_stdin_ask(lab, "--engine", engine, *extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["answer"] == "PEER ANSWER"


def test_unknown_effort_fails_with_a_controlled_error(lab):
    proc, result, launch = lab.ask("--effort", "turbo")

    assert proc.returncode == 1
    assert result["ok"] is False
    assert "turbo" in result["error"]
    for level in CLI.EFFORT_LEVELS:
        assert level in result["error"]
    assert launch is None


def test_claude_aliases_resolve_to_full_ids(lab):
    proc, result, _ = lab.ask("--model", "haiku", "--effort", "low")
    assert proc.returncode == 0, proc.stderr
    assert (result["model"], result["effort"]) == (CLI.MODEL_ALIASES["haiku"], "low")


def test_an_act_thread_resumes_only_when_the_follow_up_names_its_mode(lab):
    timed_out, result, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                                   env={"PEER_SLEEP": "5",
                                        "FAKE_THREAD_ID": "codex-timeout-thread"})
    assert timed_out.returncode == 1
    assert "resume it with -c" in result["error"]

    bare, result, launch = lab.ask("-c")
    assert bare.returncode == 1
    assert "the last session used act" in result["error"]
    assert "--act" in result["error"] and "--read" in result["error"]
    assert launch is None

    resumed, result, launch = lab.ask("-c", "--act")
    assert resumed.returncode == 0, resumed.stderr
    assert (result["mode"], result["resumed"]) == ("act", True)
    resume = next(r for r in launch["requests"] if r["method"] == "thread/resume")
    assert resume["params"]["threadId"] == "codex-timeout-thread"


def test_read_resumes_an_act_thread_in_read_mode(lab):
    timed_out, _, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                              env={"PEER_SLEEP": "5", "FAKE_THREAD_ID": "codex-timeout-thread"})
    assert timed_out.returncode == 1

    resumed, result, launch = lab.ask("-c", "--read")
    assert resumed.returncode == 0, resumed.stderr
    assert (result["mode"], result["resumed"], result["profile"]["name"]) == (
        "read", True, "codex-read")
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"


def test_act_and_read_together_are_refused(lab):
    proc, result, launch = lab.ask("--act", "--read")
    assert proc.returncode == 1
    assert "contradictory" in result["error"]
    assert launch is None


def test_timeout_without_session_id_does_not_fall_back_to_older_session(lab):
    completed, _, _ = lab.ask("--engine", "codex")
    assert completed.returncode == 0, completed.stderr

    timed_out, _, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                              env={"PEER_SLEEP_BEFORE": "5"})
    assert timed_out.returncode == 1

    resumed, result, _ = lab.ask("-c")
    assert resumed.returncode == 1
    assert "timed out before its session id was observed" in result["error"]
    assert "without -c" in result["error"]


def test_a_store_at_major_1_keeps_its_sessions_under_agent_id(lab, monkeypatch):
    """A store whose askproject tables are at 1.0, with sessions in them, is
    refused outside the store upgrade and migrated in place by the next call
    inside it: every row stays, an agent caller's column is agent_id, a caller
    with no id is named by its root, nothing is named for project_id or host,
    and the caller's own session is still found."""
    import secrets

    import psycopg
    from capabilities_contract import db

    store_env(lab.env)
    schema = "askproject_m1_" + secrets.token_hex(4)
    caller, node = f"path:{lab.caller.resolve()}", "6c61622d6e6f"
    for key in [k for k in os.environ if k.startswith("AGENTKIT_DB_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENTKIT_DB_URL", STORE_DSN)
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", schema)
    conn = db.connect(application_name="test", setting=db.resolve_setting(None))
    try:
        db.migrate(conn, "askproject", CLI.SESSIONS_STEPS[:1], major=1, minor=0)
        conn.execute(
            "INSERT INTO askproject_sessions (project_id, host, target_path, engine, "
            "last_session_id, interactions) VALUES (%s, %s, %s, 'codex', 's-1', 3), "
            "('prj_000000000002', 'elsewhere', '/t/other', 'claude', 's-2', 1)",
            (caller, node, str(lab.target)))
        conn.commit()
    finally:
        conn.close()
    try:
        refused = lab.run("targets", "--json",
                          env={"AGENTKIT_DB_SCHEMA": schema, "AGENT_NODE_ID": node})
        assert refused.returncode != 0 and "store_upgrade_needed" in refused.stderr, refused
        listed = lab.run("targets", "--json",
                         env={"AGENTKIT_DB_SCHEMA": schema, "AGENT_NODE_ID": node,
                              db.UPGRADE_ENV: "1"})
        assert listed.returncode == 0, listed.stderr
        [entry] = json.loads(listed.stdout)["targets"]
        assert (entry["target"], entry["last_session_id"], entry["interactions"]) == (
            str(lab.target), "s-1", 3)
        with psycopg.connect(STORE_DSN) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            assert conn.execute(
                "SELECT agent_id, caller_root, node_id, target_path, last_session_id, "
                "interactions FROM askproject_sessions ORDER BY last_session_id").fetchall() == [
                (None, str(lab.caller.resolve()), node, str(lab.target), "s-1", 3),
                ("prj_000000000002", None, "elsewhere", "/t/other", "s-2", 1)]
            assert conn.execute("SELECT major, minor FROM schema_version "
                                "WHERE owner = 'askproject'").fetchone() == (3, 0)
            names = [r[0] for r in conn.execute(
                "SELECT attname::text FROM pg_attribute WHERE attrelid = "
                "'askproject_sessions'::regclass AND attnum > 0 UNION ALL "
                "SELECT conname::text FROM pg_constraint WHERE conrelid = "
                "'askproject_sessions'::regclass UNION ALL "
                "SELECT indexname::text FROM pg_indexes WHERE schemaname = current_schema()"
            ).fetchall()]
            assert "agent_id" in names and not [n for n in names if "project_id" in n], names
            assert "node_id" in names and "host" not in names, names
    finally:
        with psycopg.connect(STORE_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_a_store_at_2_0_crosses_to_node_id_and_minus_c_continues_for_both_callers(
        lab, monkeypatch):
    """A store whose askproject tables are at 2.0 crosses to 3.0 only inside the
    store upgrade: the rows as they stood are copied whole into
    askproject_premig_m7, `host` becomes `node_id`, a `path:` caller becomes a
    null agent id with its root beside it, a target that is an agent's folder on
    that node gets that agent's id, and -c continues the same session for an
    agent caller and for a caller with no id; a new write names its target's
    agent too."""
    import secrets

    import psycopg
    from capabilities_contract import db

    store_env(lab.env)
    schema = "askproject_m2_" + secrets.token_hex(4)
    node, agent, target_agent = "6c61622d6e6f", "prj_aaaaaaaaaaaa", "bbbbbbbbbbbb"
    root, target = str(lab.caller.resolve()), str(lab.target.resolve())
    other = lab.root / "other"
    other.mkdir()
    for key in [k for k in os.environ if k.startswith("AGENTKIT_DB_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENTKIT_DB_URL", STORE_DSN)
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", schema)
    conn = db.connect(application_name="test", setting=db.resolve_setting(None))
    try:
        db.migrate(conn, "askproject", CLI.SESSIONS_STEPS[:2], major=2, minor=0)
        conn.execute("INSERT INTO agents (id) VALUES (%s), (%s)", (agent, target_agent))
        conn.execute("INSERT INTO nodes (id, label, kind) VALUES (%s, 'lab', 'server')",
                     (node,))
        conn.execute("INSERT INTO agent_nodes (agent_id, node, folder) VALUES (%s, %s, %s)",
                     (target_agent, node, target))
        conn.execute(
            "INSERT INTO askproject_sessions (agent_id, host, target_path, engine, "
            "last_session_id, interactions) VALUES "
            "(%s, %s, %s, 'codex', 's-path', 2), (%s, %s, %s, 'codex', 's-agent', 5), "
            "(%s, %s, %s, 'claude', 's-other', 1)",
            (f"path:{root}", node, target, agent, node, target, agent, node, str(other)))
        conn.commit()
    finally:
        conn.close()
    as_agent = {"CAPABILITIES_PROJECT_ID": agent, "CAPABILITIES_PROJECT_ID_ROOT": root}
    base = {"AGENTKIT_DB_SCHEMA": schema, "AGENT_NODE_ID": node}
    try:
        refused = lab.run("targets", "--json", env=base)
        assert refused.returncode != 0 and "store_upgrade_needed" in refused.stderr, refused
        crossed = lab.run("targets", "--json", env={**base, db.UPGRADE_ENV: "1"})
        assert crossed.returncode == 0, crossed.stderr
        with psycopg.connect(STORE_DSN) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            assert conn.execute("SELECT major, minor FROM schema_version "
                                "WHERE owner = 'askproject'").fetchone() == (3, 0)
            assert conn.execute(
                "SELECT agent_id, host, target_path, last_session_id FROM "
                "askproject_premig_m7 ORDER BY last_session_id").fetchall() == [
                (agent, node, target, "s-agent"), (agent, node, str(other), "s-other"),
                (f"path:{root}", node, target, "s-path")]
            assert conn.execute(
                "SELECT agent_id, caller_root, node_id, target_path, target_agent_id, "
                "last_session_id, interactions FROM askproject_sessions "
                "ORDER BY last_session_id").fetchall() == [
                (agent, None, node, target, target_agent, "s-agent", 5),
                (agent, None, node, str(other), None, "s-other", 1),
                (None, root, node, target, target_agent, "s-path", 2)]
        for env, session in ((as_agent, "s-agent"), ({}, "s-path")):
            proc, result, launch = lab.ask("-c", env={**base, **env})
            assert proc.returncode == 0, proc.stderr
            assert (result["resumed"], result["session_id"]) == (True, session)
            assert request(launch, "thread/resume")["threadId"] == session
        fresh, _, _ = lab.ask("--engine", "codex", env={**base, **as_agent,
                                                       "AGENT_NODE_ID": "6c61622d6e70"})
        assert fresh.returncode == 0, fresh.stderr
        with psycopg.connect(STORE_DSN) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            assert conn.execute(
                "SELECT target_path, interactions FROM askproject_sessions WHERE "
                "agent_id = %s AND node_id = %s ORDER BY target_path",
                (agent, node)).fetchall() == [(str(other), 1), (target, 6)]
            assert conn.execute(
                "SELECT interactions FROM askproject_sessions WHERE caller_root = %s",
                (root,)).fetchall() == [(3,)]
            assert conn.execute(
                "SELECT target_agent_id FROM askproject_sessions WHERE node_id = "
                "'6c61622d6e70'").fetchall() == [(None,)]
            conn.execute("INSERT INTO nodes (id, label, kind) VALUES "
                         "('6c61622d6e70', 'lab2', 'server')")
            conn.execute("INSERT INTO agent_nodes (agent_id, node, folder) VALUES "
                         "(%s, '6c61622d6e70', %s)", (target_agent, target))
            conn.commit()
        again, _, _ = lab.ask("-c", env={**base, **as_agent, "AGENT_NODE_ID": "6c61622d6e70"})
        assert again.returncode == 0, again.stderr
        with psycopg.connect(STORE_DSN) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            assert conn.execute(
                "SELECT target_agent_id FROM askproject_sessions WHERE node_id = "
                "'6c61622d6e70'").fetchall() == [(target_agent,)]
    finally:
        with psycopg.connect(STORE_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')

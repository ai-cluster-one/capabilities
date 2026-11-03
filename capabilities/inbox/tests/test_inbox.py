"""inbox's table and verbs, against a throwaway PostgreSQL.

The module starts a cluster on a loopback port when `initdb` and `pg_ctl` are
on PATH and psycopg is importable, and skips otherwise; each test gets a
database of its own and two projects, two agents, that exist only for the
test. The CLI runs as a child with a home of its own, so nothing reaches the
real one.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "inbox"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "box"
ALPHA, BETA = "prj_" + "a1a1a1a1a1a1", "prj_" + "b2b2b2b2b2b2"
CLEARED_PREFIXES = ("AGENTKIT_DB_", "CAPABILITIES_", "CLAUDE", "CODEX", "INBOX_")
TEST_NODE = "746573742d6e"
NASTY = "$(touch pwned); rm -rf x; \"' `id`"


# --- a throwaway PostgreSQL ------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.port = _free_port()
        self.env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(self, *argv: str) -> None:
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True,
                                timeout=120)
        assert result.returncode == 0, (argv, result.stdout + result.stderr)

    def start(self) -> None:
        self.run("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                 "--locale=en_US.UTF-8", "--auth=trust")
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       "unix_socket_directories = ''\n")
        self.run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "pg.log"),
                 "-w", "-t", "60", "start")

    def stop(self) -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "stop"],
                       env=self.env, capture_output=True, timeout=60)

    def connect(self, database: str = "postgres"):
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                               dbname=database, connect_timeout=10)
        conn.autocommit = True
        return conn

    def database(self) -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with self.connect() as conn:
            conn.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
        return name

    def url(self, database: str) -> str:
        return f"postgresql://{ADMIN}@127.0.0.1:{self.port}/{database}?sslmode=disable"


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-inbox-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp: Path, cluster: Cluster):
        self.tmp = tmp
        self.cluster = cluster
        self.database = cluster.database()
        self.alpha = self._project("alpha", ALPHA)
        self.beta = self._project("beta", BETA)
        self.services: list[subprocess.Popen] = []
        self.sessions: dict[Path, str] = {}

    def _project(self, slug: str, ident: str) -> Path:
        root = self.tmp / slug
        (root / ".git").mkdir(parents=True)
        envelope = root / "capabilities"
        envelope.mkdir()
        (envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": slug, "id": ident}))
        (envelope / "settings.json").write_text(json.dumps(
            {"capabilities": {"inbox": {"enabled": True}}}))
        return root

    def session(self, started_in: Path, harness: str = "claude") -> str:
        """A new session of `harness`, kept in its own session store in the
        test's home as started in `started_in`."""
        session = str(uuid.uuid4())
        home = self.tmp / "home"
        if harness == "claude":
            transcript = (home / ".claude" / "projects" / str(started_in).replace("/", "-")
                          / f"{session}.jsonl")
            entries = [{"type": "queue-operation", "sessionId": session},
                       {"type": "user", "cwd": str(started_in), "sessionId": session}]
        else:
            transcript = (home / ".codex" / "sessions" / "2026" / "11" / "03"
                          / f"rollout-2026-11-03T10-00-00-{session}.jsonl")
            entries = [{"type": "session_meta", "payload": {"id": session,
                                                             "cwd": str(started_in)}}]
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return session

    def own(self, where: Path) -> str:
        """The Claude Code session each command in `where` runs in by default."""
        if where not in self.sessions:
            self.sessions[where] = self.session(where)
        return self.sessions[where]

    def env(self, **extra: str | None) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEARED_PREFIXES)}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CAPABILITIES_MANAGER_BIN": str(MANAGER),
            "AGENTKIT_DB_URL": self.cluster.url(self.database),
            "AGENTKIT_DB_SCHEMA": SCHEMA,
            "AGENT_NODE_ID": TEST_NODE,
            "USER": "tester",
            **extra,
        })
        return {k: v for k, v in env.items() if v is not None}

    def run(self, where: Path, *args: str, **extra: str | None):
        extra = {"CLAUDE_CODE_SESSION_ID": self.own(where), **extra}
        return subprocess.run([sys.executable, str(CLI), *args], cwd=where,
                              env=self.env(**extra), text=True, capture_output=True,
                              timeout=180)

    def ok(self, where: Path, *args: str, **extra):
        result = self.run(where, *args, **extra)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def refused(self, where: Path, *args: str, **extra) -> tuple[int, dict]:
        result = self.run(where, *args, **extra)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.startswith('{"error"'))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None

    def ask(self, where: Path, title: str = "Ship it?", *more: str, **extra) -> dict:
        return self.ok(where, "ask", title, "--text", "The release is ready.", "--yes",
                       "Ship now", "--no", "Hold", "--recommend", "yes", *more, **extra)["item"]

    def serve(self, where: Path) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, str(CLI), "service", "run"], cwd=where,
                                 env=self.env(), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.services.append(child)
        return child

    def close(self) -> None:
        for child in self.services:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)


@pytest.fixture()
def lab(tmp_path, cluster):
    made = Lab(tmp_path, cluster)
    try:
        yield made
    finally:
        made.close()


def _until(check, seconds: float = 60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError("the condition did not hold in time")


# --- the table -------------------------------------------------------------------

def test_the_ledger_creates_the_table_its_comments_and_raises_the_agent_s_version(lab):
    item = lab.ask(lab.alpha)
    assert lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.inbox_items",))[0][0]
    columns = [r[0] for r in lab.sql(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = 'inbox_items' ORDER BY ordinal_position", (SCHEMA,))]
    assert columns[:2] == ["agent_id", "id"]
    assert {"delivery_lease_until", "delivery_due_at", "withdrawn_at"} <= set(columns)
    version = lab.sql(f"SELECT version FROM {SCHEMA}.schema_channel WHERE channel = "
                      "'inbox_changed' AND scope = %s", (ALPHA,))[0][0]
    lab.ok(lab.alpha, "read", item["id"])
    assert lab.sql(f"SELECT version FROM {SCHEMA}.schema_channel WHERE channel = "
                   "'inbox_changed' AND scope = %s", (ALPHA,))[0][0] == version + 1


# --- asking ----------------------------------------------------------------------

def test_the_three_kinds_and_a_key_that_makes_a_repeat_one_item(lab):
    first = lab.ok(lab.alpha, "ask", "Ship it?", "--text", "t", "--yes", "Ship", "--no",
                   "Hold", "--recommend", "no", "--key", "rel-1", "--on-answer", "true")
    again = lab.ok(lab.alpha, "ask", "Other words", "--text", "u", "--yes", "a", "--no", "b",
                   "--recommend", "yes", "--key", "rel-1")
    assert first["created"] and not again["created"]
    assert again["item"]["id"] == first["item"]["id"]
    assert again["item"]["title"] == "Ship it?" and again["item"]["recommend"] == "no"
    # The key is the agent's own: another agent's same key is another item.
    other = lab.ok(lab.beta, "notify", "Hello", "--text", "t", "--key", "rel-1")
    assert other["created"] and other["item"]["agent"] == BETA
    talk = lab.ok(lab.alpha, "talk", "Which way?", "--text", "t")["item"]
    assert talk["kind"] == "talk" and talk["session"] == lab.own(lab.alpha) and "yes" not in talk
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.inbox_items")[0][0] == 3
    code, error = lab.refused(lab.alpha, "ask", "Two\nlines", "--text", "t", "--yes", "a",
                              "--no", "b", "--recommend", "yes")
    assert (code, error["code"]) == (6, "input")
    code, _error = lab.refused(lab.alpha, "notify", "x", "--text", "t", "--on-answer", "true")
    assert code == 6


def test_every_kind_records_the_session_it_runs_in_and_its_node(lab):
    mine = lab.own(lab.alpha)
    asked = lab.ask(lab.alpha)
    talked = lab.ok(lab.alpha, "talk", "Which way?", "--text", "t")["item"]
    noted = lab.ok(lab.alpha, "notify", "FYI", "--text", "t")["item"]
    for item in (asked, talked, noted):
        assert (item["session"], item["node"]) == (mine, TEST_NODE)
        for read in (lab.ok(lab.alpha, "show", item["id"]),
                     lab.ok(lab.alpha, "status", item["id"])):
            assert (read["session"], read["node"]) == (mine, TEST_NODE)
    listed = lab.ok(lab.alpha, "list")["items"]
    assert {(i["kind"], i["session"], i["node"]) for i in listed} == {
        (kind, mine, TEST_NODE) for kind in ("ask", "talk", "notify")}
    # --session names another session of the same agent, on an ask as on a talk;
    # one started in a folder inside the project is the project's.
    (lab.alpha / "docs").mkdir()
    other, inside = lab.session(lab.alpha), lab.session(lab.alpha / "docs")
    assert lab.ask(lab.alpha, "Other?", "--session", other)["session"] == other
    assert lab.ok(lab.alpha, "talk", "x", "--text", "t",
                  "--session", inside)["item"]["session"] == inside
    # A Codex session is named by CODEX_THREAD_ID and kept in its rollout.
    codex = lab.session(lab.alpha, harness="codex")
    item = lab.ask(lab.alpha, "From codex?", CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID=codex)
    assert (item["session"], item["node"]) == (codex, TEST_NODE)
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.inbox_items WHERE node = %s",
                   (TEST_NODE,))[0][0] == 6


def test_a_session_that_is_not_this_agent_s_is_refused(lab):
    theirs = lab.own(lab.beta)
    for args in (("ask", "Ship?", "--text", "t", "--yes", "a", "--no", "b", "--recommend",
                  "yes", "--session", theirs),
                 ("talk", "x", "--text", "t", "--session", theirs),
                 ("talk", "x", "--text", "t", "--session", "no-such-session")):
        code, error = lab.refused(lab.alpha, *args)
        assert (code, error["code"]) == (6, "foreign_session"), args
    # The session the command runs in is held to the same proof, for every kind.
    for args in (("talk", "x", "--text", "t", "--on-answer", "true"),
                 ("notify", "x", "--text", "t")):
        code, error = lab.refused(lab.alpha, *args, CLAUDE_CODE_SESSION_ID=theirs)
        assert (code, error["code"]) == (6, "foreign_session"), args
    codex = lab.session(lab.beta, harness="codex")
    code, error = lab.refused(lab.alpha, "notify", "x", "--text", "t",
                              CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID=codex)
    assert (code, error["code"]) == (6, "foreign_session")
    # A folder inside the project that is a project of its own is that project.
    nested = lab.alpha / "nested"
    (nested / "capabilities").mkdir(parents=True)
    (nested / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "nested", "id": "prj_" + "c3" * 6}))
    code, error = lab.refused(lab.alpha, "talk", "x", "--text", "t",
                              "--session", lab.session(nested))
    assert (code, error["code"]) == (6, "foreign_session")
    assert lab.ok(lab.alpha, "list", "--all-agents")["count"] == 0


def test_a_question_outside_any_session_needs_an_on_answer_command(lab):
    for args in (("ask", "Ship?", "--text", "t", "--yes", "a", "--no", "b", "--recommend", "yes"),
                 ("talk", "x", "--text", "t")):
        code, error = lab.refused(lab.alpha, *args, CLAUDE_CODE_SESSION_ID=None)
        assert (code, error["code"]) == (6, "no_session"), args
    note = lab.ok(lab.alpha, "notify", "FYI", "--text", "t", CLAUDE_CODE_SESSION_ID=None)["item"]
    assert (note["session"], note["node"]) == (None, TEST_NODE)
    # What tasks puts in, run by no session: an ask and a talk that leave its command.
    tid, key = str(uuid.uuid4()), f"tasks:{uuid.uuid4()}:{uuid.uuid4()}"
    on_answer = f"tasks answer '{tid}' --key '{key}'"
    asked = lab.ok(lab.alpha, "ask", "Release it?", "--text", "t", "--yes", "Release",
                   "--no", "Hold", "--recommend", "yes", "--key", key, "--on-answer", on_answer,
                   CLAUDE_CODE_SESSION_ID=None)
    lab.ok(lab.alpha, "withdraw", "--key", key)
    talked = lab.ok(lab.alpha, "talk", "Release it?", "--text", "t", "--key", key,
                    "--on-answer", on_answer, CLAUDE_CODE_SESSION_ID=None)
    for put in (asked, talked):
        assert put["created"] and put["item"]["on_answer"] == on_answer
        assert (put["item"]["session"], put["item"]["node"]) == (None, TEST_NODE)


def test_a_table_put_in_before_sessions_and_nodes_migrates_and_keeps_its_items(lab):
    loader = (
        "import importlib.machinery, importlib.util, sys\n"
        "loader = importlib.machinery.SourceFileLoader('inbox_cli', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('inbox_cli', loader))\n"
        "loader.exec_module(mod)\n"
        "db = mod._db()\n"
        "conn = db.connect(application_name='inbox', transaction=True,\n"
        "                  setting=db.resolve_setting(mod._project_root()))\n"
        "db.migrate(conn, 'inbox', mod.LEDGER_STEPS[:3], major=1, minor=0)\n"
        "conn.commit()\n")
    made = subprocess.run([sys.executable, "-c", loader, str(CLI)], cwd=lab.alpha,
                          env=lab.env(), text=True, capture_output=True, timeout=180)
    assert made.returncode == 0, made.stderr
    old = lab.sql(f"INSERT INTO {SCHEMA}.inbox_items (agent_id, kind, title, session, created_by) "
                  "VALUES (%s, 'talk', 'Old talk', 'old-session', 'tester@old') RETURNING id",
                  (ALPHA,))[0][0]
    has_node = ("SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'inbox_items' AND column_name = 'node'")
    assert lab.sql(has_node, (SCHEMA,))[0][0] == 0
    lab.ok(lab.alpha, "migrate")
    assert lab.sql(has_node, (SCHEMA,))[0][0] == 1
    kept = lab.ok(lab.alpha, "show", str(old))
    assert (kept["session"], kept["node"]) == ("old-session", None)
    assert lab.ok(lab.alpha, "read", str(old))["items"][0]["changed"] is True
    assert lab.ask(lab.alpha)["node"] == TEST_NODE
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation, match="never changed"):
        lab.sql(f"UPDATE {SCHEMA}.inbox_items SET node = 'x' WHERE id = %s", (old,))


def test_withdraw_takes_the_question_back_frees_its_key_and_spares_an_answer(lab):
    item = lab.ask(lab.alpha, "Ship it?", "--key", "k")
    gone = lab.ok(lab.alpha, "withdraw", "--key", "k")
    assert gone["changed"] and gone["item"]["withdrawn_at"]
    assert lab.ok(lab.alpha, "withdraw", item["id"])["changed"] is False
    assert lab.refused(lab.alpha, "withdraw", "--key", "k")[0] == 3
    assert lab.ok(lab.alpha, "list")["count"] == 0
    assert lab.ok(lab.alpha, "list", "--view", "archive")["count"] == 0
    assert lab.refused(lab.alpha, "answer", item["id"], "yes")[1]["code"] == "withdrawn"
    fresh = lab.ask(lab.alpha, "Ship it?", "--key", "k")
    assert fresh["id"] != item["id"]
    lab.ok(lab.alpha, "answer", fresh["id"], "no")
    assert lab.refused(lab.alpha, "withdraw", fresh["id"])[1]["code"] == "already_answered"
    # An asker reaches only its own agent's items.
    assert lab.refused(lab.beta, "status", fresh["id"])[0] == 3
    assert lab.run(lab.alpha, "withdraw", fresh["id"], CAPABILITIES_READ_ONLY="1").returncode == 4


# --- the person ------------------------------------------------------------------

def test_the_first_answer_is_final_and_the_store_refuses_changing_it(lab):
    item = lab.ask(lab.alpha)
    answered = lab.ok(lab.alpha, "answer", item["id"][:8], "yes", "--reply", "go")
    assert answered["answer"] == "yes" and answered["reply"] == "go" and answered["read"]
    assert answered["answered_by"] == f"tester@{TEST_NODE}"
    code, error = lab.refused(lab.alpha, "answer", item["id"], "no")
    assert (code, error["code"]) == (7, "already_answered")
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation, match="final"):
        lab.sql(f"UPDATE {SCHEMA}.inbox_items SET answer = 'no' WHERE id = %s", (item["id"],))
    with pytest.raises(psycopg.errors.CheckViolation, match="never changed"):
        lab.sql(f"UPDATE {SCHEMA}.inbox_items SET title = 'x' WHERE id = %s", (item["id"],))
    talk = lab.ok(lab.alpha, "talk", "How?", "--text", "t")["item"]
    assert lab.refused(lab.alpha, "answer", talk["id"], "yes")[0] == 6
    assert lab.refused(lab.alpha, "answer", talk["id"])[0] == 6
    assert lab.ok(lab.alpha, "answer", talk["id"], "--reply", "this way")["reply"] == "this way"
    note = lab.ok(lab.alpha, "notify", "FYI", "--text", "t")["item"]
    assert lab.refused(lab.alpha, "answer", note["id"], "yes")[1]["code"] == "not_answerable"


def test_list_shows_the_folder_s_agent_or_every_agent_in_views_filters_and_pages(lab):
    made = [lab.ask(lab.alpha, f"Question {n}") for n in range(5)]
    note = lab.ok(lab.beta, "notify", "From beta", "--text", "t")["item"]
    mine = lab.ok(lab.alpha, "list")
    assert mine["agent"] == ALPHA and mine["count"] == 5
    assert [i["title"] for i in mine["items"]] == [f"Question {n}" for n in range(4, -1, -1)]
    both = lab.ok(lab.alpha, "list", "--all-agents")
    assert both["count"] == 6 and both["items"][0]["id"] == note["id"]
    assert lab.ok(lab.alpha, "list", "--agent", BETA)["count"] == 1
    assert lab.ok(lab.alpha, "list", "--all-agents", "--kind", "notify")["count"] == 1
    assert lab.refused(lab.alpha, "list", "--agent", BETA, "--all-agents")[0] == 6
    page = lab.ok(lab.alpha, "list", "--limit", "2")
    seen = [i["id"] for i in page["items"]]
    while page["next"]:
        page = lab.ok(lab.alpha, "list", "--limit", "2", "--before", page["next"])
        seen += [i["id"] for i in page["items"]]
    assert seen == [i["id"] for i in reversed(made)]
    assert lab.ok(lab.alpha, "list", "--since", "1h")["count"] == 5
    assert lab.ok(lab.alpha, "list", "--since", "2999-01-01")["count"] == 0
    archived = lab.ok(lab.alpha, "archive", made[0]["id"], made[1]["id"])["items"]
    assert [a["changed"] for a in archived] == [True, True]
    assert lab.ok(lab.alpha, "archive", made[0]["id"])["items"][0]["changed"] is False
    assert lab.ok(lab.alpha, "list")["count"] == 3
    assert lab.ok(lab.alpha, "list", "--view", "archive")["count"] == 2
    lab.ok(lab.alpha, "unarchive", made[1]["id"])
    lab.ok(lab.alpha, "read", made[2]["id"], made[3]["id"])
    lab.ok(lab.alpha, "unread", made[3]["id"])
    assert lab.ok(lab.alpha, "show", made[2]["id"])["read"] is True
    counts = lab.ok(lab.alpha, "count")
    assert counts == {"agent": ALPHA, "inbox": 4, "unread": 3, "unanswered": 4, "archive": 1,
                      "undelivered": 0}
    assert lab.ok(lab.alpha, "count", "--all-agents")["inbox"] == 5


def test_status_and_wait_read_the_asker_s_own_answer(lab):
    item = lab.ask(lab.alpha, "Ship it?", "--key", "w")
    code, error = lab.refused(lab.alpha, "wait", "--key", "w", "--timeout", "1")
    assert (code, error["code"]) == (7, "not_answered")
    waiter = subprocess.Popen([sys.executable, str(CLI), "wait", item["id"], "--timeout", "60"],
                              cwd=lab.alpha, env=lab.env(), stdout=subprocess.PIPE, text=True)
    time.sleep(2)
    lab.ok(lab.alpha, "answer", item["id"], "no")
    out, _ = waiter.communicate(timeout=90)
    assert waiter.returncode == 0 and json.loads(out)["answer"] == "no"
    assert lab.ok(lab.alpha, "status", "--key", "w")["answer"] == "no"


# --- delivering ------------------------------------------------------------------

def test_a_reply_reaches_the_command_in_its_environment_and_is_never_executed(lab):
    command = 'printf "%s|%s|%s|%s\\n" "$INBOX_KIND" "$INBOX_ANSWER" "$INBOX_KEY" "$INBOX_REPLY" > got'
    item = lab.ask(lab.alpha, "Ship it?", "--key", "n", "--on-answer", command)
    lab.ok(lab.beta, "answer", item["id"], "yes", "--reply", NASTY)
    assert lab.ok(lab.alpha, "status", item["id"])["delivery"]["state"] == "undelivered"
    done = lab.ok(lab.alpha, "deliver")
    assert [d["id"] for d in done["delivered"]] == [item["id"]] and not done["failed"]
    assert (lab.alpha / "got").read_text() == f"ask|yes|n|{NASTY}\n"
    assert not (lab.alpha / "pwned").exists() and not (lab.beta / "pwned").exists()
    assert lab.ok(lab.alpha, "status", item["id"])["delivery"]["state"] == "delivered"
    # Nothing is due any more: a second run delivers nothing.
    assert lab.ok(lab.alpha, "deliver")["delivered"] == []


def test_a_failed_command_leaves_the_answer_undelivered_and_falls_due_again(lab):
    item = lab.ask(lab.alpha, "Ship it?", "--on-answer", "echo broken >&2; exit 3")
    lab.ok(lab.alpha, "answer", item["id"], "yes")
    result = lab.run(lab.alpha, "deliver")
    assert result.returncode == 7
    failed = json.loads(result.stdout)["failed"][0]
    assert failed["attempts"] == 1 and failed["error"] == "exit 3: broken"
    status = lab.ok(lab.alpha, "status", item["id"])
    assert status["answer"] == "yes" and status["delivery"]["state"] == "undelivered"
    assert lab.ok(lab.alpha, "count")["undelivered"] == 1
    # Not due yet: the next attempt waits.
    assert lab.ok(lab.alpha, "deliver")["failed"] == []
    lab.sql(f"UPDATE {SCHEMA}.inbox_items SET delivery_due_at = now() WHERE id = %s",
            (item["id"],))
    again = json.loads(lab.run(lab.alpha, "deliver").stdout)["failed"][0]
    assert again["attempts"] == 2
    waits = lab.sql(f"SELECT extract(epoch FROM delivery_due_at - now()) FROM {SCHEMA}.inbox_items "
                    "WHERE id = %s", (item["id"],))[0][0]
    assert 50 < float(waits) <= 60


def test_the_running_service_retries_a_failed_command_to_success(lab):
    command = ('n=$(cat tries 2>/dev/null || echo 0); n=$((n+1)); echo $n > tries; '
               '[ $n -ge 2 ] || { echo "not yet" >&2; exit 3; }')
    item = lab.ask(lab.alpha, "Ship it?", "--on-answer", command)
    service = lab.serve(lab.alpha)
    _until(lambda: lab.ok(lab.alpha, "service", "status")["project"]["state"] == "served")
    lab.ok(lab.alpha, "answer", item["id"], "no")

    def failed_once():
        delivery = lab.ok(lab.alpha, "status", item["id"])["delivery"]
        return delivery if delivery["attempts"] == 1 and delivery["error"] else None
    delivery = _until(failed_once)
    assert delivery["state"] == "undelivered" and delivery["error"] == "exit 3: not yet"
    assert lab.ok(lab.alpha, "status", item["id"])["answer"] == "no"
    # The service wakes on the change and finds the delivery due.
    lab.sql(f"UPDATE {SCHEMA}.inbox_items SET delivery_due_at = now() WHERE id = %s",
            (item["id"],))
    done = _until(lambda: (lambda d: d if d["state"] == "delivered" else None)(
        lab.ok(lab.alpha, "status", item["id"])["delivery"]))
    assert done["attempts"] == 2
    assert lab.ok(lab.alpha, "service", "doctor")["ok"] is True
    assert service.poll() is None

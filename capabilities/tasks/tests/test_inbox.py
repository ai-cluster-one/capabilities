#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract>=0.8,<1",
#                 "callva-harness-runner==0.10.2", "pyyaml>=6"]
# ///
"""A task waiting on a person, put before them in the Inbox.

A move that lands a task waiting on a person runs `inbox` once it has committed,
a move away withdraws the item, and the answer comes back through `tasks
answer`. The CLI runs as a child of each case in a project of its own, against a
schema of its own in the throwaway database TASKS_TEST_DSN names, with a fake
`inbox` on PATH that records how it was called and answers as the real one does,
and a fake manager, named by CAPABILITIES_MANAGER_BIN, that answers which names
are agents and capabilities and hands every other question to the manager on
PATH. Nothing reads the machine's store or registry.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

DSN = os.environ.get("TASKS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")

PERSON, OTHER_PERSON = "Alex Example", "Bea Example"

FAKE_INBOX = r'''
import json, os, sys, time, uuid
log = os.environ["FAKE_INBOX_LOG"]
with open(log, "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                          "agent": os.environ.get("CAPABILITIES_PROJECT_ID")}) + "\n")
mode = os.environ.get("FAKE_INBOX_MODE", "ok")
if mode == "hang":
    time.sleep(120)
if mode in ("fail", "gated"):
    sys.stderr.write(json.dumps({"error": {"code": "policy" if mode == "gated" else "store",
                                           "message": "refused"}}) + "\n")
    sys.exit(4 if mode == "gated" else 5)
path = log + ".state"
state = json.load(open(path)) if os.path.exists(path) else {"items": []}
verb, rest = sys.argv[1], sys.argv[2:]
def flag(name):
    return rest[rest.index(name) + 1] if name in rest else None
def save():
    json.dump(state, open(path, "w"))
if verb in ("ask", "talk"):
    key = flag("--key")
    for item in state["items"]:
        if key and item["key"] == key and not item["withdrawn"]:
            print(json.dumps({"created": False, "item": item}))
            sys.exit(0)
    item = {"id": uuid.uuid4().hex, "kind": verb, "title": rest[0], "text": flag("--text"),
            "yes": flag("--yes"), "no": flag("--no"), "recommend": flag("--recommend"),
            "key": key, "on_answer": flag("--on-answer"), "answered": False,
            "withdrawn": False}
    state["items"].append(item)
    save()
    print(json.dumps({"created": True, "item": item}))
elif verb == "withdraw":
    live = [one for one in state["items"] if one["key"] == flag("--key") and not one["withdrawn"]]
    if not live:
        sys.stderr.write('{"error": {"code": "not_found", "message": "no item"}}\n')
        sys.exit(3)
    if live[0]["answered"]:
        sys.stderr.write('{"error": {"code": "already_answered", "message": "answered"}}\n')
        sys.exit(6)
    live[0]["withdrawn"] = True
    save()
    print(json.dumps({"changed": True, "item": live[0]}))
else:
    sys.exit(6)
'''

FAKE_MANAGER = r'''
import json, os, sys
if os.environ.get("FAKE_MANAGER_MODE") == "fail":
    sys.exit(1)
args = sys.argv[1:]
if args == ["agent", "list"]:
    print(json.dumps({"ok": True, "agents": [
        {"id": "agt0000aaaa", "slug": "helper-agent", "name": "Robin", "nodes": []}]}))
elif args == ["list", "--json"]:
    print(json.dumps({"installed": [{"name": "clickup"}, {"name": "tasks"}]}))
elif args == ["search"]:
    print(json.dumps({"matches": [{"name": "notion", "installed": False}],
                      "unavailable_sources": []}))
else:
    # Every other question - where the project is - goes to the manager on PATH.
    os.execvp("capabilities", ["capabilities", *args])
'''


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def _path_without_inbox() -> list[str]:
    """This process's PATH without any folder holding an `inbox`, so the Inbox
    the case puts on PATH, or leaves off it, is the only one."""
    return [one for one in os.environ.get("PATH", "").split(os.pathsep)
            if one and not (Path(one) / "inbox").exists()]


@pytest.fixture
def lab(tmp_path):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    ident = "prj_" + uuid.uuid4().hex[:12]
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (project / ".env.local").write_text(f"CAPABILITIES_PROJECT_ID={ident}\n")
    _cli.write_store_setting(tmp_path / "config", schema)
    tools, bare = tmp_path / "tools", tmp_path / "bare"
    tools.mkdir()
    bare.mkdir()
    _script(tools / "inbox", FAKE_INBOX)
    for folder in (tools, bare):
        (folder / "tasks").symlink_to(_cli.CLI_PATH)
    manager = _script(tmp_path / "fake-capabilities", FAKE_MANAGER)
    env = os.environ.copy()
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                   "CAPABILITIES_STORE_MODE", "FAKE_INBOX_MODE", "FAKE_MANAGER_MODE",
                   *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    rest = os.pathsep.join(_path_without_inbox())
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CAPABILITIES_MANAGER_BIN": str(manager),
        "FAKE_INBOX_LOG": str(tmp_path / "inbox.log"),
        "PATH": f"{tools}{os.pathsep}{rest}",
    })
    lab = {"project": project, "env": env, "schema": schema, "ident": ident,
           "log": tmp_path / "inbox.log", "bare": f"{bare}{os.pathsep}{rest}"}
    assert tasks(lab, "migrate").returncode == 0
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def tasks(lab, *args: str, inbox: bool = True, given: str | None = None,
          **extra: str) -> subprocess.CompletedProcess:
    env = {**lab["env"], **extra}
    if not inbox:
        env["PATH"] = lab["bare"]
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=env,
                          input=given, text=True, capture_output=True, timeout=180)


def reviewer(lab, enabled: bool = True) -> None:
    """A worker kept in the store that takes the waiting tasks assigned to it."""
    ok(tasks(lab, "workers", "save", "reviewer", "-", given=(
        f"---\nenabled: {'true' if enabled else 'false'}\nprofile: claude-act\n"
        "takes: {status: [waiting], assignee: [reviewer]}\n---\n\nReview it.\n")))


def ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def refused(result: subprocess.CompletedProcess) -> tuple[int, dict]:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.startswith('{"error"'))
    return result.returncode, json.loads(line)["error"]


def calls(lab) -> list[dict]:
    """Every call the fake Inbox took, and forgets them."""
    path = lab["log"]
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("")
    return found


def items(lab) -> list[dict]:
    path = Path(str(lab["log"]) + ".state")
    return json.loads(path.read_text())["items"] if path.exists() else []


def answered(lab, key: str) -> dict:
    """Mark the fake item of `key` answered, as `inbox answer` would."""
    path = Path(str(lab["log"]) + ".state")
    state = json.loads(path.read_text())
    item = next(one for one in state["items"] if one["key"] == key and not one["withdrawn"])
    item["answered"] = True
    path.write_text(json.dumps(state))
    return item


def add(lab, key: str, *fields: str, inbox: bool = True) -> str:
    return ok(tasks(lab, "add", "--type", "probe", "--title", f"Decide {key}", "--key", key,
                    "--objective", f"settle {key}", *fields, inbox=inbox))["created"]


def move_key(lab, tid: str) -> str:
    """The key of the task's latest move: its status row where the status moved,
    else its assignee row."""
    changes = ok(tasks(lab, "history", tid))["changes"]
    latest = max(one["changed_at"] for one in changes
                 if one["field"] in ("status", "assignee"))
    rows = [one for one in changes
            if one["changed_at"] == latest and one["field"] in ("status", "assignee")]
    row = next((one for one in rows if one["field"] == "status"), rows[0])
    return f"tasks:{tid}:{row['id']}"


def deliver(lab, item: dict, answer: str = "", reply: str = "") -> subprocess.CompletedProcess:
    """Run an item's on-answer command as `inbox deliver` does: through `sh -c`, in
    the project's folder, the answer in the environment and nowhere else."""
    env = {**lab["env"], "INBOX_ITEM_ID": item["id"], "INBOX_KEY": item["key"] or "",
           "INBOX_KIND": item["kind"], "INBOX_ANSWER": answer, "INBOX_REPLY": reply,
           "INBOX_ANSWERED_BY": "alex@node", "INBOX_ANSWERED_AT": "2026-11-03T10:00:00.000000Z"}
    return subprocess.run(["sh", "-c", item["on_answer"]], cwd=lab["project"], env=env,
                          text=True, capture_output=True, timeout=180)


def shown(lab, ref: str) -> dict:
    return ok(tasks(lab, "show", ref))


# --- Putting a wait before its person -----------------------------------------

def test_a_move_into_waiting_on_a_person_puts_a_talk(lab):
    tid = add(lab, "t-talk", "--status", "todo")
    ok(tasks(lab, "activity", tid, "The release needs a decision on the schedule."))
    answer = ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON))
    assert set(answer) == {"updated", "fields", "moved", "task"}
    key = move_key(lab, tid)
    [call] = calls(lab)
    assert call["argv"] == ["talk", "Decide t-talk", "--text",
                            "The release needs a decision on the schedule.", "--key", key,
                            "--on-answer", f"tasks answer '{tid}' --key '{key}'",
                            "--source-label", "Decide t-talk", "--source-ref", tid]
    assert Path(call["cwd"]) == lab["project"] and call["agent"] == lab["ident"]


def test_the_four_flags_put_an_ask_with_its_words(lab):
    tid = add(lab, "t-ask", "--status", "todo")
    task = ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON,
                    "--question", "Ship the release on Friday?", "--yes", "Ship Friday",
                    "--no", "Hold it a week", "--recommend", "yes"))["task"]
    key = move_key(lab, tid)
    assert task["metadata"]["question"] == {
        "text": "Ship the release on Friday?", "yes": "Ship Friday", "no": "Hold it a week",
        "recommend": "yes", "move": key.rsplit(":", 1)[1]}
    [call] = calls(lab)
    assert call["argv"] == ["ask", "Decide t-ask", "--text", "Ship the release on Friday?",
                            "--yes", "Ship Friday", "--no", "Hold it a week",
                            "--recommend", "yes", "--key", key,
                            "--on-answer", f"tasks answer '{tid}' --key '{key}'",
                            "--source-label", "Decide t-ask", "--source-ref", tid]


def test_the_question_rides_only_on_a_move_into_waiting_on_a_person(lab):
    tid = add(lab, "t-q", "--status", "todo")
    four = ("--question", "Go?", "--yes", "Go", "--no", "Stop", "--recommend", "no")
    for args in (["--status", "waiting", "--assignee", PERSON, "--question", "Go?"],
                 ["--status", "waiting", "--assignee", PERSON, *four[:6], "--recommend",
                  "maybe"],
                 ["--title", "renamed", *four],
                 ["--status", "todo", "--assignee", PERSON, *four],
                 ["--status", "waiting", "--assignee", "Robin", *four]):
        code, error = refused(tasks(lab, "set", tid, *args))
        assert code == 6 and error["code"] == "input", args
    assert shown(lab, tid)["task"]["status"] == "todo"
    assert "question" not in shown(lab, tid)["task"]["metadata"]
    assert calls(lab) == []


def test_a_move_to_a_worker_an_agent_or_a_capability_puts_nothing(lab, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    # A project of this store is no person: asked of the store alone. A task
    # assigned to one is judged by that project's grants, which this one has none
    # of, so the judgment is asked here rather than through a move.
    monkeypatch.setattr(mod, "SCHEMA", lab["schema"])
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"""insert into {lab['schema']}.tasks_tasks (project_id, type, title)
                         values ('prj_elsewhere000', 'probe', 'theirs')""")
        with conn.cursor() as cur:
            assert mod._no_person_here(cur, lab["ident"], "prj_elsewhere000") is True
            assert mod._no_person_here(cur, lab["ident"], PERSON) is False
    reviewer(lab)
    tid = add(lab, "t-names", "--status", "todo")
    for name in ("reviewer", "agt0000aaaa", "helper-agent", "Robin", "clickup", "notion"):
        ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", name))
        ok(tasks(lab, "set", tid, "--status", "todo", "--clear", "assignee"))
    # A manager that cannot answer leaves a name unjudged, and nothing is put.
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON,
             FAKE_MANAGER_MODE="fail"))
    ok(tasks(lab, "set", tid, "--status", "todo", FAKE_MANAGER_MODE="fail"))
    assert calls(lab) == []


# --- The answer coming back ---------------------------------------------------

def test_the_answer_lands_on_the_trail_and_returns_the_task(lab):
    tid = add(lab, "t-back", "--status", "todo", "--assignee", "default")
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON,
             "--question", "Ship it?", "--yes", "Ship now", "--no", "Hold",
             "--recommend", "yes"))
    key = move_key(lab, tid)
    calls(lab)
    item = answered(lab, key)
    done = deliver(lab, item, "yes", "Ship it today.")
    answer = ok(done)
    assert answer["returned"] == {"status": "todo", "assignee": "default"}
    assert answer["moved"] == ["status", "assignee"]
    task = shown(lab, tid)
    assert (task["task"]["status"], task["task"]["assignee"]) == ("todo", "default")
    entry = task["activities"][-1]["description"]
    assert entry.startswith("Answered in the Inbox by alex@node at "
                            "2026-11-03T10:00:00.000000Z: yes (Ship now). Reply: Ship it today.")
    assert "Returned to todo on default" in entry
    # The return is a move away: its withdraw meets an answered item, and that is fine.
    [withdraw] = calls(lab)
    assert withdraw["argv"] == ["withdraw", "--key", key]


def test_words_alone_on_a_yes_or_no_task_land_on_the_trail_and_return_it(lab):
    tid = add(lab, "t-words", "--status", "todo", "--assignee", "default")
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON,
             "--question", "Ship it?", "--yes", "Ship now", "--no", "Hold",
             "--recommend", "yes"))
    key = move_key(lab, tid)
    calls(lab)
    words = "-x Ship half; $(touch pwned) 'and' \"wait\" `id`"
    answer = ok(deliver(lab, answered(lab, key), "other", words))
    assert answer["returned"] == {"status": "todo", "assignee": "default"}
    task = shown(lab, tid)
    assert (task["task"]["status"], task["task"]["assignee"]) == ("todo", "default")
    entry = task["activities"][-1]["description"]
    assert entry.startswith("Answered in the Inbox by alex@node at 2026-11-03T10:00:00."
                            f"000000Z: neither yes nor no, in words: {words}\n\n")
    assert "Returned to todo on default" in entry
    assert not (lab["project"] / "pwned").exists()


def test_a_stale_key_only_adds_a_note(lab):
    tid = add(lab, "t-stale", "--status", "todo")
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON))
    key = move_key(lab, tid)
    item = next(one for one in items(lab) if one["key"] == key)
    ok(tasks(lab, "set", tid, "--status", "todo", "--assignee", "default"))
    calls(lab)
    answer = ok(deliver(lab, item, "", "Late words."))
    assert answer["moved_on"] is True and answer["returned"] is None
    task = shown(lab, tid)
    assert (task["task"]["status"], task["task"]["assignee"]) == ("todo", "default")
    assert task["activities"][-1]["description"] == (
        "Answered in the Inbox by alex@node at 2026-11-03T10:00:00.000000Z: Late words."
        "\n\nThe answer came after the task moved on: it is todo on default now, so "
        "nothing was moved.")
    assert calls(lab) == []


def test_a_refused_return_says_so_and_still_exits_zero(lab):
    reviewer(lab)
    tid = add(lab, "t-refused", "--status", "waiting", "--assignee", "reviewer")
    ok(tasks(lab, "set", tid, "--assignee", PERSON))
    key = move_key(lab, tid)
    [call] = calls(lab)
    assert call["argv"][0] == "talk" and call["argv"][call["argv"].index("--key") + 1] == key
    item = answered(lab, key)
    # Switched off since, the reviewer takes nothing back there: the return is
    # refused, and the answer is recorded all the same.
    reviewer(lab, enabled=False)
    answer = ok(deliver(lab, item, "", "Do the obvious thing."))
    assert answer["returned"] is None and "no enabled worker takes it" in answer["refused"]
    task = shown(lab, tid)
    assert (task["task"]["status"], task["task"]["assignee"]) == ("waiting", PERSON)
    assert "It could not be returned to where it was" in task["activities"][-1]["description"]


# --- Moving away, and back ----------------------------------------------------

def test_a_move_away_withdraws_and_a_wait_begun_again_is_a_new_item(lab):
    tid = add(lab, "t-away", "--status", "todo")
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON))
    first = move_key(lab, tid)
    calls(lab)
    ok(tasks(lab, "set", tid, "--status", "todo"))
    assert [one["argv"] for one in calls(lab)] == [["withdraw", "--key", first]]
    ok(tasks(lab, "set", tid, "--status", "waiting"))
    second = move_key(lab, tid)
    assert second != first
    [put] = calls(lab)
    assert put["argv"][0] == "talk" and put["argv"][put["argv"].index("--key") + 1] == second
    # Straight from one person to another: the old item goes and a new one comes.
    ok(tasks(lab, "set", tid, "--assignee", OTHER_PERSON))
    third = move_key(lab, tid)
    withdrawn, put = calls(lab)
    assert withdrawn["argv"] == ["withdraw", "--key", second]
    assert put["argv"][0] == "talk" and put["argv"][put["argv"].index("--key") + 1] == third
    assert [one["withdrawn"] for one in items(lab)] == [True, True, False]


# --- Without the Inbox --------------------------------------------------------

def _same_move(lab, **how) -> dict:
    """One move into waiting on a person and one out of it, answered as the CLI
    answers them, with the ids and moments that differ between tasks left out."""
    tid = add(lab, f"t-{secrets.token_hex(3)}", "--status", "todo")
    said = []
    for args in (["--status", "waiting", "--assignee", PERSON], ["--status", "todo"]):
        done = tasks(lab, "set", tid, *args, **how)
        assert done.returncode == 0 and done.stderr == "", done.stderr
        answer = json.loads(done.stdout)
        said.append({"keys": sorted(answer), "fields": answer["fields"],
                     "moved": answer["moved"], "status": answer["task"]["status"],
                     "assignee": answer["task"]["assignee"],
                     "metadata": answer["task"]["metadata"]})
    return {"said": said, "fields": [(one["field"], one["old_value"], one["new_value"])
                                     for one in ok(tasks(lab, "history", tid))["changes"]]}


def test_absent_failing_gated_or_hanging_inbox_leaves_the_move_as_it_was(lab):
    absent = _same_move(lab, inbox=False)
    assert calls(lab) == []
    for mode in ("fail", "gated", "hang"):
        started = time.monotonic()
        assert _same_move(lab, FAKE_INBOX_MODE=mode) == absent, mode
        # Each move asks once and is not asked again.
        assert [one["argv"][0] for one in calls(lab)] == ["talk", "withdraw"], mode
        if mode == "hang":
            assert time.monotonic() - started < 60
    assert _same_move(lab) == absent
    # With the Inbox absent a question still lands with its move, for a later put.
    tid = add(lab, "t-q-absent", "--status", "todo", inbox=False)
    task = ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON,
                    "--question", "Go?", "--yes", "Go", "--no", "Stop", "--recommend", "no",
                    inbox=False))["task"]
    assert task["metadata"]["question"]["text"] == "Go?"


# --- The one-time put ---------------------------------------------------------

def test_the_one_time_command_puts_each_waiting_person_task_once(lab):
    talk = add(lab, "t-old-talk", "--status", "todo", inbox=False)
    ok(tasks(lab, "set", talk, "--status", "waiting", "--assignee", PERSON, inbox=False))
    ask = add(lab, "t-old-ask", "--status", "todo", inbox=False)
    ok(tasks(lab, "set", ask, "--status", "waiting", "--assignee", OTHER_PERSON,
             "--question", "Merge it?", "--yes", "Merge", "--no", "Wait",
             "--recommend", "no", inbox=False))
    born = add(lab, "t-born", "--status", "waiting", "--assignee", PERSON, inbox=False)
    reviewer(lab)
    add(lab, "t-worker", "--status", "waiting", "--assignee", "reviewer", inbox=False)
    add(lab, "t-agent", "--status", "waiting", "--assignee", "Robin", inbox=False)
    assert calls(lab) == []
    keys = {talk: move_key(lab, talk), ask: move_key(lab, ask), born: f"tasks:{born}:created"}

    first = ok(tasks(lab, "inbox"))
    assert {(one["task_id"], one["key"], one["kind"], one["created"])
            for one in first["put"]} == {(talk, keys[talk], "talk", True),
                                         (ask, keys[ask], "ask", True),
                                         (born, keys[born], "talk", True)}
    assert [one["assignee"] for one in first["left"]] == ["Robin"]
    assert len(calls(lab)) == 3
    again = ok(tasks(lab, "inbox"))
    assert {(one["task_id"], one["created"]) for one in again["put"]} == {
        (talk, False), (ask, False), (born, False)}
    assert len(items(lab)) == 3
    asked = next(one for one in items(lab) if one["kind"] == "ask")
    assert (asked["text"], asked["yes"], asked["no"], asked["recommend"]) == (
        "Merge it?", "Merge", "Wait", "no")
    code, error = refused(tasks(lab, "inbox", inbox=False))
    assert (code, error["code"]) == (6, "inbox_unavailable")


# --- A supervisor passing a task to the owner ---------------------------------

def test_a_supervisor_gives_the_question_on_its_move_under_its_grant(lab):
    import psycopg

    spec, _body = mod._parse_worker((_cli.CAPABILITY_DIR / "workers" / "supervisor.md")
                                    .read_text(), "supervisor.md")
    assert spec["writes"]["held"]["metadata"] == ["question"]
    tid = add(lab, "t-held", "--status", "todo")
    execution = ok(tasks(lab, "claim", "--key", tid, "--worker", "supervisor"))["execution"]["id"]

    def bind(writes: dict) -> None:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"""update {lab['schema']}.tasks_executions
                                set metrics = jsonb_build_object('worker_writes', %s::jsonb)
                              where id = %s""", (json.dumps(writes), execution))

    bind(spec["writes"])
    worker = {"TASKS_EXECUTION": execution}
    ok(tasks(lab, "set", tid, "--status", "waiting", "--assignee", PERSON, "--question",
             "Release now?", "--yes", "Release", "--no", "Hold", "--recommend", "yes",
             **worker))
    [put] = calls(lab)
    assert put["argv"][:3] == ["ask", "Decide t-held", "--text"]
    # The task now carries a question, so a second one writes over it, which its
    # grant allows and the same writes without `metadata` refuse.
    without = {**spec["writes"], "held": {k: v for k, v in spec["writes"]["held"].items()
                                          if k != "metadata"}}
    bind(without)
    code, error = refused(tasks(lab, "set", tid, "--assignee", OTHER_PERSON, "--question",
                                "Release later?", "--yes", "Later", "--no", "Never",
                                "--recommend", "no", **worker))
    assert (code, error["code"]) == (4, "policy") and "question" in error["message"]
    bind(spec["writes"])
    task = ok(tasks(lab, "set", tid, "--assignee", OTHER_PERSON, "--question",
                    "Release later?", "--yes", "Later", "--no", "Never", "--recommend", "no",
                    **worker))["task"]
    assert task["metadata"]["question"]["text"] == "Release later?"
    assert [one["argv"][0] for one in calls(lab)] == ["withdraw", "ask"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

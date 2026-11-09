#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract>=0.8,<1",
#                 "callva-harness-runner==0.12.1", "pyyaml>=6"]
# ///
"""A task may name the one node that claims it.

A task naming no node is taken on any node, as every task was; one naming a
node is selected by no claim on another node, and `--key` naming it there is
refused saying which node it belongs to. "This node" is the value a raise
records as its `node`. The field is set by `add --node`, changed by `set
--node`, cleared by `set --node any`, and shown by `show` and `list`. The
store step that adds it leaves every task already there as it was, still
claimed as before. The verbs are driven through the CLI against a store in
TASKS_TEST_DSN, and the checks skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract>=0.8,<1' \\
        --with 'callva-harness-runner==0.12.1' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_workers_store as stored  # noqa: E402
from test_workers_store import lab, cli, ok, refused, save  # noqa: E402,F401

mod = stored.mod
needs_store = stored.needs_store

NODE_A, NODE_B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
ON_A, ON_B = {"AGENT_NODE_ID": NODE_A}, {"AGENT_NODE_ID": NODE_B}

@pytest.fixture(autouse=True)
def _nodes_registered(request):
    """The nodes a case names are registered in the lab's store."""
    if "lab" in request.fixturenames:
        lab = request.getfixturevalue("lab")
        _cli.register(lab["schema"], (), (NODE_A, NODE_B))


WORKER = """---
description: Takes changes.
takes: [change]
profile: plain
---

Do the change.
"""


def add(lab, *args: str) -> str:
    return ok(cli(lab, "add", "--type", "change", "--title", "t", "--status", "todo",
                  *args))["created"]


def listed(lab) -> dict:
    return {row["id"]: row["node"] for row in ok(cli(lab, "list"))["tasks"]}


@needs_store
def test_a_node_is_set_at_add_changed_and_cleared_with_set_and_shown(lab):
    made = ok(cli(lab, "add", "--type", "change", "--title", "t", "--node", NODE_A))
    task = made["created"]
    assert made["node"] == NODE_A
    assert ok(cli(lab, "show", task))["task"]["node"] == NODE_A
    assert listed(lab)[task] == NODE_A
    ok(cli(lab, "set", task, "--node", NODE_B))
    assert ok(cli(lab, "show", task))["task"]["node"] == NODE_B
    assert listed(lab)[task] == NODE_B
    ok(cli(lab, "set", task, "--node", "any"))
    assert ok(cli(lab, "show", task))["task"]["node"] is None
    assert listed(lab)[task] is None
    assert refused(cli(lab, "set", task, "--node", "not a node"), 6)["code"] == "input"


@needs_store
def test_a_claim_takes_a_task_of_no_node_or_its_own_and_never_another_nodes(lab):
    ok(save(lab, "alpha", WORKER))
    on_b = add(lab, "--node", NODE_B)
    anywhere = add(lab)
    on_a = add(lab, "--node", NODE_A)
    # The conveyor's own selection on node A passes the task of node B over.
    assert ok(cli(lab, "run", "alpha", env=ON_A))["would_claim"] == anywhere
    assert ok(cli(lab, "run", "alpha", env=ON_B))["would_claim"] == on_b
    # A task of node B named with --key on node A is refused, naming B.
    for args in (["claim", "--key", on_b], ["run", "alpha", "--key", on_b, "--apply"]):
        error = refused(cli(lab, *args, env=ON_A), 6)
        assert error["code"] == "conflict"
        assert f"belongs to node {NODE_B}, and this is node {NODE_A}" in error["message"]
    assert ok(cli(lab, "run", "alpha", "--key", on_b, env=ON_A))["would_claim"] is None
    taken = [ok(cli(lab, "claim", "--type", "change", env=ON_A))["claimed"]
             for _ in range(3)]
    assert taken == [anywhere, on_a, None]
    assert ok(cli(lab, "run", "alpha", env=ON_A))["would_claim"] is None
    # Node B takes its own, and the raise records the node it was claimed on.
    held = ok(cli(lab, "claim", "--key", on_b, env=ON_B))
    assert held["claimed"] == on_b and held["execution"]["node"] == NODE_B


@needs_store
def test_a_turn_binds_or_clears_a_node_only_where_its_writes_grant_it(lab):
    task = add(lab)
    held = ok(cli(lab, "claim", "--key", task, "--worker", "alpha"))["execution"]["id"]
    turn = {"TASKS_EXECUTION": held}
    for args in (["set", task, "--node", NODE_A], ["set", task, "--node", "any"],
                 ["add", "--type", "change", "--title", "t", "--node", NODE_A]):
        assert refused(cli(lab, *args, env=turn), 4)["code"] == "policy"
    assert ok(cli(lab, "show", task))["task"]["node"] is None


@pytest.fixture
def before_the_node(tmp_path):
    """A project on a store whose tables stop at the step before the task's
    node, holding one task written there."""
    import psycopg
    from capabilities_contract import db
    schema = "tasks_test_node_" + secrets.token_hex(4)
    here = "prj_" + uuid.uuid4().hex[:12]
    _cli.write_store_setting(tmp_path / "config", schema)
    steps, _major, _minor = mod._tables()
    owner, profile_steps, profile_major, profile_minor = mod._profile_tables()
    assert [one for one, _sql in steps[-3:-1]] == ["0012-task-node", "0013-agent-names"]
    conn = db.connect(application_name="tasks-test", setting=_cli.store_setting(schema))
    try:
        db.migrate(conn, owner, profile_steps, major=profile_major, minor=profile_minor)
        db.migrate(conn, mod.NAME, steps[:-3], major=1, minor=9)
    finally:
        conn.close()
    with psycopg.connect(stored.DSN, autocommit=True) as conn:
        conn.execute(f"""insert into {schema}.tasks_tasks
                           (project_id, type, unique_key, title, status, created_by)
                         values (%s, 'change', 'k-old', 'old', 'todo', 'person:a')""",
                     (here,))
    _cli.register(schema, (here,), (NODE_A, NODE_B))
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"), **ON_A})
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                   "CLAUDE_PROJECT_DIR", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    try:
        yield {"here": stored._project(tmp_path / "here", here), "env": env,
               "schema": schema}
    finally:
        with psycopg.connect(stored.DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


@needs_store
def test_the_step_adds_the_node_leaving_every_task_as_it_was(before_the_node):
    import psycopg
    lab = before_the_node
    query = f"select * from {lab['schema']}.tasks_tasks order by id"
    with psycopg.connect(stored.DSN) as conn:
        found = conn.execute(query)
        assert "node" not in [column.name for column in found.description]
        before = found.fetchall()
    # The step comes with 0013-agent-names, which crosses to 2.0, so only the
    # store upgrade takes them.
    done = ok(cli(lab, "migrate", env={"CAPABILITIES_STORE_UPGRADE": "1"}))
    assert done["applied"] == ["0012-task-node", "0013-agent-names", "0014-keys"]
    with psycopg.connect(stored.DSN) as conn:
        found = conn.execute(query)
        names = [column.name for column in found.description]
        after = found.fetchall()
    at = names.index("node")
    assert [row[:at] + row[at + 1:] for row in after] == before
    assert [row[at] for row in after] == [None]
    # The task written before the step is claimed as before, on any node.
    assert ok(cli(lab, "claim", "--key", "k-old"))["claimed"] == str(after[0][0])

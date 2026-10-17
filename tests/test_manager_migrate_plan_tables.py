"""A migration's dry run plans every capability's own move whatever state
that capability's tables are in - not made yet, or a step behind the code -
and changes nothing in the store, its tables included.

One agent holds something for each capability that moves files of its own;
the contract's tables are current, and the capabilities' own tables are
first missing, then each a step behind. The module skips without `initdb`,
`pg_ctl`, git, psycopg or the shared database library.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from test_manager_migrate import PROFILE, REPO, SCHEMA, WORKER, World, gate


NAMES = ("agentrooms", "askproject", "automations", "coolify", "deployment", "memory",
         "tasks", "telegram", "whatsapp")
AGENT = "prj_7a7a7a7a7a71"
BEHIND = REPO / "tests" / "fixtures" / "migrate_behind"
# What each mover plans for the agent, as the plan that made its tables
# showed it too.
ITEMS = {
    "agentrooms": [("worker", "bot", "would_write")],
    "askproject": [("profile", "peer-read", "would_write")],
    "automations": [("agent_profile", "books", "would_write"),
                    ("definition", "nightly", "would_write")],
    "tasks": [("worker", "reviewer", "would_write"), ("profile", "review-claude", "would_write")],
    "telegram": [("profile", "telegram-claude-opus", "would_write"),
                 ("setting", "defaults", "would_write")],
    "whatsapp": [("profile", "c-claude", "would_write")],
}


def snapshot(w: World) -> dict:
    return {
        "columns": w.sql("SELECT table_name, column_name, data_type, is_nullable, "
                         "column_default FROM information_schema.columns WHERE "
                         "table_schema = %s ORDER BY 1, 2", (SCHEMA,)),
        "indexes": w.sql("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
                         "ORDER BY 1", (SCHEMA,)),
        "rows": w.tables(),
    }


@pytest.fixture()
def world():
    pytest.importorskip("psycopg")
    pytest.importorskip("capabilities_contract")
    for tool in ("initdb", "pg_ctl", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="capabilities-plan-tables-")).resolve()
    w = World(tmp)
    try:
        w.start()
        for name in NAMES:
            w.install(w.registry, name)
        w.write(w.config / "capabilities" / "settings.json", gate("coolify", "memory"))
        w.agent("full", AGENT, {
            "{env}/settings.json": gate(*NAMES),
            "{env}/telegram/connections.json": {"default": "main", "connections": {"main": {
                "session": "main", "api_id_env": "TG_API_ID", "api_hash_env": "TG_API_HASH"}}},
            "{env}/telegram/service/settings.json": {
                "connection": "main", "assistant_name": "Relay",
                "defaults": {"worker": "claude", "workers": {"claude": {"model": "opus"}}}},
            "{env}/whatsapp/connections.json": {"connections": {"c": {
                "engine": "waha", "phone": "+10000000000"}}},
            "{env}/whatsapp/service/settings.json": {"connection": "c",
                                                     "dialogue": {"profile": "c-claude"}},
            "{env}/whatsapp/service/profiles/c-claude.toml": PROFILE,
            "{env}/tasks/workers/reviewer.md": WORKER,
            "{env}/tasks/profiles/review-claude.toml": PROFILE,
            "{env}/askproject/profiles/peer-read.toml": PROFILE.replace('"opus"', '"haiku"'),
            "{env}/automations/service/config.toml": (
                'version = 1\n\n[engine]\ntimezone = "Europe/Tallinn"\n\n'
                '[agents.workers.books]\nengine = "claude"\nmodel = "sonnet"\nmode = "read"\n\n'
                '[[automations]]\nid = "nightly"\n'
                'script = "capabilities/automations/scripts/n.py"\n'),
            "{env}/automations/scripts/n.py": "print(1)\n",
            "{env}/agentrooms/connections.json": {"connections": {"bot": {
                "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
                "token_env": "BOT_TOKEN"}}},
            "{env}/agentrooms/service/settings.json": {"version": 1, "agents": {"bot": {
                "profile": "claude-act", "turn_timeout": 600, "history": 20}}},
            "deployment/runtime.json": {
                "schema": "capabilities.deployment.runtime.v1", "name": "full",
                "profile": "container",
                "service_policy": {"auto_include": True, "default_mode": "embedded",
                                   "capabilities": {}}},
        })
        # The contract's tables as this code keeps them.
        w.answer(w.manager(w.roots["full"], "agent", "show"))
        yield w
    finally:
        w.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def plan(w: World) -> dict:
    before = snapshot(w)
    result = w.manager(w.roots["full"], "migrate", "--dry-run")
    assert "Traceback" not in result.stderr, result.stderr[-3000:]
    dry = w.answer(result)
    assert snapshot(w) == before, "a dry run changed the store"
    assert not dry["plan"]["mover_failures"], dry["plan"]["mover_failures"]
    assert [b["code"] for b in dry["blockers"]] == ["global_not_migrated"]
    items = {name: [(i["kind"], i["name"], i["outcome"]) for i in report["items"]]
             for name, report in dry["plan"]["movers"].items() if report["items"]}
    assert items == ITEMS
    assert sorted(s["document"] for s in dry["plan"]["settings"]) == [
        "agentrooms", "automations", "telegram", "whatsapp"]
    return dry


def test_a_dry_run_plans_every_mover_on_tables_not_made_yet(world):
    dry = plan(world)
    pending = {owner: entry["steps"][0] for owner, entry in dry["tables_to_upgrade"].items()}
    assert {"automations", "tasks", "telegram", "harness"} <= set(pending)
    assert pending["telegram"].startswith("0001") and pending["tasks"].startswith("0001")
    assert all(entry["from"] is None for owner, entry in dry["tables_to_upgrade"].items()
               if owner != "capabilities")


def test_a_dry_run_plans_every_mover_on_tables_a_step_behind(world):
    root = world.roots["full"]
    for name in NAMES:
        world.cap(root, name, "migrate", CAPABILITIES_TEST_TABLES_BEHIND="1",
                  PYTHONPATH=str(BEHIND))
    held = {owner: steps for owner, steps in world.sql(
        "SELECT owner, array_agg(step ORDER BY step) FROM schema_ledger GROUP BY owner")}
    assert {"automations", "tasks", "telegram"} <= set(held)
    dry = plan(world)
    for owner in ("automations", "tasks", "telegram"):
        entry = dry["tables_to_upgrade"][owner]
        assert len(entry["steps"]) == 1 and entry["steps"][0] not in held[owner], (owner, entry)
    assert world.sql("SELECT owner, array_agg(step ORDER BY step) FROM schema_ledger "
                     "GROUP BY owner") == [(o, s) for o, s in sorted(held.items())]

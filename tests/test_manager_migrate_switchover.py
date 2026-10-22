"""`capabilities migrate` at a machine's real switchover: the states a copy of
every agent on one machine met that the migration suite's agents do not.

One machine is built for the module on the migration suite's World: a store
whose contract tables stand at 1.1, as a released manager left them; an agent
kept in the store from its main checkout with a linked worktree and a clone
of it in files mode; an agent whose envelope is an empty folder; three
agents holding one connection name with different specs; a root reading a
database of its own; and a root no machine list names, next to the others.

The tests run in file order: the global rows on the 1.1 store, the second
checkouts, the empty envelope, the gate flips seen from another database,
the connection differences, the report, and the discard. The module skips
without `initdb`, `pg_ctl`, git or psycopg.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_manager_migrate import MASTER, REPO, SCHEMA, WORKER, World, answers_file, gate


NAMES = ("automations", "coolify", "memory", "tasks", "whatsapp")
IDS = {"main": "prj_5a5a5a5a5a51", "empty": "prj_5a5a5a5a5a52", "first": "prj_5a5a5a5a5a53",
       "second": "prj_5a5a5a5a5a54", "third": "prj_5a5a5a5a5a55", "foreign": "prj_5a5a5a5a5a56",
       "stray": "prj_5a5a5a5a5a57", "sched": "prj_5a5a5a5a5a58",
       "hidden": "prj_5a5a5a5a5a59", "bound": "prj_5a5a5a5a5a5a"}
OTHER_NODE = "srv-discard"


def wa(phone: str) -> dict:
    return {"connections": {"shared": {"engine": "waha", "phone": phone}}}


def upgrade_to(world: World, count: int, major: int, minor: int) -> None:
    """The contract's tables brought to their first `count` steps."""
    from capabilities_contract.capability import store as S
    from capabilities_contract import db
    setting = db.Setting(url=world.url(), schema=SCHEMA, level="environment")
    conn = db.connect(application_name=S.STORE_OWNER, setting=setting)
    try:
        db.migrate(conn, S.STORE_OWNER, S.record_table_steps()[:count], major=major,
                   minor=minor)
    finally:
        conn.close()


def ensure_global(world: World) -> None:
    """The global rows written, whatever the global run under test did, so
    each later test meets its own defect."""
    if world.sql("SELECT to_regclass('capabilities_global') IS NOT NULL")[0][0] and \
            world.sql("SELECT global_migrated_at FROM capabilities_global")[0][0]:
        return
    from capabilities_contract.capability import store as S
    upgrade_to(world, len(S.record_table_steps()), *S.STORE_TABLES_VERSION)
    world.answer(world.manager(world.roots["first"], "migrate", "--global", "--apply"))


@pytest.fixture(scope="module")
def world():
    pytest.importorskip("psycopg")
    pytest.importorskip("capabilities_contract")
    for tool in ("initdb", "pg_ctl", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="capabilities-switchover-")).resolve()
    found = World(tmp)
    found.foreign = found.database + "_foreign"
    try:
        found.start()
        with found.admin() as conn:
            conn.execute(f"CREATE DATABASE {found.foreign} ENCODING 'UTF8' "
                         "TEMPLATE template0")
        for name in NAMES:
            found.install(found.registry, name)
        build(found)
        yield found
    finally:
        try:
            with found.admin() as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {found.foreign} WITH (FORCE)")
        except Exception:
            pass
        found.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def build(w: World) -> None:
    w.write(w.config / "capabilities" / "settings.json", gate("coolify", "memory"))
    w.write(w.config / "coolify" / "connections.json", {
        "default": "ops", "connections": {"ops": {"base_url": "https://coolify.example.org",
                                                  "token_env": "COOLIFY_TOKEN"}}})
    w.write(w.config / "memory" / "service" / "settings.json", {"max_entries": 500})
    main = w.agent("main", IDS["main"], {
        "{env}/settings.json": gate("coolify"),
        "{env}/coolify/connections.json": {"connections": {"box": {
            "base_url": "https://box.example.org", "token_env": "BOX_TOKEN"}}},
        "{env}/coolify/identifiers.json": {"app": {"value": "app-1", "note": ""}},
    })
    # A linked worktree and a clone of the agent, both as committed: files.
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q",
                    str(w.tmp / "agents" / "main-wt")], check=True)
    w.roots["main-wt"] = w.tmp / "agents" / "main-wt"
    subprocess.run(["git", "clone", "-q", str(main), str(w.tmp / "agents" / "clone")],
                   check=True)
    w.roots["clone"] = w.tmp / "agents" / "clone"
    # An envelope that is an empty folder, its id in the ContextKit binding.
    empty = w.agent("empty", None, {}, bound_id=IDS["empty"])
    (empty / "capabilities" / "project.json").unlink()
    (empty / "capabilities" / ".keep").write_text("")
    w.agent("first", IDS["first"], {
        "{env}/settings.json": gate("whatsapp"),
        "{env}/whatsapp/connections.json": wa("+10000000001"),
        "{env}/whatsapp/service/settings.json": {"connection": "shared"},
    })
    w.agent("second", IDS["second"], {
        "{env}/settings.json": gate("whatsapp"),
        "{env}/whatsapp/connections.json": {"default": "shared", **wa("+10000000002")},
        "{env}/whatsapp/service/settings.json": {"connection": "shared"},
    })
    w.agent("third", IDS["third"], {
        "{env}/settings.json": gate("whatsapp"),
        "{env}/whatsapp/connections.json": wa("+10000000003"),
    })
    # An agent with an automation and a worker, whose movers read their
    # capabilities' own tables.
    w.agent("sched", IDS["sched"], {
        "{env}/settings.json": gate("automations", "tasks"),
        "{env}/automations/service/config.toml":
            'version = 1\n\n[[automations]]\nid = "nightly"\nscript = "x.py"\n',
        "{env}/tasks/workers/reviewer.md": WORKER,
    })
    w.agentkit_list()
    w.listed = list(w.roots)
    # A root reading a database of its own, and a root no list names: both
    # made after the list, outside it.
    foreign = w.tmp / "foreign" / "foreign"
    foreign.parent.mkdir(parents=True)
    w.write(foreign / "capabilities" / "project.json",
            {"schema": "capabilities.project.v1", "slug": "foreign", "id": IDS["foreign"]})
    w.write(foreign / "capabilities" / "settings.json", gate("memory"))
    url = w.url().replace(w.database, w.foreign)
    (foreign / ".env.local").write_text(f"AGENTKIT_DB_URL={url}\nAGENTKIT_DB_SCHEMA={SCHEMA}\n")
    w.roots["foreign"] = foreign
    w.agent("stray", IDS["stray"], {"{env}/settings.json": gate("memory")})
    # A root bound by ContextKit alone, with no envelope folder, beside them.
    w.agent("bound", None, {}, bound_id=IDS["bound"])
    shutil.rmtree(w.roots["bound"] / "capabilities")
    # An agent kept in the store from a checkout no list names and no run is
    # given, and a clone of it in files mode.
    hidden = w.tmp / "hidden" / "main"
    hidden.parent.mkdir(parents=True)
    w.write(hidden / "capabilities" / "project.json",
            {"schema": "capabilities.project.v1", "slug": "hidden", "id": IDS["hidden"]})
    w.write(hidden / "capabilities" / "settings.json", gate("coolify"))
    w.write(hidden / "capabilities" / "coolify" / "identifiers.json",
            {"app": {"value": "app-h", "note": ""}})
    subprocess.run(["git", "init", "-q", str(hidden)], check=True)
    subprocess.run(["git", "-C", str(hidden), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(hidden), "-c", "user.email=t@example.org", "-c",
                    "user.name=t", "commit", "-qm", "agent"], check=True)
    subprocess.run(["git", "clone", "-q", str(hidden), str(w.tmp / "hidden" / "clone")],
                   check=True)
    w.roots["hidden"], w.roots["hidden-clone"] = hidden, w.tmp / "hidden" / "clone"
    upgrade_to(w, 8, 1, 1)


def state(w: World, agent_id: str) -> tuple:
    rows = w.sql("SELECT migrating_since, migrated_at FROM capabilities_agents WHERE id = %s",
                 (agent_id,))
    return rows[0] if rows else (None, None)


# --- D1: the global rows on a store whose contract tables are at 1.1 ---------------

def store_snapshot(w: World) -> dict:
    """The store's schema - every column, index and constraint - and every
    table's rows, the ledger and the version table among them."""
    return {
        "columns": w.sql("SELECT table_name, column_name, data_type, is_nullable, "
                         "column_default FROM information_schema.columns WHERE "
                         "table_schema = %s ORDER BY 1, 2", (SCHEMA,)),
        "indexes": w.sql("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
                         "ORDER BY 1", (SCHEMA,)),
        "constraints": w.sql("SELECT conrelid::regclass::text, conname, "
                             "pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN "
                             "pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s "
                             "ORDER BY 1, 2", (SCHEMA,)),
        "rows": w.tables(),
    }


def dry_runs_change_nothing(w: World, version: str) -> dict:
    """A global and an agent dry run on the store as it stands: neither
    crashes, both name the steps an apply would run, and the store - schema,
    ledger and rows - is byte-identical afterwards."""
    root = w.roots["first"]
    before = store_snapshot(w)
    # The capabilities' own tables, not made yet or behind: planned as if
    # they stood at this code's version, their steps named, nothing written.
    result = w.manager(w.roots["sched"], "migrate", "--dry-run")
    sched = w.answer(result)
    assert not sched["plan"]["mover_failures"], sched["plan"]["mover_failures"]
    items = {name: [(i["name"], i["outcome"]) for i in report["items"]]
             for name, report in sched["plan"]["movers"].items() if report["items"]}
    assert items == {"automations": [("nightly", "would_write")],
                     "tasks": [("reviewer", "would_write")]}
    assert {"capabilities", "automations", "tasks", "harness"} <= set(sched["tables_to_upgrade"])
    assert store_snapshot(w) == before
    result = w.manager(root, "migrate", "--global", "--dry-run")
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    dry = w.answer(result)
    assert dry["applied"] is False and dry["would_write"] is True
    assert dry["tables_to_upgrade"]["capabilities"]["from"] == version
    assert dry["tables_to_upgrade"]["capabilities"]["to"] == "1.3"
    assert "tables_upgraded" not in dry
    assert {r["capability"] for r in dry["plan"]["policy"]} == {"coolify", "memory"}
    assert store_snapshot(w) == before
    result = w.manager(root, "migrate", "--dry-run")
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    agent = w.answer(result)
    assert agent["tables_to_upgrade"]["capabilities"] == dry["tables_to_upgrade"]["capabilities"]
    assert [b["code"] for b in agent["blockers"]] == ["global_not_migrated"]
    assert not agent["plan"]["mover_failures"], agent["plan"]["mover_failures"]
    assert {(c["name"], c["outcome"]) for c in agent["plan"]["records"]["connections"]} \
        == {("shared", "write"), ("ops", "write")}
    assert store_snapshot(w) == before
    return dry


def automations_ledger_behind(w: World) -> tuple[str, str]:
    """automations' own tables made by all but their last step: that step, and
    the version its runtime migrates them to."""
    import importlib.util
    import sys
    from capabilities_contract import db
    spec = importlib.util.spec_from_file_location(
        "automations_runtime_under_test",
        REPO / "capabilities" / "automations" / "service" / "runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime
    spec.loader.exec_module(runtime)
    setting = db.Setting(url=w.url(), schema=SCHEMA, level="environment")
    conn = db.connect(application_name="automations", setting=setting)
    try:
        db.migrate(conn, runtime.LEDGER_OWNER, runtime.LEDGER_STEPS[:-1], major=1, minor=0)
    finally:
        conn.close()
    return runtime.LEDGER_STEPS[-1][0], f"{runtime.LEDGER_MAJOR}.{runtime.LEDGER_MINOR}"


def test_dry_runs_on_a_store_at_1_1_or_1_2_change_nothing_and_apply_upgrades(world):
    root = world.roots["first"]
    assert world.sql("SELECT major, minor FROM schema_version WHERE owner = 'capabilities'") \
        == [(1, 1)]
    dry = dry_runs_change_nothing(world, "1.1")
    assert dry["tables_to_upgrade"]["capabilities"]["steps"][0].startswith("0009")
    # At 1.2: the registry is there, the node on identifiers and the
    # settings tables are not; automations' tables are one step behind.
    upgrade_to(world, 13, 1, 2)
    last, current = automations_ledger_behind(world)
    dry = dry_runs_change_nothing(world, "1.2")
    assert dry["tables_to_upgrade"]["capabilities"]["steps"][0].startswith("0014")
    sched = world.answer(world.manager(world.roots["sched"], "migrate", "--dry-run"))
    assert sched["tables_to_upgrade"]["automations"] == {"from": "1.0", "to": current,
                                                         "steps": [last]}
    applied = world.answer(world.manager(root, "migrate", "--global", "--apply"))
    assert applied["state"] == "migrated" and applied["changed"] is True
    assert applied["tables_upgraded"]["version"] == "1.3"
    assert applied["tables_upgraded"]["steps"][0].startswith("0014")
    assert world.sql("SELECT major, minor FROM schema_version WHERE owner = 'capabilities'") \
        == [(1, 3)]
    # Each capability's own tables, as its install migrated them.
    for name in ("automations", "tasks", "whatsapp", "memory"):
        result = world.cap(root, name, "migrate")
        assert result.returncode == 0, (name, result.stderr)


# --- D2: second checkouts verify only -------------------------------------------------

def test_a_second_checkout_never_writes_the_agents_rows(world):
    ensure_global(world)
    main = world.roots["main"]
    # The agent's records are in the store from its main checkout, and the
    # store holds more than the second checkouts' files.
    world.answer(world.manager(main, "connections", "import"))
    world.answer(world.cap(main, "coolify", "ids", "set", "extra", "x-1", "--note", "later"))
    for name in ("main-wt", "clone"):
        root = world.roots[name]
        before = world.everything()
        applied = world.manager(root, "migrate", "--apply")
        assert world.everything() == before, (name, applied.stdout[-3000:])
        assert state(world, IDS["main"]) == (None, None)
        error = world.error(applied)
        assert error["code"] == "second_checkout" and str(main.resolve()) in error["message"]
        dry = world.answer(world.manager(root, "migrate"))
        assert dry["second_checkout"]["main"] == str(main.resolve()), dry
        assert [b["code"] for b in dry["blockers"]][:1] == ["second_checkout"]
        assert dry["would_write"] is False and "plan" not in dry
        assert world.everything() == before, name
    assert world.sql("SELECT label FROM capabilities_identifiers WHERE agent_id = %s "
                     "ORDER BY label", (IDS["main"],)) == [("app",), ("extra",)]
    # The main checkout moves; the others then read its rows and change nothing.
    applied = world.answer(world.manager(main, "migrate", "--apply"))
    assert applied["state"] == "migrated" and not applied["verify"]["differences"]
    for name in ("main-wt", "clone"):
        before = world.everything()
        again = world.answer(world.manager(world.roots[name], "migrate", "--apply"))
        assert again["state"] == "migrated" and again["changed"] is False
        assert again["second_checkout"]["main"] == str(main.resolve())
        assert world.everything() == before


def test_a_clone_whose_main_checkout_is_nowhere_found_writes_nothing(world):
    ensure_global(world)
    hidden, clone = world.roots["hidden"], world.roots["hidden-clone"]
    # The agent's records went into the store from its own checkout, which no
    # list names and this run is not given.
    world.answer(world.manager(hidden, "connections", "import"))
    world.answer(world.cap(hidden, "coolify", "ids", "set", "extra", "x-h", "--note", "later"))
    before = world.everything()
    rows = store_snapshot(world)
    applied = world.manager(clone, "migrate", "--apply")
    assert store_snapshot(world) == rows, applied.stdout[-3000:]
    assert world.everything() == before
    error = world.error(applied)
    assert error["code"] == "second_checkout", error
    assert state(world, IDS["hidden"]) == (None, None)
    dry = world.answer(world.manager(clone, "migrate"))
    [question] = dry["questions"]
    assert question["id"] == "checkout" and question["choices"] == [str(clone.resolve())]
    assert dry["would_write"] is False and "plan" not in dry
    # A person who says this clone is the agent's checkout gets its plan.
    answers = answers_file(world, "hidden-clone", {"checkout": str(clone.resolve())})
    planned = world.answer(world.manager(clone, "migrate", "--answers", answers))
    assert "second_checkout" not in planned and planned["plan"]["records"]["remove"]
    assert store_snapshot(world) == rows
    assert world.sql("SELECT label FROM capabilities_identifiers WHERE agent_id = %s "
                     "ORDER BY label", (IDS["hidden"],)) == [("app",), ("extra",)]


# --- D3: an empty envelope folder moves nothing -----------------------------------------

def test_an_empty_envelope_folder_is_no_mover_failure(world):
    ensure_global(world)
    root = world.roots["empty"]
    dry = world.answer(world.manager(root, "migrate"))
    assert not dry["plan"]["mover_failures"], dry["plan"]["mover_failures"]
    assert not dry["blockers"] and dry["would_write"] is True
    applied = world.answer(world.manager(root, "migrate", "--apply"))
    assert applied["state"] == "migrated"
    assert "id" not in json.loads((root / "capabilities" / "project.json").read_text())
    assert f"CAPABILITIES_PROJECT_ID={IDS['empty']}\n" in (root / ".env.local").read_text()


# --- D4: gate flips only for agents reading the same database ---------------------------

def test_gate_flips_name_only_agents_of_the_same_database(world):
    ensure_global(world)
    root = world.roots["foreign"]
    # Seen from the machine's store, agents of the machine's store flip
    # nothing: their global rows are written.
    dry = world.answer(world.manager(root, "migrate", "--global"))
    assert dry["store"]["level"] == "project" and dry["global_migrated_at"] is None
    assert {r["capability"]: r["outcome"] for r in dry["plan"]["policy"]} == {
        "coolify": "add", "memory": "add"}
    machine = {IDS[n] for n in ("main", "first", "second", "third", "empty")}
    assert not machine & {f["agent_id"] for f in dry["gate_flips"]}, dry["gate_flips"]
    assert dry["gate_flips"] == []


# --- D5 and D6: one connection name, different specs ----------------------------------

def test_a_connection_renamed_for_this_agent(world):
    ensure_global(world)
    first = world.answer(world.manager(world.roots["first"], "migrate", "--apply"))
    assert first["state"] == "migrated"
    root = world.roots["second"]
    dry = world.answer(world.manager(root, "migrate"))
    [question] = [q for q in dry["questions"] if q["id"] == "connections.whatsapp/shared"]
    assert {"rename": "<a new name>"} in question["choices"]
    answers = answers_file(world, "second", {"connections": {
        "whatsapp/shared": {"rename": "second-shared"}}})
    applied = world.answer(world.manager(root, "migrate", "--apply", "--answers", answers))
    assert applied["state"] == "migrated" and not applied["verify"]["differences"]
    assert dict(world.sql("SELECT name, spec->>'phone' FROM capabilities_connections "
                          "WHERE capability = 'whatsapp'")) == {
        "shared": "+10000000001", "second-shared": "+10000000002"}
    assert world.sql("SELECT connection, is_default FROM capabilities_connection_grants "
                     "WHERE agent_id = %s AND capability = 'whatsapp'", (IDS["second"],)) \
        == [("second-shared", True)]
    assert world.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                     "capability = 'whatsapp' AND path = '{connection}'",
                     (IDS["second"],)) == [("second-shared",)]
    switch = world.through_switch(root, ["whatsapp"])
    assert switch["adapter"] == "AgentRecords"
    assert {cid: v[0]["phone"] for cid, v in switch["connections"]["whatsapp/False"].items()} \
        == {"second-shared": "+10000000002"}
    assert switch["defaults"]["whatsapp"] == "second-shared"
    assert switch["settings"]["whatsapp"]["keys"]["connection"] == "second-shared"
    # The others keep the stored one.
    assert world.sql("SELECT connection FROM capabilities_connection_grants WHERE "
                     "agent_id = %s AND capability = 'whatsapp'", (IDS["first"],)) == [("shared",)]


def test_keeping_the_stores_connection_adopts_it(world):
    ensure_global(world)
    root = world.roots["third"]
    answers = answers_file(world, "third", {"connections": {"whatsapp/shared": "store"}})
    result = world.manager(root, "migrate", "--apply", "--answers", answers)
    applied = world.answer(result)
    assert applied["state"] == "migrated" and not applied["verify"]["differences"]
    assert state(world, IDS["third"])[0] is None
    assert world.sql("SELECT spec->>'phone' FROM capabilities_connections WHERE "
                     "capability = 'whatsapp' AND name = 'shared'") == [("+10000000001",)]
    switch = world.through_switch(root, ["whatsapp"])
    assert {cid: v[0]["phone"] for cid, v in switch["connections"]["whatsapp/False"].items()} \
        == {"shared": "+10000000001"}


# --- D9: the report tells live readers from historical notes -------------------------

def test_report_counts_historical_notes_apart(world):
    ensure_global(world)
    root = world.roots["first"]
    world.write(root / "assets" / "research" / "old.md", "read capabilities/x/connections.json\n")
    world.write(root / "CHANGELOG.md", "- stopped reading service/settings.json\n")
    world.write(root / "scripts" / "reader.py", "open('capabilities/x/connections.json')\n")
    report = world.answer(world.manager(root, "migrate", "--report", "--root", str(root)))
    [entry] = [a for a in report["agents"] if Path(a["root"]) == root.resolve()]
    assert entry["consumers"] == [
        "scripts/reader.py:1: open('capabilities/x/connections.json')"]
    assert entry["historical_notes"] == 2
    (root / "scripts" / "reader.py").unlink()
    report = world.answer(world.manager(root, "migrate", "--report", "--root", str(root)))
    [entry] = [a for a in report["agents"] if Path(a["root"]) == root.resolve()]
    assert entry["consumers"] == [] and not entry["files"]


# --- D8 and D7: the discard -------------------------------------------------------------

def config_files(w: World) -> list[str]:
    return sorted(str(p.relative_to(w.config)) for p in w.config.rglob("*") if p.is_file())


def listing_only(w: World, names: list[str]) -> None:
    """AgentKit's agent list naming only these roots, as a machine whose
    lists miss most of its agents."""
    listing = w.home / "Library" / "Application Support" / "AgentKit" / "agents.plist"
    listing.write_bytes(plistlib.dumps([{"path": str(w.roots[n])} for n in names]))


def test_discard_weighs_every_root_found_near_the_listed_ones(world):
    ensure_global(world)
    before = config_files(world)
    assert "coolify/connections.json" in before
    # No list names any agent; the discard runs in a migrated one, and an
    # unmigrated one sits beside it.
    listing_only(world, [])
    try:
        result = world.manager(world.roots["first"], "migrate", "--discard",
                               AGENT_NODE_NAME=OTHER_NODE)
        assert config_files(world) == before, result.stdout
    finally:
        listing_only(world, world.listed)
    error = world.error(result)
    assert error["code"] == "agents_unmigrated", error
    assert str(world.roots["stray"].resolve()) in error["message"]
    waiting = {a["root"]: a for a in error["detail"]["agents"]}
    stray = waiting[str(world.roots["stray"].resolve())]
    assert stray["sources"][0] == f"found under {(world.tmp / 'agents').resolve()}"
    # A root bound by ContextKit alone reads the config home all the same.
    bound = waiting[str(world.roots["bound"].resolve())]
    assert bound["id"] == IDS["bound"] and bound["state"] != "migrated"
    assert config_files(world) == before


def test_discard_refuses_on_a_master_node(world):
    ensure_global(world)
    for name in ("stray", "bound", "sched"):
        applied = world.answer(world.manager(world.roots[name], "migrate", "--apply"))
        assert applied["state"] == "migrated", name
    before = config_files(world)
    # Run on this agent's master, every root it weighs moved.
    listing_only(world, ["first"])
    try:
        result = world.manager(world.roots["first"], "migrate", "--discard")
        assert config_files(world) == before, result.stdout
    finally:
        listing_only(world, world.listed)
    error = world.error(result)
    assert error["code"] == "master_node", error
    assert MASTER in error["message"] and config_files(world) == before
    unnamed = world.error(world.manager(world.roots["first"], "migrate", "--discard",
                                        AGENT_NODE_NAME=None))
    assert unnamed["code"] == "node_unnamed" and config_files(world) == before
    discarded = world.answer(world.manager(world.roots["first"], "migrate", "--discard",
                                           AGENT_NODE_NAME=OTHER_NODE))
    assert "coolify/connections.json" in {str(Path(p).relative_to(world.config))
                                          for p in discarded["removed"]}


# --- D10: machine connections after the discard -------------------------------------------

def test_list_counts_machine_connections_from_the_store_after_discard(world):
    ensure_global(world)
    # Discarded, whether or not the discard under test did it.
    for path in world.config.glob("*/connections.json"):
        path.unlink()
    listed = world.answer(world.manager(world.roots["first"], "list"))
    counts = {row["name"]: row["machine_connections"] for row in listed["installed"]}
    held = dict(world.sql("SELECT capability, count(*) FROM capabilities_connections "
                          "GROUP BY capability"))
    assert counts["whatsapp"] == held["whatsapp"] == 2
    assert counts["coolify"] == held["coolify"] >= 1
    assert counts["memory"] == 0

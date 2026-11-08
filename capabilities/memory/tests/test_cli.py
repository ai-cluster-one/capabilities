"""The command line, run as a child the way an agent runs it, against a store
schema of each case's own."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from _cli import World, drop_schema, error, needs_store, new_schema, project_id


@pytest.fixture
def world(tmp_path):
    schema = new_schema()
    made = World(tmp_path, schema)
    try:
        yield made
    finally:
        drop_schema(schema)


def tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_manifest_declares_the_constant_block_and_no_service(tmp_path):
    world = World(tmp_path, None)
    manifest = world.ok(tmp_path, "manifest", "--json")
    assert set(manifest["contribution"]) == {"markdown"}
    block = manifest["contribution"]["markdown"]
    marks = ["built-in memory", "`memory add \"", "`memory search \"<terms>\"`", "subagent",
             "`memory show <id>`", "`memory help`"]
    places = [block.index(mark) for mark in marks]
    assert places == sorted(places)
    assert not re.search(r"\d+ active entr", block)
    for words in ("No entries yet", "--since", "newest written", "_Rendered", "memory render"):
        assert words not in block
    assert not any(line.startswith("#") for line in block.splitlines())
    assert "service" not in manifest
    assert manifest["tables"] is True and manifest["state"] is False
    assert manifest["docs"]["topics"] == sorted(manifest["docs"]["topics"])


def test_without_a_store_every_store_verb_refuses(tmp_path):
    world = World(tmp_path, None)
    root = world.project("p")
    for args in (("add", "x", "--title", "t"), ("list",), ("groom", "--if-due")):
        done = world.run(root, *args)
        assert done.returncode == 6, done.stderr
        assert error(done)["code"] == "store_not_configured"


@needs_store
def test_write_read_and_the_block(world):
    root = world.project("p")
    added = world.ok(root, "add", "Bare dates are UTC.", "--title", "Dates", "--pin")
    assert added["created"] and added["entry"]["pinned"]
    assert "context" not in added
    again = world.ok(root, "add", "Bare dates are UTC.", "--title", "Dates", "--pin")
    assert again["created"] is False
    world.ok(root, "add", "-", "--title", "From stdin", stdin="read from stdin\n")

    listed = world.ok(root, "list", "--json")
    assert listed["total"] == 2 and [e["title"] for e in listed["entries"]] == \
        ["From stdin", "Dates"]
    lines = world.run(root, "list").stdout.splitlines()
    assert lines[0].endswith("· From stdin") and lines[1].endswith("· Dates (pinned)")

    found = world.ok(root, "search", "stdin", "--json")
    assert [e["title"] for e in found["entries"]] == ["From stdin"]
    entry = found["entries"][0]
    shown = world.ok(root, "show", entry["short"], "--json")
    assert shown["entry"]["body"] == "read from stdin"
    assert [e["kind"] for e in shown["trail"]] == ["add"]
    assert "Trail:" in world.run(root, "show", entry["short"]).stdout

    status = world.ok(root, "status")
    assert status["entries"]["active"] == 2 and status["entries"]["pinned"] == 1
    assert status["writes"]["last_7_days"] == 2
    assert "context" not in status and "service" not in status

    # Nothing renders a block: no write leaves a file in the envelope's state.
    assert not (root / "capabilities" / "memory" / "state").exists()

    doctor = world.ok(root, "doctor")
    assert doctor["ok"] and "context" not in doctor and "service" not in doctor


@needs_store
def test_supersede_drop_and_the_trail(world):
    root = world.project("p")
    old = world.ok(root, "add", "first", "--title", "Rule")["entry"]
    out = world.ok(root, "supersede", old["short"], "--with", "second", "--title", "Rule v2",
                   "--reason", "it changed")
    assert out["superseded"][0]["status"] == "superseded"
    new = out["created"][0]
    done = world.run(root, "drop", old["short"], "--reason", "x")
    assert done.returncode == 6 and error(done)["code"] == "not_active"
    dropped = world.ok(root, "drop", new["short"], "--reason", "promoted", "--to",
                       "context/RULES.md")
    assert dropped["dropped"]["destination"] == "context/RULES.md"
    log = world.ok(root, "log", "--json")
    assert [e["kind"] for e in log["events"]] == ["drop", "supersede", "add", "add"]
    assert world.ok(root, "list", "--status", "all", "--json")["total"] == 2
    assert "(no active entries)" in world.run(root, "list").stdout
    mismatch = world.run(root, "supersede", old["short"], "--with", "a", "--with", "b",
                         "--title", "only one")
    assert mismatch.returncode == 6


@needs_store
def test_another_projects_memory_is_read_and_never_written(world):
    mine, theirs = world.project("mine"), world.project("theirs")
    entry = world.ok(theirs, "add", "their text", "--title", "Theirs")["entry"]
    their_id = project_id(theirs)
    shown = world.ok(mine, "show", entry["id"], "--json")
    assert shown["entry"]["project_id"] == their_id
    assert world.ok(mine, "list", "--project", their_id, "--json")["total"] == 1
    assert world.ok(mine, "list", "--json")["total"] == 0
    done = world.run(mine, "drop", entry["id"], "--reason", "not mine")
    assert done.returncode == 4 and error(done)["code"] == "foreign_project"
    done = world.run(mine, "pin", entry["id"])
    assert done.returncode == 4


@needs_store
def test_the_read_only_switch_refuses_writes_and_leaves_reads(world):
    root = world.project("p")
    world.ok(root, "add", "x", "--title", "t")
    for args in (("add", "y", "--title", "u"), ("drop", "0000000", "--reason", "x"),
                 ("groom", "--apply"), ("adopt", "--apply", str(root))):
        done = world.run(root, *args, CAPABILITIES_READ_ONLY="1")
        assert done.returncode == 4, (args, done.stderr)
        assert error(done)["code"] == "read_only_switch"
    assert world.ok(root, "list", "--json", CAPABILITIES_READ_ONLY="1")["total"] == 1
    done = world.run(root, "groom", "--if-due", CAPABILITIES_READ_ONLY="1")
    assert done.returncode == 4 and error(done)["code"] == "read_only_switch"


@needs_store
def test_policy_and_identity(world):
    disabled = world.project("off", enabled=False)
    done = world.run(disabled, "list")
    assert done.returncode == 4 and error(done)["code"] == "disabled"
    root = world.project("p")
    (root / "capabilities" / "project.json").write_text(json.dumps({"slug": "p"}))
    (root / ".env.local").unlink()
    done = world.run(root, "add", "x", "--title", "t")
    assert done.returncode == 6 and error(done)["code"] == "no_project_identity"


@needs_store
def test_doctor_warns_of_an_earlier_release_grooming_against_the_store(world):
    """A holder of the groom advisory lock that no lease row names is a pass
    of an earlier release: a warning naming its machine, never a failure. One
    a row names by lock_pid is this release's own pass."""
    import psycopg
    from _cli import dsn_url
    root = world.project("p")
    world.ok(root, "add", "x", "--title", "t")
    assert "earlier_release" not in world.ok(root, "doctor")
    project = project_id(root)
    with psycopg.connect(dsn_url(), autocommit=True,
                         application_name="memory-earlier") as earlier:
        earlier.execute("SELECT pg_advisory_lock(hashtext('memory groom ' || %s), hashtext(%s))",
                        (world.schema, project))
        done = world.run(root, "doctor")
        assert done.returncode == 0, done.stdout + done.stderr
        warned = json.loads(done.stdout)["earlier_release"]
        assert warned["ok"] is True and warned["retires"] == "2027-01-05"
        [holder] = warned["holders"]
        assert holder["pid"] == earlier.info.backend_pid and holder["this_project"] is True
        assert holder["application_name"] == "memory-earlier"
        assert holder["client_addr"] and holder["backend_start"]
        assert "memory-earlier" in warned["warning"] and "2027-01-05" in warned["warning"]
        earlier.execute(f'INSERT INTO "{world.schema}".memory_groom_leases '
                        "(agent_id, holder, lock_pid, expires_at) "
                        "VALUES (%s, gen_random_uuid(), pg_backend_pid(), "
                        "now() + interval '1 hour')", (project,))
        assert "earlier_release" not in world.ok(root, "doctor")


@needs_store
def test_settings_shape_the_window_and_bad_ones_refuse(world):
    root = world.project("p")
    settings = root / "capabilities" / "memory" / "service" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"version": 1, "window_days": 1, "min_entries": 0}))
    world.ok(root, "add", "x", "--title", "t")
    assert world.ok(root, "groom", "--if-due")["window"]["window"] == 1
    settings.write_text(json.dumps({"window_days": "a month"}))
    done = world.run(root, "groom", "--if-due")
    assert done.returncode == 6 and error(done)["code"] == "settings_invalid"


@needs_store
def test_export_and_inventory(world, tmp_path):
    root = world.project("p")
    world.ok(root, "add", "x", "--title", "t")
    out = tmp_path / "export.json"
    written = world.ok(root, "export", "--out", str(out))
    assert written["entries"] == 1 and written["events"] == 1
    document = json.loads(out.read_text())
    assert document["entries"][0]["body"] == "x" and document["project"] == project_id(root)
    before = tree(root)
    inventory = world.ok(root, "inventory")
    assert inventory["metrics"] == [] and inventory["items"] == []
    assert inventory["service"] is None
    assert tree(root) == before


@needs_store
def test_migrate_reports_and_applies(world):
    root = world.project("p")
    status = world.ok(root, "migrate", "status")
    assert status["state"] == "pending"
    applied = world.ok(root, "migrate")
    assert applied["owner"] == "memory"
    assert applied["applied"] == status["pending"] and applied["skipped"] == []
    assert world.ok(root, "migrate", "status")["state"] == "current"
    again = world.ok(root, "migrate")
    assert again["applied"] == [] and again["skipped"] == status["pending"]

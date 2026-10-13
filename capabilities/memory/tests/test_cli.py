"""The command line, run as a child the way an agent runs it, against a store
schema of each case's own."""

from __future__ import annotations

import hashlib
import json
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


def test_manifest_declares_the_contribution_and_the_service(tmp_path):
    world = World(tmp_path, None)
    manifest = world.ok(tmp_path, "manifest", "--json")
    assert manifest["contribution"] == {"path": "state/context.md", "max_bytes": 32768}
    assert manifest["tables"] is True and manifest["state"] is True
    machine = manifest["service"]["machine"]
    assert machine["command"] == ["memory", "service", "run", "--machine"]
    for verb in ("init", "doctor", "run", "start", "reload", "stop", "status", "logs",
                 "join", "leave"):
        assert verb in manifest["service"]["verbs"]
    assert manifest["docs"]["topics"] == sorted(manifest["docs"]["topics"])


def test_without_a_store_every_store_verb_refuses(tmp_path):
    world = World(tmp_path, None)
    root = world.project("p")
    for args in (("add", "x", "--title", "t"), ("list",), ("render",)):
        done = world.run(root, *args)
        assert done.returncode == 6, done.stderr
        assert error(done)["code"] == "store_not_configured"


@needs_store
def test_write_read_and_the_block(world):
    root = world.project("p")
    added = world.ok(root, "add", "Bare dates are UTC.", "--title", "Dates", "--pin")
    assert added["created"] and added["entry"]["pinned"]
    assert added["context"]["rendered"] and added["context"]["fits"]
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
    assert status["context"]["current"] is True

    context = root / "capabilities" / "memory" / "state" / "context.md"
    text = context.read_text()
    assert text.index("Pinned:") < text.index("Dates") < text.index("From stdin")
    sidecar = json.loads(context.with_suffix(".json").read_text())
    assert sidecar["last_event"] == status["last_event"]

    before = tree(root)
    fragment = world.run(root, "render", "--fragment")
    assert fragment.returncode == 0 and "From stdin" in fragment.stdout
    assert tree(root) == before

    doctor = world.ok(root, "doctor")
    assert doctor["ok"] and doctor["context"]["current"]


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
    assert world.ok(root, "render", CAPABILITIES_READ_ONLY="1")["shown"] == 1


@needs_store
def test_policy_and_identity(world):
    disabled = world.project("off", enabled=False)
    done = world.run(disabled, "list")
    assert done.returncode == 4 and error(done)["code"] == "disabled"
    root = world.project("p")
    (root / "capabilities" / "project.json").write_text(json.dumps({"slug": "p"}))
    done = world.run(root, "add", "x", "--title", "t")
    assert done.returncode == 6 and error(done)["code"] == "no_project_identity"


@needs_store
def test_doctor_finds_a_block_behind_the_store(world):
    root = world.project("p")
    world.ok(root, "add", "x", "--title", "t")
    (root / "capabilities" / "memory" / "state" / "context.md").unlink()
    done = world.run(root, "doctor")
    assert done.returncode == 7
    assert json.loads(done.stdout)["context"]["exists"] is False
    world.ok(root, "render")
    assert world.ok(root, "doctor")["ok"]


@needs_store
def test_settings_shape_the_block_and_bad_ones_refuse(world):
    root = world.project("p")
    settings = root / "capabilities" / "memory" / "service" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"version": 1, "window_days": 1, "min_entries": 0}))
    world.ok(root, "add", "x", "--title", "t")
    assert world.ok(root, "render")["window"] == 1
    settings.write_text(json.dumps({"window_days": "a month"}))
    done = world.run(root, "render")
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
    assert inventory["service"] == {"state": "stopped", "ok": True}
    assert tree(root) == before


def test_inventory_names_a_missing_block(tmp_path):
    world = World(tmp_path, None)
    root = world.project("p")
    service = world.ok(root, "inventory")["service"]
    assert service["ok"] is False and "memory render" in service["problem"]


@needs_store
def test_migrate_reports_and_applies(world):
    root = world.project("p")
    status = world.ok(root, "migrate", "status")
    assert status["state"] == "pending"
    applied = world.ok(root, "migrate")
    assert applied["owner"] == "memory"
    assert world.ok(root, "migrate", "status")["state"] == "current"

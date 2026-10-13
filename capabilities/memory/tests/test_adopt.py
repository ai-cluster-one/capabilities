"""Carrying memory folders into the store: a dry run with every decision, an
apply that enables, joins and renders, and a second apply that carries
nothing twice."""

from __future__ import annotations

import json

import pytest

from _cli import World, drop_schema, manager_bin, needs_store, new_schema, project_id

pytestmark = needs_store

DAY = ("# 2026-09-14\n\n## 00:01 — What memory is for\n\nOne conclusion per entry.\n\n"
       "## 00:02 — Owner decision on stores\n\nThe owner said: “without a store there is "
       "no memory”.\n\n## 00:03 — Empty\n")


@pytest.fixture
def world(tmp_path):
    schema = new_schema()
    made = World(tmp_path, schema)
    made.enable_globally()
    try:
        yield made
    finally:
        drop_schema(schema)


def folder(root, text=DAY):
    (root / "memory").mkdir()
    (root / "memory" / "2026-09-14.md").write_text(text)
    (root / "memory" / "README.txt").write_text("not memory")


def test_dry_run_reports_every_decision_and_writes_nothing(world, tmp_path):
    root = world.project("p", enabled=None)
    folder(root)
    out = world.ok(tmp_path, "adopt", str(root))
    project = out["projects"][0]
    assert out["mode"] == "dry-run" and project["decision"] == "would adopt"
    decisions = {e["title"]: e["decision"] for e in project["entries"]}
    assert decisions == {"What memory is for": "carry", "Owner decision on stores": "carry",
                         "Empty": "skip"}
    assert project["not_read"][0]["why"] == "not a Markdown file"
    assert project["counts"]["carry"] == 2
    gate = json.loads((root / "capabilities" / "settings.json").read_text())
    assert "memory" not in gate["capabilities"]


@pytest.mark.skipif(manager_bin() is None, reason="no manager beside the source")
def test_apply_enables_joins_carries_and_is_idempotent(world, tmp_path):
    root = world.project("p", enabled=None)
    folder(root)
    out = world.ok(tmp_path, "adopt", str(root), "--apply", "--pin-titles", "^Owner", TZ="UTC")
    project = out["projects"][0]
    assert project["enabled"]["ok"], project
    assert project["counts"]["carried"] == 2 and project["joined"]["joined"] is True
    assert project["context"]["rendered"]
    gate = json.loads((root / "capabilities" / "settings.json").read_text())
    assert gate["capabilities"]["memory"] == {"enabled": True}
    entries = {e["title"]: e for e in world.ok(root, "list", "--json")["entries"]}
    assert entries["Owner decision on stores"]["pinned"] is True
    assert entries["What memory is for"]["created_at"] == "2026-09-14T00:01:00Z"
    listed = json.loads((world.config / "memory" / "service" / "projects.json").read_text())
    assert project_id(root) in listed["projects"]

    again = world.ok(tmp_path, "adopt", str(root), "--apply")["projects"][0]
    assert again["counts"]["carried"] == 0 and again["counts"]["already"] == 2
    zoned = world.ok(tmp_path, "adopt", str(root), "--apply", "--timezone",
                     "Asia/Tokyo")["projects"][0]
    assert zoned["counts"]["carried"] == 0 and zoned["counts"]["already"] == 2
    assert world.ok(root, "status")["entries"]["active"] == 2
    assert (root / "memory" / "2026-09-14.md").read_text() == DAY


def test_timezone_names_the_zone_heading_times_are_read_in(world, tmp_path):
    root = world.project("p", enabled=None)
    folder(root)
    out = world.ok(tmp_path, "adopt", str(root), "--timezone", "Europe/Berlin")
    entries = {e["title"]: e for e in out["projects"][0]["entries"]}
    first = entries["What memory is for"]
    assert first["created_at"] == "2026-09-13T22:01:00Z"
    assert first["dated_from"] == ("the file name and the section's time, read in "
                                   "Europe/Berlin (UTC+02:00)")
    local = world.ok(tmp_path, "adopt", str(root), TZ="UTC")["projects"][0]["entries"][0]
    assert local["created_at"] == "2026-09-14T00:01:00Z"
    assert "the machine's local zone (UTC, UTC+00:00)" in local["dated_from"]
    done = world.run(tmp_path, "adopt", str(root), "--timezone", "Mars/Olympus")
    assert done.returncode == 6 and "--timezone names no IANA zone" in done.stderr


def test_disabled_projects_and_projects_without_identity_are_skipped(world, tmp_path):
    off = world.project("off", enabled=False)
    folder(off)
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    folder(bare)
    out = world.ok(tmp_path, "adopt", str(off), str(bare), "--apply")
    rows = {p["root"]: p for p in out["projects"]}
    assert rows[str(off)]["decision"] == "skip" and "disabled" in rows[str(off)]["why"]
    assert rows[str(bare)]["decision"] == "skip" and "capabilities init" in rows[str(bare)]["why"]


def test_default_roots_are_the_machine_opt_in_lists(world, tmp_path):
    root = world.project("listed")
    folder(root)
    other = world.config / "othercap" / "service" / "projects.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"version": 1, "projects": {
        project_id(root): {"root": str(root), "slug": "listed"}}}))
    out = world.ok(tmp_path, "adopt")
    assert out["roots_from"] == "the machine's service opt-in lists"
    assert [p["root"] for p in out["projects"]] == [str(root)]
    assert out["lists"][0]["roots"] == 1


def test_a_marked_entry_is_carried_once_even_after_its_heading_changes(world, tmp_path):
    memory_id = "d" * 64
    root = world.project("p")
    (root / "memory").mkdir()
    day = root / "memory" / "2026-10-02.md"
    day.write_text(f"# 2026-10-02\n\n<!-- contextkit-memory-id: {memory_id} -->\n"
                   "## 20:44 — The direction\n\nOne store layer.\n")
    dry = world.ok(tmp_path, "adopt", str(root))["projects"][0]
    assert [(e["title"], e["decision"]) for e in dry["entries"]] == [("The direction", "carry")]
    assert dry["entries"][0]["memory_id"] == memory_id
    first = world.ok(tmp_path, "adopt", str(root), "--apply")["projects"][0]
    assert first["counts"]["carried"] == 1
    day.write_text(f"# 2026-10-02\n\n<!-- contextkit-memory-id: {memory_id} -->\n"
                   "## 20:44 — The direction, renamed\n\nOne store layer.\n")
    again = world.ok(tmp_path, "adopt", str(root), "--apply")["projects"][0]
    assert again["counts"] == {"carried": 0, "already": 1, "skipped": 0, "not_read": 0}
    entries = world.ok(root, "list", "--json")["entries"]
    assert [e["title"] for e in entries] == ["The direction"]
    shown = world.ok(root, "show", entries[0]["short"], "--json")
    assert "contextkit-memory-id" not in shown["entry"]["body"]
    assert shown["trail"][0]["detail"]["contextkit_memory_id"] == memory_id

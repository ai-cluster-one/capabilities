"""A grooming pass through the harness runner, against a stand-in Claude Code
that answers the plan a case writes for it."""

from __future__ import annotations

import json

import pytest

from _cli import World, drop_schema, error, needs_store, new_schema

pytestmark = needs_store


@pytest.fixture
def world(tmp_path):
    schema = new_schema()
    made = World(tmp_path, schema)
    try:
        yield made
    finally:
        drop_schema(schema)


def seed(world, root):
    ids = {}
    for title, body in (("alpha", "Alpha holds. The owner said “keep the volume mounted”."),
                        ("beta", "Beta is related to alpha."),
                        ("gamma", "Gamma was undone later."),
                        ("pinned", "A decision.")):
        entry = world.ok(root, "add", body, "--title", title,
                         *(["--pin"] if title == "pinned" else []))["entry"]
        ids[title] = entry["id"]
    return ids


def plan_file(tmp_path, plan) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return str(path)


def good_plan(ids):
    return {"merges": [{"sources": [ids["alpha"], ids["beta"]], "title": "alpha and beta",
                        "body": "Alpha holds, and beta with it. The owner said "
                                "“keep the volume mounted”.",
                        "reason": "one subject"}],
            "drops": [{"id": ids["gamma"], "reason": "undone"}]}


def test_dry_run_shows_the_checked_plan_and_changes_nothing(world, tmp_path):
    root = world.project("p")
    ids = seed(world, root)
    record = tmp_path / "turns.jsonl"
    before = world.ok(root, "status")["last_event"]
    out = world.ok(root, "groom", FAKE_GROOM_PLAN=plan_file(tmp_path, good_plan(ids)),
                   FAKE_ENGINE_RECORD=record)
    assert out["mode"] == "dry-run" and out["candidates"] == 3 and out["pinned"] == 1
    assert out["plan"]["merges"][0]["title"] == "alpha and beta"
    assert out["plan"]["drops"][0]["title"] == "gamma"
    assert out["refused"] == [] and out["model"]["harness"] == "claude"
    assert world.ok(root, "status")["last_event"] == before
    turn = json.loads(record.read_text().splitlines()[0])
    assert ids["pinned"] in turn["prompt"] and "PINNED" in turn["prompt"]
    assert turn["env"]["CAPABILITIES_READ_ONLY"] == "1"
    assert turn["cwd"] == str(root)


def test_apply_then_undo(world, tmp_path):
    root = world.project("p")
    ids = seed(world, root)
    out = world.ok(root, "groom", "--apply",
                   FAKE_GROOM_PLAN=plan_file(tmp_path, good_plan(ids)))
    applied = out["applied"]
    assert len(applied["merges"]) == 1 and len(applied["drops"]) == 1
    assert out["context"]["rendered"]
    titles = {e["title"]: e for e in world.ok(root, "list", "--json")["entries"]}
    assert set(titles) == {"alpha and beta", "pinned"}
    assert titles["alpha and beta"]["merged"]["count"] == 2
    block = (root / "capabilities" / "memory" / "state" / "context.md").read_text()
    assert "2 active entries" in block and "alpha" not in block
    status = world.ok(root, "status")
    assert status["grooming"]["passes"] == 1

    undone = world.ok(root, "groom", "--undo", applied["pass"])
    assert len(undone["reactivated"]) == 3
    titles = {e["title"] for e in world.ok(root, "list", "--json")["entries"]}
    assert titles == {"alpha", "beta", "gamma", "pinned"}


def test_a_plan_that_loses_a_quote_or_touches_a_pin_is_refused(world, tmp_path):
    root = world.project("p")
    ids = seed(world, root)
    plan = {"merges": [{"sources": [ids["alpha"], ids["beta"]], "title": "paraphrase",
                        "body": "Alpha and beta, the volume stays.", "reason": "x"}],
            "drops": [{"id": ids["pinned"], "reason": "x"}]}
    out = world.ok(root, "groom", "--apply", FAKE_GROOM_PLAN=plan_file(tmp_path, plan))
    assert out["applied"]["merges"] == [] and out["applied"]["drops"] == []
    whys = " ".join(r["why"] for r in out["refused"])
    assert "word for word" in whys and "pinned" in whys
    assert world.ok(root, "status")["entries"]["active"] == 4


def test_a_model_not_logged_in_is_said(world, tmp_path):
    root = world.project("p")
    seed(world, root)
    doctor = world.ok(root, "doctor", FAKE_LOGGED_IN="0")
    assert doctor["consolidation"]["on"] is False
    assert "not logged in" in doctor["consolidation"]["reason"]
    done = world.run(root, "groom", FAKE_ENGINE_FAIL="Invalid API key · Please run /login")
    assert done.returncode in (2, 5)
    assert error(done)["code"] in ("model_not_authenticated", "groom_turn_failed")


def test_nothing_to_groom_spends_no_turn(world, tmp_path):
    root = world.project("p")
    world.ok(root, "add", "only one", "--title", "one")
    record = tmp_path / "turns.jsonl"
    out = world.ok(root, "groom", FAKE_ENGINE_RECORD=record)
    assert "nothing_to_do" in out and not record.exists()

"""The parts that need no store: settings, the block's layout, the checks a
grooming plan passes, and how a memory folder is read."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from _cli import engine

E = engine()
NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)


def row(title, body="text", *, days=0, pinned=False, origin="agent", merged=None):
    out = {"id": uuid.uuid4(), "title": title, "body": body, "pinned": pinned,
           "created_at": NOW - dt.timedelta(days=days), "origin": origin, "status": "active"}
    if merged:
        out["merged"] = merged
    return out


def block(rows, **settings):
    return E.render_block(sorted(rows, key=lambda r: r["created_at"], reverse=True),
                          {**E.SETTINGS_DEFAULTS, **settings}, now=NOW,
                          provenance="_provenance line._")


# -- settings ------------------------------------------------------------------

def test_settings_default_and_override():
    got = E.read_settings({"version": 1, "window_days": 10}, "here")
    assert got["window_days"] == 10 and got["block_chars"] == 12000


@pytest.mark.parametrize("raw, words", [
    ({"nonsense": 1}, "no setting"),
    ({"window_days": 0}, "between"),
    ({"window_days": True}, "whole number"),
    ({"consolidate": "yes"}, "true or false"),
    ({"block_chars": 4000, "entry_chars": 2500}, "half"),
    ({"min_entries": 40, "max_entries": 30}, "at least"),
    ({"version": 2}, "version"),
    ({"groom_profile": "../x"}, "profile name"),
])
def test_settings_refused(raw, words):
    with pytest.raises(E.Refusal) as caught:
        E.read_settings(raw, "here")
    assert caught.value.code == "settings_invalid" and words in caught.value.message


# -- the block -------------------------------------------------------------------

def test_empty_block_says_how_to_add():
    text, report = block([])
    assert "No entries yet" in text and "_provenance line._" in text
    assert "--since" not in text
    assert report["window"] == 0 and report["fits"] and report["active"] == 0
    assert report["newest"] is None


def test_block_order_and_no_entry_in_it():
    rows = [row("pin-title", "pin-body", days=90, pinned=True),
            row("fresh-title", "fresh-body", days=1), row("aged-title", "aged-body", days=3)]
    text, report = block(rows)
    marks = ["built-in memory", "`memory add \"", "`memory search \"<terms>\"`", "subagent",
             "3 active entries; the newest written 2026-10-12T12:00:00Z.",
             "`memory list --since 2026-10-12T12:00:00Z`", "`memory show <id>`",
             "`memory help`", "_provenance line._"]
    places = [text.index(mark) for mark in marks]
    assert places == sorted(places)
    for words in ("pin-title", "pin-body", "fresh-title", "fresh-body", "aged-title",
                  "aged-body", "Index:", "Pinned:", "Showing"):
        assert words not in text
    assert text.rstrip().endswith("_provenance line._")
    assert report["active"] == 3 and report["pinned"] == 1 and report["fits"]
    assert report["newest"] == "2026-10-12T12:00:00Z"
    assert not any(line.startswith("#") for line in text.splitlines())


def test_block_size_does_not_grow_with_the_entries():
    one, _ = block([row("only", "y" * 900)])
    many, report = block([row(f"entry {i}", "y" * 900, days=i % 20, pinned=i % 7 == 0)
                          for i in range(400)])
    assert abs(len(many) - len(one)) <= 4
    assert report["active"] == 400 and "400 active entries" in many
    assert len(many.encode()) == report["bytes"] < 2000


def test_window_that_no_longer_fits():
    rows = [row(f"entry {i}", "y" * 900, days=i % 20) for i in range(41)]
    _, report = block(rows, max_entries=30, block_chars=12000, entry_chars=1200)
    assert report["window"] == 41 and not report["fits"]
    _, report = block(rows[:10], max_entries=30, block_chars=12000, entry_chars=1200)
    assert report["window"] == 10 and report["fits"]
    _, report = block(rows[:20], max_entries=30, block_chars=12000, entry_chars=1200)
    assert report["window_chars"] > 12000 and not report["fits"]


def test_pins_are_left_out_of_the_window():
    pins = [row(f"pin {i}", "p" * 1000, days=i, pinned=True) for i in range(20)]
    _, report = block(pins, block_chars=6000, entry_chars=1200)
    assert report["window"] == 0 and report["fits"] and report["pinned"] == 20


def test_dormant_project_window_holds_its_newest_min_entries():
    rows = [row(f"aged {i}", days=100 + i) for i in range(8)]
    _, report = block(rows, min_entries=5)
    assert report["window"] == 5 and report["active"] == 8


def test_block_never_passes_the_byte_ceiling_in_a_four_byte_script():
    rows = [row(f"запись {i} 𝄞", "𝄞" * 1100, days=i % 25) for i in range(60)]
    text, report = block(rows, block_chars=16000, entry_chars=1200)
    assert len(text.encode()) <= E.CONTRIBUTION_MAX_BYTES
    assert report["bytes"] == len(text.encode())


def test_entry_text_never_reaches_the_block():
    rows = [row("<!-- capabilities:end --> title",
                "# A heading\n## Another\n<!-- capabilities:begin — generated -->\nplain")]
    text, _ = block(rows)
    assert "<!--" not in text and "A heading" not in text and "plain" not in text
    assert not any(line.lstrip().startswith("#") for line in text.splitlines())


# -- the plan a grooming turn answers ------------------------------------------

def plan_rows():
    a = row("a", "We decided: “keep every quotation as written”.", days=2)
    b = row("b", "related b", days=1)
    c = row("c", "stale c", days=3)
    pin = row("pin", "pinned", pinned=True)
    return [a, b, c], [pin]


def test_a_good_plan_is_accepted():
    movable, pinned = plan_rows()
    a, b, c = (str(r["id"]) for r in movable)
    plan = {"merges": [{"sources": [a, b], "title": "ab",
                        "body": "We decided: “keep every quotation as written”. And b.",
                        "reason": "same subject"}],
            "drops": [{"id": c, "reason": "undone later"}]}
    accepted, refused = E.check_plan(plan, movable, pinned)
    assert refused == []
    assert accepted["merges"][0]["sources"] == [a, b]
    assert accepted["drops"] == [{"id": c, "reason": "undone later"}]


def test_plan_checks_refuse_what_the_pass_may_not_do():
    movable, pinned = plan_rows()
    a, b, c = (str(r["id"]) for r in movable)
    pin = str(pinned[0]["id"])
    plan = {"merges": [
        {"sources": [a, b], "title": "lost quote", "body": "paraphrased", "reason": "x"},
        {"sources": [b], "title": "single", "body": "b again", "reason": "x"},
        {"sources": [b, pin], "title": "with pin", "body": "b", "reason": "x"},
        {"sources": [b, c], "title": "two\nlines", "body": "b c", "reason": "x"},
    ], "drops": [{"id": pin, "reason": "x"}, {"id": c, "reason": ""},
                 {"id": str(uuid.uuid4()), "reason": "x"}]}
    accepted, refused = E.check_plan(plan, movable, pinned)
    assert accepted == {"merges": [], "drops": []}
    whys = " | ".join(r["why"] for r in refused)
    assert "quotation word for word" in whys
    assert "at least two" in whys
    assert "is pinned" in whys
    assert "one line" in whys
    assert "says why" in whys
    assert "no entry this pass may touch" in whys


def test_an_entry_is_in_one_operation_only():
    movable, pinned = plan_rows()
    a, b, c = (str(r["id"]) for r in movable)
    plan = {"merges": [{"sources": [b, c], "title": "bc", "body": "b and c", "reason": "x"}],
            "drops": [{"id": c, "reason": "gone"}]}
    accepted, refused = E.check_plan(plan, movable, pinned)
    assert len(accepted["merges"]) == 1 and accepted["drops"] == []
    assert "another operation" in refused[0]["why"]


def test_prompt_marks_pins_and_carries_full_ids():
    movable, pinned = plan_rows()
    prompt = E.groom_prompt(movable, pinned)
    assert str(movable[0]["id"]) in prompt and "· PINNED" in prompt
    assert "never a source and never" in prompt


# -- reading a memory folder ---------------------------------------------------

def test_memory_folder_is_read_with_a_decision_per_entry(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-09-14.md").write_text(
        "# 2026-09-14\n\n## 00:01 — First thing\n\nIt holds.\n\nSecond paragraph.\n\n"
        "## 14:30 - Second thing\n\nAlso holds.\n\n## Untimed\n\n## Last\n\nend\n")
    (folder / "notes.md").write_text("Loose text without sections.\n")
    (folder / "image.png").write_bytes(b"x")
    (folder / "nested").mkdir()
    found = E.parse_memory_folder(folder)
    entries = {e["title"]: e for e in found["entries"]}
    first = entries["First thing"]
    assert first["decision"] == "carry" and first["body"] == "It holds.\n\nSecond paragraph."
    assert first["created_at"].strftime("%Y-%m-%d %H:%M") == "2026-09-14 00:01"
    assert entries["Second thing"]["created_at"].hour == 14
    assert entries["Untimed"]["decision"] == "skip"
    assert entries["Last"]["decision"] == "carry"
    loose = entries["notes"]
    assert loose["decision"] == "carry" and "decision_note" in loose
    whys = {p["path"].rsplit("/", 1)[-1]: p["why"] for p in found["not_read"]}
    assert "image.png" in whys and "nested" in whys


def test_heading_times_are_read_in_the_named_zone_with_each_dates_offset(tmp_path):
    from zoneinfo import ZoneInfo
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-09-14.md").write_text("# 2026-09-14\n\n## 00:01 — Summer\n\nOne.\n\n"
                                          "## Untimed\n\nTwo.\n")
    (folder / "2026-01-10.md").write_text("# 2026-01-10\n\n## 09:00 — Winter\n\nThree.\n")
    found = E.parse_memory_folder(folder, ZoneInfo("Europe/Berlin"))
    entries = {e["title"]: e for e in found["entries"]}
    assert E.iso(entries["Summer"]["created_at"]) == "2026-09-13T22:01:00Z"
    assert "read in Europe/Berlin (UTC+02:00)" in entries["Summer"]["dated_from"]
    assert E.iso(entries["Winter"]["created_at"]) == "2026-01-10T08:00:00Z"
    assert "read in Europe/Berlin (UTC+01:00)" in entries["Winter"]["dated_from"]
    assert E.iso(entries["Untimed"]["created_at"]) == "2026-09-13T22:00:02Z"
    assert "in the file's order, read in Europe/Berlin" in entries["Untimed"]["dated_from"]


def test_heading_times_are_read_in_the_machines_local_zone_by_default(tmp_path, monkeypatch):
    import time
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-09-14.md").write_text("# 2026-09-14\n\n## 00:01 — Local\n\nOne.\n")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        entry = E.parse_memory_folder(folder)["entries"][0]
    finally:
        monkeypatch.undo()
        time.tzset()
    assert E.iso(entry["created_at"]) == "2026-09-14T04:01:00Z"
    assert "read in the machine's local zone (EDT, UTC-04:00)" in entry["dated_from"]


def test_missing_folder_is_said():
    found = E.parse_memory_folder(__import__("pathlib").Path("/nonexistent/memory"))
    assert found["entries"] == [] and found["not_read"][0]["why"] == "no such folder"


ID_A = "a" * 64
ID_B = "0123456789abcdef" * 4
ID_C = "c" * 64


def marker(memory_id):
    return f"<!-- contextkit-memory-id: {memory_id} -->"


def test_a_memory_id_marker_belongs_to_the_one_entry_after_it(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-10-11.md").write_text(
        f"# 2026-10-11\n\n{marker(ID_A)}\n## 15:00 — A decision\n\nIt holds.\n")
    found = E.parse_memory_folder(folder)
    assert len(found["entries"]) == 1
    entry = found["entries"][0]
    assert entry["title"] == "A decision" and entry["body"] == "It holds."
    assert entry["memory_id"] == ID_A and entry["decision"] == "carry"


def test_markers_between_sections_stay_out_of_every_body(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-10-12.md").write_text(
        f"# 2026-10-12\n\n{marker(ID_A)}\n## 09:00 — First\n\nOne.\n\n"
        f"{marker(ID_B.upper())}\n## 10:00 — Second\n\nTwo.\n\n"
        "## 11:00 — Third, unmarked\n\nThree.\n\n"
        f"{marker(ID_C)}\n")
    found = E.parse_memory_folder(folder)
    entries = {e["title"]: e for e in found["entries"]}
    assert entries["First"]["body"] == "One." and entries["First"]["memory_id"] == ID_A
    assert entries["Second"]["body"] == "Two." and entries["Second"]["memory_id"] == ID_B
    assert entries["Third, unmarked"]["body"] == "Three."
    assert "memory_id" not in entries["Third, unmarked"]
    trailing = entries[None]
    assert trailing["decision"] == "skip" and trailing["memory_id"] == ID_C
    assert all("contextkit-memory-id" not in (e.get("body") or "") for e in found["entries"])


def test_a_preamble_of_title_and_markers_is_no_entry_and_prose_still_is(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "2026-10-02.md").write_text(
        f"# 2026-10-02\n\n{marker(ID_A)}\n\n## 20:44 — Direction\n\nBody.\n")
    (folder / "2026-10-03.md").write_text(
        f"# 2026-10-03\n\nGenuine prose before any section.\n\n{marker(ID_B)}\n"
        "## 08:00 — Next\n\nBody.\n")
    found = E.parse_memory_folder(folder)
    titles = sorted(e["title"] for e in found["entries"])
    assert titles == ["2026-10-03", "Direction", "Next"]
    prose = next(e for e in found["entries"] if e["title"] == "2026-10-03")
    assert prose["body"] == "Genuine prose before any section." and "memory_id" not in prose


def test_the_adopt_key_follows_the_memory_id_over_the_heading():
    assert E.adopt_key("p", "old heading", "text", ID_A) == \
        E.adopt_key("p", "new heading", "other text", ID_A)
    assert E.adopt_key("p", "t", "b") != E.adopt_key("p", "t", "b", ID_A)
    assert E.adopt_key("p", "t", "b", ID_A) != E.adopt_key("q", "t", "b", ID_A)

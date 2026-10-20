"""The verbs over a throwaway store: save and its versions, the stale base,
retire, history, diff, search, and an assets folder imported and exported
back byte for byte."""

from __future__ import annotations

import json
from pathlib import Path

from _cli import error, needs_store, world  # noqa: F401

pytestmark = needs_store


def save(world, root, name, body: bytes, *extra):  # noqa: F811
    return world.ok(root, "save", name, "--from", "-", *extra, stdin=body)


def test_save_creates_then_adds_a_version_and_a_repeat_saves_nothing(world):  # noqa: F811
    root = world.project()
    first = save(world, root, "plans/2026-10-20_rollout", b"# Rollout\n\nstep one\n",
                 "--note", "first draft")
    assert first["created"] and first["document_created"] and first["replaced"] is None
    assert first["document"]["kind"] == "plan"
    assert first["version"]["title"] == "Rollout" and first["version"]["note"] == "first draft"
    again = save(world, root, "plans/2026-10-20_rollout", b"# Rollout\n\nstep one\n")
    assert again["created"] is False and again["version"]["id"] == first["version"]["id"]
    second = save(world, root, "plans/2026-10-20_rollout", b"# Rollout\n\nstep two\n")
    assert second["created"] and not second["document_created"]
    assert second["replaced"] == first["version"]["id"]
    shown = world.ok(root, "show", "plans/2026-10-20_rollout")
    assert shown["version"]["id"] == second["version"]["id"]
    assert shown["version"]["body"] == "# Rollout\n\nstep two\n"
    old = world.ok(root, "show", "plans/2026-10-20_rollout", "--version",
                   first["version"]["id"][:8])
    assert old["version"]["body"] == "# Rollout\n\nstep one\n" and not old["version"]["active"]


def test_a_save_from_a_stale_base_is_refused_and_changes_nothing(world):  # noqa: F811
    root = world.project()
    base = save(world, root, "research/r", b"# R\n\nv1\n")["version"]["id"]
    mine = save(world, root, "research/r", b"# R\n\nv2\n", "--base", base)
    assert mine["replaced"] == base
    late = world.run(root, "save", "research/r", "--from", "-", "--base", base,
                     stdin=b"# R\n\nv3 from a writer that read v1\n")
    assert late.returncode == 7, late.stderr
    refusal = error(late)
    assert refusal["code"] == "stale_base"
    assert mine["version"]["id"][:8] in refusal["message"]
    history = world.ok(root, "history", "research/r")
    assert [v["id"] for v in history["versions"]] == [mine["version"]["id"], base]
    assert [v["active"] for v in history["versions"]] == [True, False]
    missing = world.run(root, "save", "research/new", "--from", "-", "--base", base,
                        stdin=b"x\n")
    assert missing.returncode == 7 and error(missing)["code"] == "stale_base"


def test_retire_hides_the_document_and_keeps_its_versions(world):  # noqa: F811
    root = world.project()
    first = save(world, root, "meetings/m", b"# M\n\nnotes\n")
    retired = world.ok(root, "retire", "meetings/m")
    assert retired["retired"]["retired_at"]
    assert world.ok(root, "list")["count"] == 0
    assert world.ok(root, "search", "notes")["matched"] == 0
    again = world.run(root, "retire", "meetings/m")
    assert again.returncode == 3 and error(again)["code"] == "not_found"
    kept = world.ok(root, "show", "meetings/m", "--version", first["version"]["id"])
    assert kept["version"]["body"] == "# M\n\nnotes\n" and kept["document"]["retired_at"]
    reborn = save(world, root, "meetings/m", b"# M\n\nnew notes\n")
    assert reborn["document_created"] and reborn["document"]["id"] != first["document"]["id"]


def test_history_and_diff(world):  # noqa: F811
    root = world.project()
    v1 = save(world, root, "reports/q3", b"# Q3\n\nalpha\nbeta\n")["version"]["id"]
    v2 = save(world, root, "reports/q3", b"# Q3\n\nalpha\ngamma\n", "--note", "fix")["version"]["id"]
    v3 = save(world, root, "reports/q3", b"# Q3\n\nalpha\ngamma\ndelta")["version"]["id"]
    history = world.ok(root, "history", "reports/q3")
    assert history["count"] == 3
    assert [v["id"] for v in history["versions"]] == [v3, v2, v1]
    assert history["versions"][1]["note"] == "fix" and "body" not in history["versions"][0]
    bare = world.ok(root, "diff", "reports/q3")
    assert bare["from"]["id"] == v2 and bare["to"]["id"] == v3 and bare["changed"]
    assert "+delta\n\\ No newline at end of file\n" in bare["diff"]
    one = world.ok(root, "diff", "reports/q3", v1[:6])
    assert one["from"]["id"] == v1 and one["to"]["id"] == v3
    two = world.ok(root, "diff", "reports/q3", v1, v2)
    assert "-beta\n" in two["diff"] and "+gamma\n" in two["diff"]
    only = save(world, root, "notes/solo", b"solo\n")
    first = world.ok(root, "diff", "notes/solo")
    assert first["from"] is None and first["to"]["id"] == only["version"]["id"]
    unknown = world.run(root, "diff", "reports/q3", "ffffff")
    assert unknown.returncode == 3 and error(unknown)["code"] == "version_not_found"


def test_search_reads_the_active_versions_only(world):  # noqa: F811
    root = world.project()
    save(world, root, "research/fts", "# Поиск\n\nzebra crossing downtown\n".encode())
    save(world, root, "research/fts", b"# Search\n\nyak shaving\n")
    save(world, root, "plans/other", b"# Other\n\nnothing to see\n")
    assert world.ok(root, "search", "zebra")["matched"] == 0
    hit = world.ok(root, "search", "yak")
    assert hit["matched"] == 1 and hit["results"][0]["name"] == "research/fts"
    assert "<b>yak</b>" in hit["results"][0]["headline"]
    save(world, root, "journal/j", "# Päevik\n\nvanalinn täna\n".encode())
    assert world.ok(root, "search", "täna")["results"][0]["name"] == "journal/j"
    assert world.ok(root, "search", "search")["results"][0]["title"] == "Search"


def test_names_are_paths_under_assets_without_md(world):  # noqa: F811
    root = world.project()
    for bad in ("plans/x.md", "/plans/x", "plans//x", "../x", "plans/./x", "a\\b"):
        done = world.run(root, "save", bad, "--from", "-", stdin=b"x\n")
        assert done.returncode == 6 and error(done)["code"] == "bad_name", bad
    kinds = {name: save(world, root, name, b"x\n")["document"]["kind"] for name in (
        "plans/a", "research/a", "sessions/a", "meetings/a", "journal/a", "digests/a",
        "reports/a", "inbox/a", "loose")}
    assert kinds == {"plans/a": "plan", "research/a": "research", "sessions/a": "session",
                     "meetings/a": "meeting", "journal/a": "journal", "digests/a": "digest",
                     "reports/a": "report", "inbox/a": "note", "loose": "note"}
    bad_body = world.run(root, "save", "notes/bin", "--from", "-", stdin=b"\xff\xfe\x00")
    assert bad_body.returncode == 6 and error(bad_body)["code"] == "bad_body"


def test_writes_refuse_under_the_read_only_switch(world):  # noqa: F811
    root = world.project()
    done = world.run(root, "save", "plans/x", "--from", "-", stdin=b"x\n",
                     CAPABILITIES_READ_ONLY="1")
    assert done.returncode == 4 and error(done)["code"] == "read_only_switch"


def _assets_folder(base: Path) -> dict[str, bytes]:
    files = {
        "plans/2026-10-20_plan.md": b"# Plan\n\n- one\n- two\n",
        "research/topic/analysis.md": "# Анализ\r\n\r\nCRLF and Cyrillic\r\n".encode(),
        "sessions/2026-10-20_s.md": b"no heading and no final newline",
        "journal/day.md": b"\xef\xbb\xbf# BOM kept\n",
        "inbox/loose-note.md": b"",
        "top.md": b"# Top\n\n```sh\n# not a title\n```\n",
        "research/topic/data.json": b"{}\n",
        "research/topic/run.py": b"print(1)\n",
        "plans/.gitkeep": b"",
        "reports/table.tsv": b"a\tb\n",
        "reports/latin1.md": "café\n".encode("latin-1"),
    }
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


def test_import_then_export_reproduces_every_markdown_file(world, tmp_path):  # noqa: F811
    root = world.project()
    files = _assets_folder(root / "assets")
    imported = world.ok(root, "import")
    outcomes = {row["path"]: row["outcome"] for row in imported["documents"]}
    markdown = {rel for rel in files if rel.endswith(".md") and rel != "reports/latin1.md"}
    assert set(outcomes) == markdown and set(outcomes.values()) == {"created"}
    skipped = {row["path"]: row["reason"] for row in imported["skipped"]}
    assert set(skipped) == {"research/topic/data.json", "research/topic/run.py",
                            "plans/.gitkeep", "reports/table.tsv", "reports/latin1.md"}
    assert skipped["plans/.gitkeep"] == "not markdown"
    assert skipped["reports/latin1.md"].startswith("not UTF-8")
    listed = {d["name"]: d for d in world.ok(root, "list")["documents"]}
    assert listed["research/topic/analysis"]["kind"] == "research"
    assert listed["research/topic/analysis"]["title"] == "Анализ"
    assert listed["top"]["title"] == "Top" and listed["top"]["kind"] == "note"
    assert listed["sessions/2026-10-20_s"]["title"] == "2026-10-20_s"
    again = world.ok(root, "import")
    assert {row["outcome"] for row in again["documents"]} == {"unchanged"}
    out = tmp_path / "exported"
    exported = world.ok(root, "export", str(out))
    assert exported["complete"] and exported["counts"]["written"] == len(markdown)
    written = {p.relative_to(out).as_posix(): p.read_bytes()
               for p in out.rglob("*") if p.is_file()}
    assert written == {rel: files[rel] for rel in markdown}
    (out / "top.md").write_bytes(b"# Top\n\nlocal edit\n")
    repeat = world.ok(root, "export", str(out))
    kept = {row["name"]: row["outcome"] for row in repeat["documents"]}
    assert kept["top"] == "kept" and not repeat["complete"]
    assert (out / "top.md").read_bytes() == b"# Top\n\nlocal edit\n"
    assert set(kept.values()) == {"unchanged", "kept"}


def test_projects_do_not_see_each_others_assets(world):  # noqa: F811
    one = world.project("one")
    two = world.project("two")
    save(world, one, "plans/shared-name", b"# One\n")
    assert world.ok(two, "list")["count"] == 0
    assert world.run(two, "show", "plans/shared-name").returncode == 3
    assert json.loads(world.run(two, "search", "One").stdout)["matched"] == 0

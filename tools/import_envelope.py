#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy a project envelope's records into a store, without touching the files.

The envelope stays exactly as it is; this reads it. Run it as many times as you
like — every write is an upsert keyed the same way, so a second run changes
nothing a first run did not.

Everything lands at ONE scope, the project's own, reproducing today's behaviour
exactly. Deciding that a mailbox is really a global fact is a judgement, and a
migration is the wrong place to make one: promote afterwards, deliberately, and
watch what changes. The one transformation it does make is structural rather
than editorial — `allow_write` is lifted out of each connection into a grant,
because that is the shape the store keeps connections in.

    uv run tools/import_envelope.py <project-root> --store <path> [--apply]

Without --apply it reports what it would write and writes nothing.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contract"))

from store import Scopes, StoreError, open_store  # noqa: E402

GRANT_KEYS = ("allow_write", "enabled")
MANAGER = "capabilities"
SERVICE_SETTINGS = ("telegram", "slack")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def plan(envelope: Path) -> list[dict]:
    """Every row the envelope implies, in the order it would be written."""
    rows: list[dict] = []

    gate = (_read_json(envelope / "settings.json") or {}).get("capabilities") or {}
    for name, entry in sorted(gate.items()):
        rows.append({"capability": MANAGER, "collection": "policy", "key": name,
                     "value": entry, "note": None})

    for reg_file in sorted(envelope.glob("*/connections.json")):
        capability = reg_file.parent.name
        registry = _read_json(reg_file) or {}
        default = registry.get("default")
        if default:
            rows.append({"capability": capability, "collection": "setting",
                         "key": "connection.default", "value": default, "note": None})
        for cid, entry in (registry.get("connections") or {}).items():
            if not isinstance(entry, dict):
                continue
            identity = {k: v for k, v in entry.items() if k not in GRANT_KEYS}
            rows.append({"capability": capability, "collection": "connection",
                         "key": cid, "value": identity, "note": None})
            grant = {k: entry[k] for k in GRANT_KEYS if k in entry}
            if grant:
                rows.append({"capability": capability, "collection": "grant",
                             "key": cid, "value": grant, "note": None})

    for ids_file in sorted(envelope.glob("*/identifiers.json")):
        capability = ids_file.parent.name
        body = _read_json(ids_file) or {}
        for label, entry in (body.get("identifiers") or body).items():
            if isinstance(entry, dict) and "value" in entry:
                value, note = entry["value"], entry.get("note")
            else:
                value, note = entry, None
            rows.append({"capability": capability, "collection": "identifier",
                         "key": label, "value": value, "note": note})

    for capability in SERVICE_SETTINGS:
        body = _read_json(envelope / capability / "service" / "settings.json")
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            rows.append({"capability": capability, "collection": "setting",
                         "key": key, "value": value, "note": None})

    config = envelope / "automations" / "service" / "config.toml"
    if config.is_file():
        try:
            raw = tomllib.loads(config.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
        # The automations array is a record class of its own and waits for the
        # table that will hold it; the engine and agent sections are settings.
        for section in ("engine", "agents"):
            if isinstance(raw.get(section), dict):
                rows.append({"capability": "automations", "collection": "setting",
                             "key": section, "value": raw[section], "note": None})
    return rows


def verify(store, rows: list[dict], scopes: Scopes) -> list[str]:
    """Read every row back through the resolver a capability would use and
    compare. A migration that cannot prove itself is a rewrite."""
    problems: list[str] = []
    for row in rows:
        got = store.config_get(row["capability"], row["collection"], row["key"], scopes)
        if got != row["value"]:
            problems.append(
                f"{row['capability']}/{row['collection']}/{row['key']}: "
                f"stored {got!r}, expected {row['value']!r}"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--store", required=True, help="sqlite path or postgresql:// url")
    ap.add_argument("--envelope", type=Path, default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = args.root.resolve()
    envelope = args.envelope or (root / "capabilities")
    if not envelope.is_dir():
        print(f"no envelope at {envelope}", file=sys.stderr)
        return 5

    identity = _read_json(envelope / "project.json") or {}
    project_id, slug = identity.get("id"), args.slug or identity.get("slug")
    if not project_id or not slug:
        print(f"no project identity in {envelope / 'project.json'} — "
              "run `capabilities init` there first", file=sys.stderr)
        return 5

    rows = plan(envelope)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["collection"]] = counts.get(row["collection"], 0) + 1

    report = {"project": {"id": project_id, "slug": slug}, "envelope": str(envelope),
              "rows": len(rows), "by_collection": counts, "applied": False}

    if not args.apply:
        report["sample"] = [f"{r['capability']}/{r['collection']}/{r['key']}"
                            for r in rows[:12]]
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    scopes = Scopes(project=slug)
    scope = ("project", slug)
    with open_store(args.store) as store:
        store.migrate()
        store.project_register(project_id, slug)
        store.project_bind_path(slug, "local", str(root))
        for row in rows:
            store.config_set(row["capability"], row["collection"], row["key"],
                             row["value"], scope, actor="import_envelope",
                             note=row["note"])
        problems = verify(store, rows, scopes)

    report["applied"] = True
    report["verified"] = not problems
    if problems:
        report["problems"] = problems
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not problems else 7


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StoreError as exc:
        print(json.dumps({"error": {"code": exc.slug, "message": exc.message,
                                    "hint": exc.hint}}), file=sys.stderr)
        sys.exit(6)

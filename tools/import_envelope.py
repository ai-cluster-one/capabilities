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


def _stamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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

    # `slack/policy.json` is deliberately not read. It restates four keys that
    # `slack/service/settings.json` also carries, and the two have already
    # diverged — the service file holds a real allowed user where the policy
    # file holds an empty map. Choosing between them is a judgement about which
    # is live, and a migration is the wrong place to make one.

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


def _script_key(script_path: str) -> str:
    """A script's document key, by the same rule `plan_documents` uses, so the
    row and the document agree on a name without either naming the other."""
    return "script." + Path(script_path).stem.replace("_", "-").lower()


def _toml_comments(text: str) -> list[str]:
    """The comment block standing above each `[[automations]]` header, in order.

    `tomllib` throws comments away, and for this file that would throw away the
    only record of WHY each automation exists — which is exactly what the
    description column is for. So the text is read twice: once for the values,
    once for the prose above them."""
    blocks, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            current.append(stripped.lstrip("#").strip())
        elif stripped == "[[automations]]":
            blocks.append(" ".join(current).strip() or None)
            current = []
        elif stripped:
            current = []
    return blocks


def plan_automations(envelope: Path) -> list[dict]:
    """The `[[automations]]` array as rows. The one transformation: `script`
    stops being a path into the repository and becomes the key of a document,
    because a row that points at a file has not actually moved anywhere."""
    config = envelope / "automations" / "service" / "config.toml"
    if not config.is_file():
        return []
    text = config.read_text()
    try:
        raw = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    descriptions = _toml_comments(text)
    out = []
    for index, item in enumerate(raw.get("automations") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append({
            "slug": item["id"],
            "name": item.get("name"),
            "description": item.get("description")
            or (descriptions[index] if index < len(descriptions) else None),
            "enabled": 1 if item.get("enabled", True) else 0,
            "script_key": _script_key(str(item.get("script") or "")),
            "schedule": item.get("schedule"),
            "every_seconds": item.get("every_seconds"),
            "timeout_seconds": float(item.get("timeout_seconds", 300)),
            "max_parallel": int(item.get("max_parallel", 1)),
            "max_pending": int(item.get("max_pending", 1)),
            "overlap": str(item.get("overlap") or "skip"),
            "retries": int(item.get("retries", 0)),
            "arguments": item.get("arguments") or [],
            "environments": item.get("environments") or [],
        })
    return out


# Long text is imported as a document version and pinned, because the file on
# disk IS the version in force today — a migration that recorded the text but
# pinned nothing would quietly deploy nothing.
DOCUMENT_SOURCES = (
    ("*/reference/*.md", "reference"),
    ("*/service/context/*.md", "context"),
    ("*/service/context.md", None),
    ("*/service/voice-agent.md", None),
    ("geminitalk/base.md", None),
    ("automations/scripts/*.py", "script"),
    ("automations/scripts/*.json", "script"),
)

MEDIA_TYPES = {".md": "text/markdown", ".py": "text/x-python", ".json": "application/json"}


def plan_documents(envelope: Path) -> list[dict]:
    """Every long-text file the envelope holds, keyed so its origin stays legible."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for pattern, prefix in DOCUMENT_SOURCES:
        for path in sorted(envelope.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            capability = path.relative_to(envelope).parts[0]
            key = f"{prefix}.{path.stem}" if prefix else path.stem
            key = key.replace("_", "-").lower()
            if (capability, key) in seen:
                continue
            seen.add((capability, key))
            try:
                body = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            out.append({"capability": capability, "key": key, "body": body,
                        "media_type": MEDIA_TYPES.get(path.suffix),
                        "source": str(path.relative_to(envelope))})
    return out


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
    documents = plan_documents(envelope)
    automations = plan_automations(envelope)
    document_keys = {(d["capability"], d["key"]) for d in documents}
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["collection"]] = counts.get(row["collection"], 0) + 1

    report = {"project": {"id": project_id, "slug": slug}, "envelope": str(envelope),
              "rows": len(rows), "by_collection": counts,
              "documents": len(documents), "automations": len(automations),
              "applied": False}
    orphan_scripts = [a["slug"] for a in automations
                      if ("automations", a["script_key"]) not in document_keys]
    if orphan_scripts:
        report["automations_without_a_script"] = orphan_scripts

    if not args.apply:
        report["sample"] = [f"{r['capability']}/{r['collection']}/{r['key']}"
                            for r in rows[:8]]
        report["document_sample"] = [f"{d['capability']}/{d['key']} <- {d['source']}"
                                     for d in documents]
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
        if automations:
            # The schema is the capability's to declare, so it is read from the
            # capability source rather than restated here.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                                   / "capabilities" / "automations" / "service"))
            import runtime as _automations_runtime  # noqa: PLC0415
            store.migrate(_automations_runtime.STORE_NAMESPACE,
                          _automations_runtime.STORE_VERSION,
                          _automations_runtime.STORE_MIGRATIONS)
            project_id = store._project_id(slug)
            for a in automations:
                _automations_runtime.store_upsert(store, "project", project_id, a)
            store._conn.commit()
        for doc in documents:
            store.context_put(doc["capability"], doc["key"], doc["body"], scope,
                              author="import_envelope", media_type=doc["media_type"],
                              activate=True)
        problems = verify(store, rows, scopes)
        for doc in documents:
            got = store.context_read(doc["capability"], doc["key"], scopes)
            if not got or got["body"] != doc["body"]:
                problems.append(f"{doc['capability']}/{doc['key']}: body differs")

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

"""The service's own prompts, kept as versioned rows in the project's store.

A prompt is the text a run class is given whole - `context` for a dialogue
turn, `job-worker` for a job, a provider's voice prompts, a channel's
`context.<x>` - by the document key `settings_schema.document_key` folds its
file name to. Each save is a new row of `telegram_prompts`; exactly one row per
project and key is active, and the store refuses any change to a row but
clearing `active`, so every earlier version stays readable as it was.

The table is this capability's own, created by step `0005-prompts` of the
register's ledger (`jobs.STORE_STEPS`), so it exists wherever the register
opens. The files under `capabilities/telegram/service/` are what `import`
reads and `export` writes; the service reads a key's active row, and its file
only while the store holds no version of that key. Reference documents
(`reference.<x>`) are not prompts and stay files.

Every function takes an autocommit psycopg connection bound to the store's
schema - the register's own, `JobStore.conn` - and the project's id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

TABLE = "telegram_prompts"
# Where a key's file sits under a service directory: `context.<x>` in
# `context/`, every other key beside settings.json.
CONTEXT_PREFIX = "context."
# The key the records layer gives a reference document, which is not a prompt.
REFERENCE_PREFIX = "reference."


class PromptError(Exception):
    """A save the store refused, as the CLI reports it."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug, self.message, self.hint = slug, message, hint


def is_prompt_key(key: str) -> bool:
    return bool(key) and not key.startswith(REFERENCE_PREFIX)


_COLUMNS = "id::text, key, body, active, created_at, created_by, note"


def _row(values) -> dict:
    vid, key, body, active, created_at, created_by, note = values
    return {"id": vid, "key": key, "body": body, "active": active,
            "created_at": created_at.isoformat() if created_at else None,
            "created_by": created_by, "note": note}


def active(conn, project_id: str, key: str) -> dict | None:
    """The version of `key` in force for the project, or None where the store
    holds none."""
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM {TABLE} WHERE project_id = %s AND key = %s AND active",
        (project_id, key)).fetchone()
    return _row(found) if found else None


def version(conn, project_id: str, key: str, version_id: str) -> dict | None:
    """One version of `key`, active or not, or None where it is not one."""
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM {TABLE} WHERE project_id = %s AND key = %s "
        "AND id::text = %s", (project_id, key, version_id)).fetchone()
    return _row(found) if found else None


def versions(conn, project_id: str, key: str) -> list[dict]:
    """Every version of `key`, newest first, without their text."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM {TABLE} WHERE project_id = %s AND key = %s "
        "ORDER BY created_at DESC, id", (project_id, key)).fetchall()
    return [{k: v for k, v in _row(r).items() if k != "body"} for r in rows]


def keys(conn, project_id: str) -> list[dict]:
    """Every key the project holds a version of: its active version and how
    many versions it has."""
    rows = conn.execute(
        f"SELECT key, count(*), max(id::text) FILTER (WHERE active), "
        f"max(created_at) FILTER (WHERE active) FROM {TABLE} WHERE project_id = %s "
        "GROUP BY key ORDER BY key", (project_id,)).fetchall()
    return [{"key": key, "versions": count, "active": vid,
             "active_since": since.isoformat() if since else None}
            for key, count, vid, since in rows]


def save(conn, project_id: str, key: str, body: str, created_by: str,
         note: str | None = None, base: str | None = None) -> tuple[dict, bool]:
    """Make `body` the active version of `key`, as a new row that retires the
    one it replaces in the same transaction. A body equal to the active one
    saves nothing. `base`, where given, is the version the writer read: a save
    over any other version is refused as `conflict`, and so is one that lost a
    race for the same key. Answers the active version and whether it is new."""
    if not is_prompt_key(key):
        raise PromptError("input", f"{key!r} is not a prompt key",
                          "a prompt key is a service file's name without `.md`; "
                          "reference documents stay files")
    from psycopg import errors
    try:
        with conn.transaction():
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM {TABLE} WHERE project_id = %s AND key = %s "
                "AND active FOR UPDATE", (project_id, key)).fetchone()
            current = _row(current) if current else None
            if base is not None and (current or {}).get("id") != base:
                raise PromptError(
                    "conflict",
                    f"the active version of {key} is {(current or {}).get('id')}, "
                    f"not {base}",
                    f"read it again with `telegram prompts show {key}` and save over it")
            if current is not None and current["body"] == body:
                return current, False
            if current is not None:
                conn.execute(f"UPDATE {TABLE} SET active = false WHERE id = %s::uuid",
                             (current["id"],))
            found = conn.execute(
                f"INSERT INTO {TABLE} (project_id, key, body, created_by, note) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (project_id, key, body, created_by, note)).fetchone()
    except errors.UniqueViolation as exc:
        raise PromptError("conflict", f"another save of {key} landed first",
                          f"read it again with `telegram prompts show {key}`") from exc
    except errors.CheckViolation as exc:
        raise PromptError("input", f"the store refused {key!r}: {exc}") from exc
    return _row(found), True


# --- The file form ---------------------------------------------------------------

def files(directory: Path, key_of: Callable[[Path], str]) -> dict[str, Path]:
    """The prompt files a service directory holds, by key: `*.md` beside
    settings.json and `context/*.md`, the first file a key folds from winning
    as it does for the daemon. `key_of` is `settings_schema.document_key`."""
    found: dict[str, Path] = {}
    for folder in (directory, directory / "context"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.is_file():
                found.setdefault(key_of(path.relative_to(directory)), path)
    return found


def file_for(directory: Path, key: str, key_of: Callable[[Path], str]) -> Path:
    """Where `key`'s file is in `directory`: the file it already folds from,
    else its canonical name."""
    existing = files(directory, key_of).get(key)
    if existing is not None:
        return existing
    if key.startswith(CONTEXT_PREFIX):
        return directory / "context" / f"{key[len(CONTEXT_PREFIX):]}.md"
    return directory / f"{key}.md"


def import_files(conn, project_id: str, directory: Path, key_of: Callable[[Path], str],
                 created_by: str, note: str | None = None) -> list[dict]:
    """Save every prompt file in `directory` whose text is not the active
    version already; answers each key's outcome."""
    report = []
    for key, path in sorted(files(directory, key_of).items()):
        body = path.read_text(encoding="utf-8")
        row, saved = save(conn, project_id, key, body, created_by, note=note)
        report.append({"key": key, "file": str(path), "version": row["id"],
                       "outcome": "saved" if saved else "unchanged"})
    return report


def export_files(conn, project_id: str, directory: Path,
                 key_of: Callable[[Path], str]) -> list[dict]:
    """Write every key's active version into `directory` in the file form; a
    file already equal is left alone."""
    report = []
    for entry in keys(conn, project_id):
        row = active(conn, project_id, entry["key"])
        if row is None:
            continue
        path = file_for(directory, row["key"], key_of)
        if path.is_file() and path.read_text(encoding="utf-8") == row["body"]:
            outcome = "unchanged"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(row["body"], encoding="utf-8")
            outcome = "written"
        report.append({"key": row["key"], "file": str(path), "version": row["id"],
                       "outcome": outcome})
    return report

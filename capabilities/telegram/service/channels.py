"""The service's register, one row per channel in the project's store.

The register is what decides whether a message is answered: per channel - a
chat, or one forum topic in it - the watermark (`last_processed_message_id`),
the channel's `/set` overrides, which outgoing voice echoes speak for whom, and
the dialogue jobs reserved and not yet finished. It is a row of
`telegram_channels`, keyed by project, environment, connection and channel, so
whichever node runs the service next reads the register the last one left,
rather than one of its own on that machine's disk.

The table is this capability's own, created by step `0006-channels` of the
register's ledger (`jobs.STORE_STEPS`). The daemon holds the register as one
dict of rows, as it always has, and every write is `write`: the rows that
changed since the store last held them, in one transaction, so a job leaving
its row and the watermark passing it are one commit. The watermark is written
as the greater of the stored value and the new one, so a node working from an
older copy can never move it back.

`import_file` carries a `register.json` from before into the store once: per
channel, the copy whose watermark is newer wins whole, and the store's copy wins
a tie, since it is the one in use.

Every function takes an autocommit psycopg connection bound to the store's
schema - the register's own, `JobStore.conn` - and the scope
`(project_id, environment, connection)`.
"""

from __future__ import annotations

import json
from typing import Iterable

TABLE = "telegram_channels"
# The fields of a register row that have a column of their own; everything
# else in the row is kept in `state` as the daemon wrote it.
WATERMARK = "last_processed_message_id"
SETTINGS = "settings"
VOICE_ECHO_SENDERS = "voice_echo_senders"
_OWN_COLUMNS = (WATERMARK, SETTINGS, VOICE_ECHO_SENDERS)


def _watermark(row: dict) -> int:
    try:
        return max(int(row.get(WATERMARK) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _mapping(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def _row(watermark, settings, voice_echo_senders, state) -> dict:
    """A register row as the daemon holds it."""
    row = _mapping(state)
    row[WATERMARK] = int(watermark or 0)
    settings = _mapping(settings)
    if settings:
        row[SETTINGS] = settings
    echoes = _mapping(voice_echo_senders)
    if echoes:
        row[VOICE_ECHO_SENDERS] = echoes
    return row


def _values(row: dict) -> tuple:
    state = {k: v for k, v in row.items() if k not in _OWN_COLUMNS}
    return (_watermark(row),
            json.dumps(_mapping(row.get(SETTINGS)), ensure_ascii=False),
            json.dumps(_mapping(row.get(VOICE_ECHO_SENDERS)), ensure_ascii=False),
            json.dumps(state, ensure_ascii=False))


def rows(conn, scope: tuple[str, str, str]) -> dict:
    """The whole register for the scope, by channel key."""
    found = conn.execute(
        f"SELECT channel_key, {WATERMARK}, settings, voice_echo_senders, state "
        f"FROM {TABLE} WHERE project_id = %s AND environment = %s AND connection = %s",
        tuple(scope)).fetchall()
    return {key: _row(watermark, settings, echoes, state)
            for key, watermark, settings, echoes, state in found}


_UPSERT = f"""
    INSERT INTO {TABLE} AS t (project_id, environment, connection, channel_key,
                              {WATERMARK}, settings, voice_echo_senders, state)
    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
    ON CONFLICT (project_id, environment, connection, channel_key) DO UPDATE SET
        {WATERMARK} = GREATEST(t.{WATERMARK}, EXCLUDED.{WATERMARK}),
        settings = EXCLUDED.settings,
        voice_echo_senders = EXCLUDED.voice_echo_senders,
        state = EXCLUDED.state,
        updated_at = now()"""


def write(conn, scope: tuple[str, str, str], changed: dict,
          removed: Iterable[str] = ()) -> None:
    """Write the changed rows and drop the removed ones, in one transaction."""
    with conn.transaction():
        for key, row in changed.items():
            conn.execute(_UPSERT, (*scope, str(key), *_values(row)))
        for key in removed:
            conn.execute(
                f"DELETE FROM {TABLE} WHERE project_id = %s AND environment = %s "
                "AND connection = %s AND channel_key = %s", (*scope, str(key)))


def import_file(conn, scope: tuple[str, str, str], register: dict) -> list[str]:
    """Carry an older `register.json` into the store, in one transaction: each
    channel whose file row is newer than the stored one, or that the store does
    not hold, is written whole. The channel keys taken from the file."""
    taken = []
    with conn.transaction():
        for key, row in register.items():
            if not isinstance(row, dict):
                continue
            stored = conn.execute(
                f"SELECT {WATERMARK} FROM {TABLE} WHERE project_id = %s "
                "AND environment = %s AND connection = %s AND channel_key = %s "
                "FOR UPDATE", (*scope, str(key))).fetchone()
            if stored is not None and _watermark(row) <= int(stored[0] or 0):
                continue
            conn.execute(_UPSERT, (*scope, str(key), *_values(row)))
            taken.append(str(key))
    return taken

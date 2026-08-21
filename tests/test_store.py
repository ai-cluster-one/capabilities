"""The store tier: scope resolution, the additive-migration guard, revisions.

The three resolution semantics are the part worth pinning down, because each
one exists to satisfy a different standing rule and they disagree on purpose.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "contract"))

from store import (
    COLLECTIONS,
    PostgresStore,
    Scopes,
    SQLiteStore,
    StoreError,
    open_store,
)

# The same suite runs against every backend the host can reach, which is the
# only real proof that the adapter boundary holds: set STORE_TEST_PG to a DSN
# for a THROWAWAY database and Postgres joins the parameter list.
_PG_DSN = os.environ.get("STORE_TEST_PG")
_BACKENDS = ["sqlite"] + (["postgres"] if _PG_DSN else [])


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SQLiteStore.open(str(tmp_path / "store.db"))
    else:
        s = PostgresStore.open(_PG_DSN)
        with s.transaction():
            s._execute("DROP SCHEMA public CASCADE")
            s._execute("CREATE SCHEMA public")
    s.migrate()
    yield s
    s.close()


@pytest.fixture()
def scopes():
    return Scopes(project="marvin", instance="kz-mbp")


# --- scope model -------------------------------------------------------------

def test_chain_is_highest_first(scopes):
    assert scopes.chain() == [("instance", "kz-mbp"), ("project", "marvin"), ("global", "")]


def test_chain_skips_absent_scopes():
    assert Scopes().chain() == [("global", "")]
    assert Scopes(project="marvin").chain() == [("project", "marvin"), ("global", "")]


def test_write_target_refuses_a_scope_it_has_no_name_for():
    with pytest.raises(StoreError) as exc:
        Scopes().write_target("project")
    assert exc.value.slug == "no_project_scope"


def test_global_scope_takes_no_name(store):
    with pytest.raises(StoreError) as exc:
        store.config_set("telegram", "setting", "a", 1, ("global", "oops"))
    assert exc.value.slug == "bad_scope"


# --- MERGE: rule 17, an absent higher entry inherits the lower ---------------

def test_merge_inherits_per_key(store, scopes):
    store.config_set("telegram", "setting", "tick", 1, ("global", ""))
    store.config_set("telegram", "setting", "voice", "on", ("global", ""))
    store.config_set("telegram", "setting", "tick", 5, ("project", "marvin"))

    assert store.config_get("telegram", "setting", "tick", scopes) == 5
    assert store.config_get("telegram", "setting", "voice", scopes) == "on"


def test_merge_lets_instance_beat_project(store, scopes):
    store.config_set("telegram", "setting", "model", "sol", ("global", ""))
    store.config_set("telegram", "setting", "model", "terra", ("project", "marvin"))
    store.config_set("telegram", "setting", "model", "local", ("instance", "kz-mbp"))

    assert store.config_get("telegram", "setting", "model", scopes) == "local"
    assert store.config_origin("telegram", "setting", "model", scopes) == ("instance", "kz-mbp")


def test_policy_gate_merges_like_the_file_gate_did(store, scopes):
    store.config_set("capabilities", "policy", "telegram", {"enabled": True}, ("global", ""))
    store.config_set("capabilities", "policy", "slack", {"enabled": True}, ("global", ""))
    store.config_set("capabilities", "policy", "slack", {"enabled": False}, ("project", "marvin"))

    resolved = store.config_resolve("capabilities", "policy", scopes)
    assert resolved["telegram"]["value"] == {"enabled": True}
    assert resolved["slack"]["value"] == {"enabled": False}


# --- FIRST: rule 18, the highest scope holding anything wins whole -----------

def test_first_never_merges_a_half_identity(store, scopes):
    store.config_set("telegram", "connection", "kz", {"api_id": 1}, ("global", ""))
    store.config_set("telegram", "connection", "marvin", {"api_id": 2}, ("global", ""))
    store.config_set("telegram", "connection", "marvin", {"api_id": 3}, ("project", "marvin"))

    resolved = store.config_resolve("telegram", "connection", scopes)
    # The project scope holds an entry, so it wins whole: the global-only "kz"
    # connection does NOT leak through.
    assert set(resolved) == {"marvin"}
    assert resolved["marvin"]["value"] == {"api_id": 3}


def test_first_falls_through_an_empty_scope(store, scopes):
    store.config_set("telegram", "connection", "kz", {"api_id": 1}, ("global", ""))
    resolved = store.config_resolve("telegram", "connection", scopes)
    assert set(resolved) == {"kz"}
    assert resolved["kz"]["scope_kind"] == "global"


def test_the_two_semantics_disagree_on_the_same_data(store, scopes):
    """The same rows resolve differently by collection — which is the point."""
    for collection in ("connection", "setting"):
        store.config_set("x", collection, "only_global", "g", ("global", ""))
        store.config_set("x", collection, "overridden", "p", ("project", "marvin"))

    assert set(store.config_resolve("x", "setting", scopes)) == {"only_global", "overridden"}
    assert set(store.config_resolve("x", "connection", scopes)) == {"overridden"}


# --- EXACT: rule 16, state does not cascade ----------------------------------

def test_state_does_not_fall_back_to_a_lower_scope(store):
    store.state_set("telegram", "cursor", 42, ("project", "marvin"))
    assert store.state_get("telegram", "cursor", ("project", "marvin")) == 42
    assert store.state_get("telegram", "cursor", ("instance", "kz-mbp")) is None
    assert store.state_get("telegram", "cursor", ("global", "")) is None


def test_state_is_not_reachable_as_config(store, scopes):
    COLLECTIONS["ephemeral"] = {"resolve": "exact", "writer": "capability"}
    try:
        with pytest.raises(StoreError) as exc:
            store.config_resolve("telegram", "ephemeral", scopes)
        assert exc.value.slug == "bad_collection"
    finally:
        del COLLECTIONS["ephemeral"]


def test_expired_state_reads_as_absent_and_sweeps(store):
    store.state_set("telegram", "lease", "held", ("project", "marvin"), ttl_seconds=-5)
    assert store.state_get("telegram", "lease", ("project", "marvin")) is None
    assert store.state_sweep() == 1
    assert store.state_sweep() == 0


def test_live_state_survives_a_sweep(store):
    store.state_set("telegram", "lease", "held", ("project", "marvin"), ttl_seconds=600)
    assert store.state_sweep() == 0
    assert store.state_get("telegram", "lease", ("project", "marvin")) == "held"


# --- revisions: what git used to answer --------------------------------------

def test_every_write_records_who_and_what_it_was(store):
    store.config_set("telegram", "setting", "model", "sol", ("global", ""), actor="kz")
    store.config_set("telegram", "setting", "model", "terra", ("global", ""), actor="marvin")

    log = store.revisions("telegram", "setting", "model")
    assert [r["actor"] for r in log] == ["marvin", "kz"]
    assert log[0]["old_value"] == "sol" and log[0]["new_value"] == "terra"
    assert log[1]["old_value"] is None


def test_a_delete_is_recorded_too(store):
    store.config_set("telegram", "setting", "model", "sol", ("global", ""), actor="kz")
    assert store.config_delete("telegram", "setting", "model", ("global", ""), actor="kz") is True
    assert store.config_delete("telegram", "setting", "model", ("global", ""), actor="kz") is False

    latest = store.revisions("telegram", "setting", "model")[0]
    assert latest["old_value"] == "sol" and latest["new_value"] is None


# --- migrations --------------------------------------------------------------

def test_migrate_is_idempotent(store):
    assert store.schema_version() == 1
    assert store.migrate() == 1


def test_a_capability_owns_its_own_namespace(store):
    store.migrate("automations", 1, ["CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY)"])
    assert store.schema_version("automations") == 1
    assert store.schema_version("core") == 1


@pytest.mark.parametrize("step", [
    "DROP TABLE runs",
    "ALTER TABLE runs DROP COLUMN summary",
    "ALTER TABLE runs RENAME TO jobs",
])
def test_destructive_migration_steps_are_refused(store, step):
    with pytest.raises(StoreError) as exc:
        store.migrate("automations", 1, [step])
    assert exc.value.slug == "destructive_migration"


def test_an_additive_step_is_allowed(store):
    store.migrate("automations", 1, ["CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY)"])
    store.migrate("automations", 2, ["ALTER TABLE runs ADD COLUMN note TEXT"])
    assert store.schema_version("automations") == 2


# --- naming and validation ---------------------------------------------------

def test_unknown_collection_is_refused(store, scopes):
    with pytest.raises(StoreError) as exc:
        store.config_get("telegram", "nonsense", "k", scopes)
    assert exc.value.slug == "bad_collection"


def test_capability_names_are_validated(store):
    with pytest.raises(StoreError) as exc:
        store.config_set("Not A Name", "setting", "k", 1, ("global", ""))
    assert exc.value.slug == "bad_name"


def test_values_round_trip_every_json_type(store, scopes):
    cases = {"b": True, "n": 42, "f": 1.5, "s": "text", "l": [1, 2], "o": {"a": {"b": 1}}}
    for key, value in cases.items():
        store.config_set("telegram", "setting", key, value, ("global", ""))
    for key, value in cases.items():
        assert store.config_get("telegram", "setting", key, scopes) == value


def test_a_type_column_is_unnecessary(store, scopes):
    """JSON already carries the type; storing one is what the type column was for."""
    store.config_set("telegram", "setting", "enabled", False, ("global", ""))
    got = store.config_get("telegram", "setting", "enabled", scopes)
    assert got is False and isinstance(got, bool)


# --- health and resolution ---------------------------------------------------

def test_health_reports_dialect_and_namespaces(store):
    health = store.health()
    assert health["dialect"] == store.dialect
    assert health["namespaces"] == {"core": 1}
    assert health["roundtrip_ms"] >= 0


def test_open_store_routes_by_scheme(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPABILITIES_STORE_URL", raising=False)
    with pytest.raises(StoreError) as exc:
        open_store()
    assert exc.value.slug == "no_store_url"

    with open_store(str(tmp_path / "s.db")) as s:
        assert s.dialect == "sqlite"

    with pytest.raises(StoreError) as exc:
        open_store("mysql://host/db")
    assert exc.value.slug == "unknown_store"


def test_postgres_adapter_translates_placeholders():
    """The one dialect difference the base class hides, checked without a server."""
    translate = PostgresStore._sql
    assert translate(PostgresStore(None), "SELECT ? FROM t WHERE a = ?") == \
        "SELECT %s FROM t WHERE a = %s"
    assert SQLiteStore._sql(SQLiteStore(None), "SELECT ?") == "SELECT ?"


def test_sqlite_stores_json_as_text(tmp_path):
    """The dialect difference the encode/decode hooks hide from every caller."""
    with SQLiteStore.open(str(tmp_path / "s.db")) as s:
        s.migrate()
        s.config_set("telegram", "setting", "k", {"a": 1}, ("global", ""))
        raw = s.connection.execute("SELECT value FROM config WHERE key = 'k'").fetchone()[0]
    assert isinstance(raw, str) and json.loads(raw) == {"a": 1}

"""The store tier: scope resolution, the additive-migration guard, revisions.

The three resolution semantics are the part worth pinning down, because each
one exists to satisfy a different standing rule and they disagree on purpose.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "contract"))

from store import (
    COLLECTIONS,
    HASH_LENGTH as SCHEMA_HASH_LENGTH,
    SCHEMA_VERSION,
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


ATLAS_ID = "3f1c9b4e-0000-4d92-9a11-0c5e8f2d6a44"
OTHER_ID = "3f1c9b4e-1111-4d92-9a11-111111111111"


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
    # A record scoped to a project needs the project to exist; rows refer to it
    # by id, and there is no id until someone claims the slug.
    s.project_register(ATLAS_ID, "atlas")
    yield s
    s.close()


@pytest.fixture()
def scopes():
    return Scopes(project="atlas")


# --- scope model -------------------------------------------------------------

def test_chain_is_highest_first(store, scopes):
    assert store._chain(scopes) == [("project", ATLAS_ID), ("global", None)]


def test_chain_skips_a_project_nobody_registered(store):
    assert store._chain(Scopes()) == [("global", None)]
    assert store._chain(Scopes(project="never-claimed")) == [("global", None)]


def test_the_global_scope_takes_no_project(store):
    with pytest.raises(StoreError) as exc:
        store.config_set("telegram", "setting", "a", 1, ("global", "atlas"))
    assert exc.value.slug == "bad_scope"


def test_writing_to_an_unregistered_project_is_refused(store):
    with pytest.raises(StoreError) as exc:
        store.config_set("telegram", "setting", "a", 1, ("project", "ghost"))
    assert exc.value.slug == "unknown_project"


def test_write_target_refuses_a_scope_it_has_no_name_for():
    with pytest.raises(StoreError) as exc:
        Scopes().write_target("project")
    assert exc.value.slug == "no_project_scope"


def test_write_target_names_the_two_scopes_there_are():
    assert Scopes(project="atlas").write_target("project") == ("project", "atlas")
    assert Scopes(project="atlas").write_target("global") == ("global", None)


# --- MERGE: rule 17, an absent higher entry inherits the lower ---------------

def test_merge_inherits_per_key(store, scopes):
    store.config_set("telegram", "setting", "tick", 1, ("global", ""))
    store.config_set("telegram", "setting", "voice", "on", ("global", ""))
    store.config_set("telegram", "setting", "tick", 5, ("project", "atlas"))

    assert store.config_get("telegram", "setting", "tick", scopes) == 5
    assert store.config_get("telegram", "setting", "voice", scopes) == "on"


def test_a_project_beats_global(store, scopes):
    store.config_set("telegram", "setting", "model", "sol", ("global", ""))
    store.config_set("telegram", "setting", "model", "terra", ("project", "atlas"))

    assert store.config_get("telegram", "setting", "model", scopes) == "terra"
    assert store.config_origin("telegram", "setting", "model", scopes) == ("project", ATLAS_ID)


def test_policy_gate_merges_like_the_file_gate_did(store, scopes):
    store.config_set("capabilities", "policy", "telegram", {"enabled": True}, ("global", ""))
    store.config_set("capabilities", "policy", "slack", {"enabled": True}, ("global", ""))
    store.config_set("capabilities", "policy", "slack", {"enabled": False}, ("project", "atlas"))

    resolved = store.config_resolve("capabilities", "policy", scopes)
    assert resolved["telegram"]["value"] == {"enabled": True}
    assert resolved["slack"]["value"] == {"enabled": False}


# --- FIRST: the highest scope holding anything wins whole --------------------

@pytest.fixture()
def isolated_collection():
    """FIRST is available for a project that must not see the global set at all
    — client work where personal connections may not show through."""
    COLLECTIONS["isolated"] = {"resolve": "first", "writer": "human"}
    yield "isolated"
    del COLLECTIONS["isolated"]


def test_first_takes_one_scope_whole(store, scopes, isolated_collection):
    store.config_set("telegram", isolated_collection, "kz", {"api_id": 1}, ("global", ""))
    store.config_set("telegram", isolated_collection, "atlas", {"api_id": 3}, ("project", "atlas"))

    resolved = store.config_resolve("telegram", isolated_collection, scopes)
    assert set(resolved) == {"atlas"}  # the global-only "kz" does not leak through


def test_first_falls_through_an_empty_scope(store, scopes, isolated_collection):
    store.config_set("telegram", isolated_collection, "kz", {"api_id": 1}, ("global", ""))
    resolved = store.config_resolve("telegram", isolated_collection, scopes)
    assert set(resolved) == {"kz"}
    assert resolved["kz"]["scope"] == "global"


def test_the_two_semantics_disagree_on_the_same_data(store, scopes, isolated_collection):
    """The same rows resolve differently by collection — which is the point."""
    for collection in (isolated_collection, "setting"):
        store.config_set("x", collection, "only_global", "g", ("global", ""))
        store.config_set("x", collection, "overridden", "p", ("project", "atlas"))

    assert set(store.config_resolve("x", "setting", scopes)) == {"only_global", "overridden"}
    assert set(store.config_resolve("x", isolated_collection, scopes)) == {"overridden"}


# --- connection + grant: identity is a fact, permission is a decision --------

ATLAS_BOX = {"address": "assistant@example.com", "imap_host": "mail.example.com",
              "imap_port": 993, "secret_env": "MAILBOX_ATLAS_APP_PASSWORD"}


def test_a_project_grants_write_without_restating_the_identity(store, scopes):
    """The whole point: one grant row, and not one field of the box repeated."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": False}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True, "allow_write": True},
                     ("project", "atlas"))

    effective = store.connections_effective("mailbox", scopes)
    assert effective["atlas"]["allow_write"] is True
    assert effective["atlas"]["value"] == ATLAS_BOX          # identity untouched
    assert effective["atlas"]["scope"] == ("global", None)      # and still global
    assert effective["atlas"]["grant_scope"] == ("project", ATLAS_ID)


def test_a_project_can_disable_a_globally_declared_connection(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "connection", "osyris", {"address": "osyris@gmail.com"}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))
    store.config_set("mailbox", "grant", "osyris", {"enabled": False}, ("project", "atlas"))

    assert set(store.connections_effective("mailbox", scopes)) == {"atlas"}
    both = store.connections_effective("mailbox", scopes, include_disabled=True)
    assert both["osyris"]["enabled"] is False


def test_a_project_only_connection_lives_beside_the_global_ones(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))
    store.config_set("mailbox", "connection", "client", {"address": "a@client.tld"},
                     ("project", "atlas"))

    effective = store.connections_effective("mailbox", scopes)
    assert set(effective) == {"atlas", "client"}
    assert effective["client"]["scope"] == ("project", ATLAS_ID)


def test_a_project_may_replace_a_global_identity_whole(store, scopes):
    """Entry-level merge, never field-level: the project's row is taken whole,
    so no connection is ever assembled out of two scopes."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "connection", "atlas", {"address": "other@x.tld"},
                     ("project", "atlas"))

    value = store.connections_effective("mailbox", scopes)["atlas"]["value"]
    assert value == {"address": "other@x.tld"}
    assert "imap_host" not in value  # nothing inherited from the global entry


def test_writability_falls_back_to_the_capability_default(store, scopes):
    store.config_set("callva", "connection", "smart-id", {"k": 1}, ("global", ""))
    store.config_set("callva", "grant", "smart-id", {"enabled": True}, ("project", "atlas"))
    assert store.connections_effective("callva", scopes)["smart-id"]["allow_write"] is False
    assert store.connections_effective("callva", scopes, write_default=True)["smart-id"]["allow_write"] is True


def test_a_grant_naming_an_unknown_field_is_refused(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_read": True}, ("global", ""))
    with pytest.raises(StoreError) as exc:
        store.connections_effective("mailbox", scopes)
    assert exc.value.slug == "bad_grant"


def test_a_grant_aimed_at_nothing_is_reported_rather_than_dropped(store, scopes):
    """A mistyped id would otherwise mean permission silently not granted, which
    looks exactly like permission correctly withheld."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))
    store.config_set("mailbox", "grant", "marvni", {"allow_write": True}, ("project", "atlas"))

    assert set(store.connections_effective("mailbox", scopes)) == {"atlas"}
    orphans = store.grant_orphans("mailbox", scopes)
    assert [o["key"] for o in orphans] == ["marvni"]
    assert orphans[0]["scope"] == ("project", ATLAS_ID)


def test_a_grant_that_lands_is_not_reported_as_an_orphan(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": True}, ("project", "atlas"))
    assert store.grant_orphans("mailbox", scopes) == []


# --- who may use a connection: the project's own grant, and nothing else ------
#
# An identity that resolves at project scope is a project that already said
# yes -- declaring it locally is the act of permission. An identity inherited
# from the global scope is withheld until this project's own grant says so, so
# one line in the machine's config cannot hand the same connection to every
# project on it.

def test_a_global_identity_is_withheld_until_this_project_grants_it(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    assert store.connections_effective("mailbox", scopes) == {}


def test_a_project_grant_is_what_opens_a_global_identity(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))

    effective = store.connections_effective("mailbox", scopes)
    assert effective["atlas"]["enabled"] is True
    assert effective["atlas"]["scope"] == ("global", None)
    assert effective["atlas"]["grant_scope"] == ("project", ATLAS_ID)


def test_a_global_grant_is_never_the_blessing(store, scopes):
    """It would restore the hole everywhere at once, which is the whole reason
    the decision has to resolve where the project can see it."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("global", ""))
    assert store.connections_effective("mailbox", scopes) == {}


def test_a_project_grant_that_decides_only_writability_is_not_a_blessing(store, scopes):
    """`allow_write` answers what may be done with a connection, never whether
    this project may reach it at all."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": True}, ("project", "atlas"))
    assert store.connections_effective("mailbox", scopes) == {}


def test_a_project_scope_identity_is_enabled_by_declaring_it(store, scopes):
    store.config_set("mailbox", "connection", "client", {"address": "a@client.tld"},
                     ("project", "atlas"))
    effective = store.connections_effective("mailbox", scopes)
    assert effective["client"]["enabled"] is True
    assert effective["client"]["grant_scope"] is None


def test_granting_reach_does_not_grant_write_the_owner_withheld(store, scopes):
    """A grant is a decision, and its fields are decided one at a time: the
    machine's `allow_write: false` survives a project that only says `enabled`.
    Write permission is never picked up on the way to asking for something else."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": False}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))

    effective = store.connections_effective("mailbox", scopes, write_default=True)
    assert effective["atlas"]["enabled"] is True
    assert effective["atlas"]["allow_write"] is False


def test_a_project_deciding_writability_for_itself_still_wins(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": False}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True, "allow_write": True},
                     ("project", "atlas"))
    assert store.connections_effective("mailbox", scopes)["atlas"]["allow_write"] is True


def test_a_field_no_scope_decided_falls_back_to_the_capability_default(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))
    assert store.connections_effective(
        "mailbox", scopes, write_default=True)["atlas"]["allow_write"] is True
    assert store.connections_effective(
        "mailbox", scopes)["atlas"]["allow_write"] is False


def test_a_global_enabled_true_is_still_no_blessing_beside_a_project_grant(store, scopes):
    """Field-wise resolution must not smuggle the blessing back: `enabled` is
    decided by the highest scope that declares it, and only a project deciding
    it opens an inherited identity."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": True}, ("project", "atlas"))
    assert store.connections_effective("mailbox", scopes) == {}


def test_include_disabled_still_returns_what_is_withheld(store, scopes):
    """The manager's doctor reconciles what is declared, not what may be used,
    so nothing may be hidden from it."""
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "connection", "client", {"address": "a@client.tld"},
                     ("project", "atlas"))
    everything = store.connections_effective("mailbox", scopes, include_disabled=True)
    assert set(everything) == {"atlas", "client"}
    assert everything["atlas"]["enabled"] is False
    assert everything["atlas"]["value"] == ATLAS_BOX
    assert everything["client"]["enabled"] is True


# --- the project registry ----------------------------------------------------

ATLAS_ID = "018f2c1a-7b3e-4d92-9a11-0c5e8f2d6a44"
OTHER_ID = "018f2c1a-0000-4d92-9a11-000000000000"


def test_a_project_is_addressed_by_slug_not_by_directory(store):
    store.project_register(ATLAS_ID, "atlas", name="Marvin")
    assert store.project_get("atlas")["name"] == "Marvin"
    assert store.project_get("atlas")["id"] == ATLAS_ID
    assert store.project_get("nope") is None


def test_the_same_project_sits_at_a_different_path_on_each_machine(store):
    store.project_register(ATLAS_ID, "atlas")
    store.project_bind_path("atlas", "laptop-1", "/home/dev/atlas")
    store.project_bind_path("atlas", "prod-1", "/opt/atlas")

    assert store.project_path("atlas", "laptop-1") == "/home/dev/atlas"
    assert store.project_path("atlas", "prod-1") == "/opt/atlas"
    assert store.project_path("atlas", "unknown-box") is None


def test_binding_a_path_to_an_unregistered_project_is_refused(store):
    with pytest.raises(StoreError) as exc:
        store.project_bind_path("ghost", "laptop-1", "/tmp/ghost")
    assert exc.value.slug == "unknown_project"


def test_a_second_machine_joins_the_project_the_id_names(store):
    """The id travels in the repository, so a laptop and a server agree that
    they are the same project without anyone telling them."""
    store.project_register(ATLAS_ID, "atlas", name="Marvin")
    store.project_register(ATLAS_ID, "atlas", name="Marvin AI")
    assert [p["slug"] for p in store.project_list()] == ["atlas"]
    assert store.project_get("atlas")["name"] == "Marvin AI"


def test_another_project_cannot_take_a_label_that_is_held(store):
    """The collision that matters: an unrelated repository also calling itself
    the caller is told the label is taken, rather than silently sharing the rows."""
    store.project_register(ATLAS_ID, "atlas")
    with pytest.raises(StoreError) as exc:
        store.project_register(OTHER_ID, "atlas")
    assert exc.value.slug == "slug_taken"
    assert store.project_get("atlas")["id"] == ATLAS_ID


def test_relabelling_a_project_is_a_migration_not_an_edit(store):
    store.project_register(ATLAS_ID, "atlas")
    with pytest.raises(StoreError) as exc:
        store.project_register(ATLAS_ID, "atlas-two")
    assert exc.value.slug == "slug_immutable"


def test_a_project_without_an_id_is_refused(store):
    with pytest.raises(StoreError) as exc:
        store.project_register("", "atlas")
    assert exc.value.slug == "bad_project_id"


def test_the_migration_moves_the_label_and_leaves_the_rows_where_they_are(store):
    """The point of the whole arrangement: the label is a way in, the id is what
    holds the rows, so the rows are found under the new label without one of
    them being touched."""
    store.state_set("telegram", "cursor", 42, ("project", "atlas"))
    store.config_set("mailbox", "identifier", "inbox", "INBOX", ("project", "atlas"))

    assert store.project_relabel(ATLAS_ID, "atlas-two") == "atlas"

    assert store.project_get("atlas") is None
    assert store.project_get("atlas-two")["id"] == ATLAS_ID
    assert store.state_get("telegram", "cursor", ("project", "atlas-two")) == 42
    assert store.config_get("mailbox", "identifier", "inbox",
                            Scopes(project="atlas-two")) == "INBOX"


def test_a_relabel_keeps_the_name_and_the_creation_date(store):
    """A rename is a rename. Anything it changes beyond the label is something
    nobody asked it to change."""
    store.project_register(ATLAS_ID, "atlas", name="A Consuming Project")
    before = store.project_get("atlas")
    store.project_relabel(ATLAS_ID, "atlas-two")
    after = store.project_get("atlas-two")
    assert (after["name"], after["created_at"]) == (before["name"], before["created_at"])


def test_a_relabel_cannot_take_a_label_another_project_holds(store):
    """Refused for the reason registration refuses it: the label is how rows are
    found, so taking it would take that project's rows."""
    store.project_register(OTHER_ID, "client")
    with pytest.raises(StoreError) as exc:
        store.project_relabel(ATLAS_ID, "client")
    assert exc.value.slug == "slug_taken"
    assert store.project_get("client")["id"] == OTHER_ID
    assert store.project_get("atlas")["id"] == ATLAS_ID


def test_relabelling_an_unregistered_project_does_not_register_it(store):
    """A relabel moves a label that is held; there is nothing to move here, and
    minting the project instead would claim the label for rows that do not
    exist."""
    with pytest.raises(StoreError) as exc:
        store.project_relabel("018f2c1a-9999-4d92-9a11-999999999999", "ghost")
    assert exc.value.slug == "unknown_project"
    assert store.project_get("ghost") is None
    assert [p["slug"] for p in store.project_list()] == ["atlas"]


def test_a_relabel_validates_the_new_label_as_registration_does(store):
    with pytest.raises(StoreError) as exc:
        store.project_relabel(ATLAS_ID, "Atlas Two")
    assert exc.value.slug == "bad_name"
    assert store.project_get("atlas")["id"] == ATLAS_ID


def test_relabelling_to_the_label_already_held_changes_nothing(store):
    """The retry after a half-finished rename has to be able to succeed, so the
    second run of the same request is not an error."""
    assert store.project_relabel(ATLAS_ID, "atlas") == "atlas"
    assert store.project_get("atlas")["id"] == ATLAS_ID


# --- EXACT: rule 16, state does not cascade ----------------------------------

def test_state_does_not_fall_back_to_another_scope(store):
    store.state_set("telegram", "cursor", 42, ("project", "atlas"))
    assert store.state_get("telegram", "cursor", ("project", "atlas")) == 42
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
    store.state_set("telegram", "lease", "held", ("project", "atlas"), ttl_seconds=-5)
    assert store.state_get("telegram", "lease", ("project", "atlas")) is None
    assert store.state_sweep() == 1
    assert store.state_sweep() == 0


def test_live_state_survives_a_sweep(store):
    store.state_set("telegram", "lease", "held", ("project", "atlas"), ttl_seconds=600)
    assert store.state_sweep() == 0
    assert store.state_get("telegram", "lease", ("project", "atlas")) == "held"


# --- revisions: what git used to answer --------------------------------------

def test_every_write_records_who_and_what_it_was(store):
    store.config_set("telegram", "setting", "model", "sol", ("global", ""), actor="kz")
    store.config_set("telegram", "setting", "model", "terra", ("global", ""), actor="atlas")

    log = store.revisions("telegram", "setting", "model")
    assert [r["actor"] for r in log] == ["atlas", "kz"]
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
    assert store.schema_version() == SCHEMA_VERSION
    assert store.migrate() == SCHEMA_VERSION


def test_a_capability_owns_its_own_namespace(store):
    store.migrate("automations", 1, ["CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY)"])
    assert store.schema_version("automations") == 1
    assert store.schema_version("core") == SCHEMA_VERSION


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


def test_an_email_address_is_a_valid_connection_id(store, scopes):
    """Real mailboxes are addressed by address; the key has to carry one."""
    store.config_set("mailbox", "connection", "owner@example.com",
                     {"address": "owner@example.com"}, ("global", ""))
    store.config_set("mailbox", "grant", "owner@example.com",
                     {"enabled": True, "allow_write": True}, ("project", "atlas"))
    effective = store.connections_effective("mailbox", scopes)
    assert effective["owner@example.com"]["allow_write"] is True


def test_a_key_with_whitespace_is_still_refused(store):
    with pytest.raises(StoreError) as exc:
        store.config_set("mailbox", "connection", "two words", {}, ("global", ""))
    assert exc.value.slug == "bad_name"


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
    assert health["namespaces"] == {"core": SCHEMA_VERSION}
    assert health["roundtrip_ms"] >= 0


def test_open_store_routes_by_scheme(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPABILITIES_STORE_URL", raising=False)
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


# --- context: saving is not deploying ----------------------------------------

def test_a_version_is_addressed_by_its_content(store, scopes):
    a = store.context_put("telegram", "voice-agent", "you are terse", ("global", ""))
    b = store.context_put("telegram", "voice-agent", "you are terse", ("global", ""))
    c = store.context_put("telegram", "voice-agent", "you are verbose", ("global", ""))
    assert a == b and a != c
    assert len(store.context_versions("telegram", "voice-agent", scopes)) == 2


def test_a_version_name_is_short_enough_to_read(store):
    digest = store.context_put("telegram", "voice-agent", "text", ("global", ""))
    assert len(digest) == SCHEMA_HASH_LENGTH
    assert digest == hashlib.sha256(b"text").hexdigest()[:SCHEMA_HASH_LENGTH]


def test_saving_a_version_changes_nothing_until_it_is_activated(store, scopes):
    """The whole reason this is safe to hold executable text in."""
    first = store.context_put("telegram", "voice-agent", "terse", ("global", ""))
    store.context_activate("telegram", "voice-agent", first, ("global", ""))
    assert store.context_read("telegram", "voice-agent", scopes)["body"] == "terse"

    store.context_put("telegram", "voice-agent", "verbose", ("global", ""))
    assert store.context_read("telegram", "voice-agent", scopes)["body"] == "terse"


def test_activating_puts_the_new_version_in_force(store, scopes):
    first = store.context_put("telegram", "voice-agent", "terse", ("global", ""))
    second = store.context_put("telegram", "voice-agent", "verbose", ("global", ""))
    store.context_activate("telegram", "voice-agent", first, ("global", ""))
    store.context_activate("telegram", "voice-agent", second, ("global", ""))
    assert store.context_read("telegram", "voice-agent", scopes)["body"] == "verbose"


def test_exactly_one_version_is_active_and_the_database_says_so(store, scopes):
    first = store.context_put("telegram", "voice-agent", "one", ("global", ""))
    second = store.context_put("telegram", "voice-agent", "two", ("global", ""))
    store.context_activate("telegram", "voice-agent", first, ("global", ""))
    store.context_activate("telegram", "voice-agent", second, ("global", ""))
    active = [v for v in store.context_versions("telegram", "voice-agent", scopes) if v["active"]]
    assert [v["hash"] for v in active] == [second]


def test_rolling_back_is_activating_the_older_version(store, scopes):
    first = store.context_put("telegram", "voice-agent", "v1", ("global", ""))
    second = store.context_put("telegram", "voice-agent", "v2", ("global", ""))
    store.context_activate("telegram", "voice-agent", second, ("global", ""))
    store.context_activate("telegram", "voice-agent", first, ("global", ""), actor="kz")
    assert store.context_read("telegram", "voice-agent", scopes)["body"] == "v1"
    assert store.revisions("telegram", "context", "voice-agent")[0]["actor"] == "kz"


def test_a_project_inherits_the_globally_active_version(store, scopes):
    shared = store.context_put("telegram", "voice-agent", "the shared one", ("global", ""))
    store.context_activate("telegram", "voice-agent", shared, ("global", ""))
    got = store.context_read("telegram", "voice-agent", scopes)
    assert got["body"] == "the shared one"
    assert got["scope"] == ("global", None)


def test_a_projects_own_version_wins_over_the_global_one(store, scopes):
    shared = store.context_put("telegram", "voice-agent", "shared", ("global", ""))
    store.context_activate("telegram", "voice-agent", shared, ("global", ""))
    own = store.context_put("telegram", "voice-agent", "ours", ("project", "atlas"))
    store.context_activate("telegram", "voice-agent", own, ("project", "atlas"))

    assert store.context_read("telegram", "voice-agent", scopes)["body"] == "ours"
    assert store.context_read("telegram", "voice-agent", Scopes())["body"] == "shared"


def test_one_projects_drafts_stay_out_of_anothers_history(store, scopes):
    """Without a scope on the row, every project's versions of the same key
    would pool under one name and each would read the others' history."""
    store.project_register(OTHER_ID, "client")
    store.context_put("telegram", "voice-agent", "the agent's", ("project", "atlas"))
    store.context_put("telegram", "voice-agent", "client's", ("project", "client"))

    assert len(store.context_versions("telegram", "voice-agent", scopes)) == 1
    assert len(store.context_versions("telegram", "voice-agent", Scopes(project="client"))) == 1


def test_a_version_from_another_scope_cannot_be_activated(store):
    """Activation names a version in its own scope: a project running someone
    else's text would be a copy nobody made deliberately."""
    theirs = store.context_put("telegram", "voice-agent", "theirs", ("global", ""))
    with pytest.raises(StoreError) as exc:
        store.context_activate("telegram", "voice-agent", theirs, ("project", "atlas"))
    assert exc.value.slug == "unknown_version"


def test_an_unactivated_item_reads_as_absent(store, scopes):
    store.context_put("telegram", "voice-agent", "drafted, never deployed", ("global", ""))
    assert store.context_read("telegram", "voice-agent", scopes) is None


def test_a_body_survives_exactly(store, scopes):
    body = "#!/usr/bin/env python3\nprint('héllo')\n\n\ttabbed\n"
    digest = store.context_put("automations", "upstream-watch", body, ("project", "atlas"),
                               media_type="text/x-python", activate=True)
    got = store.context_read("automations", "upstream-watch", scopes)
    assert got["body"] == body and got["hash"] == digest
    assert got["media_type"] == "text/x-python"
    assert got["scope"] == ("project", ATLAS_ID)


def test_history_omits_the_bodies_it_lists(store, scopes):
    store.context_put("telegram", "voice-agent", "x" * 5000, ("global", ""))
    entry = store.context_versions("telegram", "voice-agent", scopes)[0]
    assert entry["bytes"] == 5000 and "body" not in entry


def test_a_reference_is_context_like_any_other(store, scopes):
    body = "---\nname: Project Telegram Session\ndescription: how to wire it\n---\n\nbody\n"
    store.context_put("telegram", "reference.project-session", body, ("project", "atlas"),
                      media_type="text/markdown", activate=True)
    assert store.context_read("telegram", "reference.project-session", scopes)["body"] == body
    assert store.context_keys("telegram", scopes) == ["reference.project-session"]


def test_a_truncated_hash_naming_different_text_is_refused(store):
    """What makes twelve characters safe is that a clash is loud, not unlikely."""
    digest = store.context_put("telegram", "voice-agent", "original", ("global", ""))
    with store.transaction():
        store._execute(
            "UPDATE context SET body = ? WHERE capability = ? AND key = ? AND hash = ?",
            ("tampered", "telegram", "voice-agent", digest))
    with pytest.raises(StoreError) as exc:
        store.context_put("telegram", "voice-agent", "original", ("global", ""))
    assert exc.value.slug == "hash_collision"


def test_a_store_exists_without_anyone_configuring_one(tmp_path, monkeypatch):
    """Tracking is not optional: the first caller to ask creates the store."""
    monkeypatch.delenv("CAPABILITIES_STORE_URL", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = tmp_path / "state" / "capabilities" / "store.db"
    assert not expected.exists()
    with open_store() as s:
        s.migrate()
    assert expected.is_file()

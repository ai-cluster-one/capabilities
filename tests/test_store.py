"""The store tier's records adapter: how connections and grants resolve across
the two scopes, held to the files a project and the user's config home keep.

Every question is put through `FileRecords`, written at the scope it names and
read back as the capability reads it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "contract"))

from store import FileRecords, StoreError  # noqa: E402

ATLAS_ID = "3f1c9b4e-0000-4d92-9a11-0c5e8f2d6a44"


class _Scoped:
    """The records adapter, addressed by the scope each write names, so a test
    reads as the decision it records."""

    def __init__(self, records: FileRecords):
        self.records = records

    def config_set(self, capability, collection, key, value, scope):
        self.records.set(capability, collection, key, value, scope=scope[0])

    def connections_effective(self, capability, _scopes=None, **kwargs):
        return self.records.connections(capability, **kwargs)


@pytest.fixture()
def store(tmp_path):
    envelope = tmp_path / "project" / "capabilities"
    envelope.mkdir(parents=True)
    return _Scoped(FileRecords(envelope, tmp_path / "config", ATLAS_ID, "atlas"))


@pytest.fixture()
def scopes():
    return None


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
    store.config_set("mailbox", "connection", "personal", {"address": "personal@example.com"}, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"enabled": True}, ("project", "atlas"))
    store.config_set("mailbox", "grant", "personal", {"enabled": False}, ("project", "atlas"))

    assert set(store.connections_effective("mailbox", scopes)) == {"atlas"}
    both = store.connections_effective("mailbox", scopes, include_disabled=True)
    assert both["personal"]["enabled"] is False


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


def test_unknown_collection_is_refused(store):
    with pytest.raises(StoreError) as exc:
        store.records.get("telegram", "nonsense", "k")
    assert exc.value.slug == "bad_collection"


def test_a_grant_only_entry_is_a_connection_in_files(store, scopes):
    """One file keeps an identity and its grant together, so an entry carrying
    only a decision is a fieldless connection rather than a grant aimed at
    nothing."""
    store.config_set("mailbox", "grant", "marvni", {"allow_write": True}, ("project", "atlas"))
    effective = store.connections_effective("mailbox", scopes)
    assert effective["marvni"]["value"] == {}
    assert effective["marvni"]["allow_write"] is True


def test_the_read_only_switch_closes_every_connection(store, scopes, monkeypatch):
    store.config_set("mailbox", "connection", "client", {"address": "a@client.tld"},
                     ("project", "atlas"))
    store.config_set("mailbox", "grant", "client", {"allow_write": True}, ("project", "atlas"))
    monkeypatch.setenv("CAPABILITIES_READ_ONLY", "1")
    assert store.connections_effective("mailbox", scopes)["client"]["allow_write"] is False


def test_a_machine_read_lends_the_global_connection_read_only(store, scopes):
    store.config_set("mailbox", "connection", "atlas", ATLAS_BOX, ("global", ""))
    store.config_set("mailbox", "grant", "atlas", {"allow_write": True}, ("global", ""))
    assert store.connections_effective("mailbox", scopes) == {}
    lent = store.connections_effective("mailbox", scopes, machine_read=True)["atlas"]
    assert lent["machine_read"] is True and lent["allow_write"] is False
    store.config_set("mailbox", "grant", "atlas", {"enabled": False}, ("global", ""))
    assert store.connections_effective("mailbox", scopes, machine_read=True) == {}

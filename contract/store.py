"""Canonical store tier — the one true copy of the records layer.

This is the required records tier alongside `capability core` and the optional
`connections` tier in `contract/preamble.py`, and it obeys the same law: the
helpers between the fence markers are COPIED into each `bin/<name>`, never
imported, so a manager update can never break a deployed capability (SHEBANG.md
"spec, never a shared runtime library"). Every capability carries it because the
core identifiers and policy surface resolve through the records adapter. A
bundled service carries the same body whole as `service/store.py`.

WHAT THIS TIER OWNS
===================
The records a *running* capability reads and writes: connections, grants,
identifiers, settings, the policy gate and documents. They are kept in files -
the project's envelope, then the user's config home - except in a project in
store mode, which keeps its connections, grants, identifiers and policy in the
contract's tables in its store (`StoreRecords`); a project that moved only its
connections and grants there keeps the rest in files. Which database that is,
like the database any runtime state lives in, is the shared database library's
answer alone (`capabilities_contract.db`); this tier only asks it.

Every connection this tier opens is a transaction connection (the library's
`connect(..., transaction=True)`), dialled at the store's transaction address
where its setting names one: the process's one working connection, which the
library lends for each read or write and keeps between them, so a command
connects once however many records it reads. What was read is served from the
adapter's cache for STORE_READ_SECONDS.

THREE RESOLUTION SEMANTICS, ALREADY IN THE DOCTRINE
===================================================
The precedence is always project over global, but *how* the scopes combine is
declared per collection:

  - MERGE  — per key; a key absent at the higher scope inherits the lower.
    This is rule 17 for the gate ("an absent project entry inherits the global
    entry"). Connections take it too, at the entry level: rule 18 keeps an
    identity atomic — taken whole from one scope, never assembled out of two —
    while letting a project add to the set it inherits rather than replace it.

  - FIRST  — the highest scope holding ANY entry wins, whole.

  - EXACT  — no cascade; a record belongs to exactly one scope and is read
    there (rule 16).

ONE WRITER PER COLLECTION
=========================
Rule 15 is addressed by `(capability, collection)` instead of by path: the
manager alone writes `policy`, a capability alone writes its own `identifier`
records, and `connection`, `grant`, `setting` and `document` records are
human-written through a CLI verb.

EXCEPTIONS ARE RAISED, NEVER PRINTED
====================================
This tier is a library, not a command. It raises `StoreError`; the carrying
capability maps that onto its own `_die` envelope and exit codes, so the error
surface stays the capability's own.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import socket
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>
# A region carries what it needs. Relying on the host file to have imported
# the right names is a coupling nothing checks and nothing reports: it works in
# whichever capability happened to import them and fails at import time in the
# rest. Re-importing a name the host already has costs nothing.
import json
import os
import hashlib
import re
import socket
# The capability core region reads the clock through `time` and imports nothing
# of its own, so this region keeps carrying it.
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


# Two scopes and no more: a record either belongs to one project (the
# project's envelope) or to every project (the user's config home).
SCOPE_KINDS = ("global", "project")

FIRST = "first"
MERGE = "merge"
EXACT = "exact"

# The collection registry: how each resolves, and who is allowed to write it.
# Adding a collection here is the only place a new record class is declared.
#
# `connection` and `grant` are the same fact split along the seam that matters:
# an entry's identity — where the thing is and how to reach it — is a fact about
# the world, the same in every project forever. What a project may DO with that
# identity is a decision, different per project by definition. Glued together,
# the second cannot be overridden without restating the first; apart, a project
# that needs write access writes one grant and repeats no address, no host,
# no secret.
#
# An identity is atomic: it merges by entry, never by field, so no connection is
# ever assembled from two scopes. That is what rule 18's "never merged" was
# protecting, and it survives; what it gives up is refusing to let a project add
# to the global set at all.
COLLECTIONS: dict[str, dict[str, str]] = {
    "connection": {"resolve": MERGE, "writer": "human"},
    "grant": {"resolve": MERGE, "writer": "human"},
    "identifier": {"resolve": MERGE, "writer": "capability"},
    "setting": {"resolve": MERGE, "writer": "human"},
    "policy": {"resolve": MERGE, "writer": "manager"},
    "document": {"resolve": MERGE, "writer": "human"},
}

# Grant fields and what they mean when nothing declares them. `allow_write`
# falls back to the capability's own WRITE_DEFAULT rather than to a value here,
# because "does a write leave the system" is the capability's fact.
GRANT_FIELDS = ("enabled", "allow_write")

# A document version is named by the first twelve hex characters of its sha256.
# Forty-eight bits is far more than a project's worth of prose needs, and a name
# a person can read at a glance is worth more than headroom nobody will reach.
HASH_LENGTH = 12

# A key is a label a human chose, and for a mailbox that label is naturally an
# email address — so `@` and `+` belong here. What stays out is whitespace,
# separators and anything that would make a key ambiguous to address.
KEY_RE = re.compile(r"[a-z0-9][a-z0-9._@+-]{0,127}", re.IGNORECASE)
CAPABILITY_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")


class StoreError(Exception):
    """Every failure this tier reports. Carries a slug the caller maps to an
    exit code, so the CLI's error envelope stays the capability's own."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug = slug
        self.message = message
        self.hint = hint


# The process-wide read-only switch. A caller hands a whole process tree to
# something that may read through the capabilities but change nothing through
# them by setting this one variable, which every descendant inherits. It is
# decided here and nowhere else, because this tier is the one every capability
# carries and the one the manager and the bundled services import. It only ever
# closes: nothing it says makes a connection writable, and unset, `0` or
# `false` leave every record and every gate exactly as declared.
READ_ONLY_ENV = "CAPABILITIES_READ_ONLY"
READ_ONLY_EFFECT = ("every connection resolves read-only whatever its grant or "
                    "WRITE_DEFAULT; write verbs, project-record writes and "
                    "service activation exit 4")


def read_only_switch() -> bool:
    """True when this process runs under the read-only switch: the variable
    holds `1` or `true`, in any case."""
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in ("1", "true")


# The name a machine declares for itself as a node. A container's host name is
# its container id and changes on every redeploy, so a node that declares one
# keeps the same name in the store across them.
NODE_NAME_ENV = "AGENT_NODE_NAME"


def node_name() -> str:
    """The node this process runs on, by the one name the package gives it: a
    store-mode grant naming a node is matched against it, and a service that
    records which node holds its work records it. It is the name the machine
    declares in AGENT_NODE_NAME, and the host name where it declares none."""
    return os.environ.get(NODE_NAME_ENV, "").strip() or socket.gethostname()


def _node_check(node: Any) -> str | None:
    """A node a policy row is written for: a name, stripped, or None for every
    node."""
    if node is None:
        return None
    if not isinstance(node, str) or not node.strip():
        raise StoreError("bad_node", f"node {node!r} must name a node, or be null "
                         "for every node")
    return node.strip()


# Where a project keeps its records: files (the default) or the store, which
# then holds its connections, grants, identifiers and policy together. The
# manager alone writes this key into `project.json`, when it imports a
# project's files into the store; nothing infers it from a configured store.
RECORDS_MODE_KEY = "records"
RECORDS_MODES = ("files", "store")

# The key a project that moved only its connections and grants to the store
# carries. It keeps meaning exactly that: such a project reads its identifiers
# and its gate from files until it is imported again.
CONNECTIONS_MODE_KEY = "connections"
CONNECTIONS_MODES = ("files", "store")

# The contract's own tables in the store, migrated under the contract's own
# ledger owner, and the schema version this code writes them at.
STORE_OWNER = "capabilities"
STORE_CONNECTIONS_TABLE = "capabilities_connections"
STORE_GRANTS_TABLE = "capabilities_connection_grants"
STORE_IDENTIFIERS_TABLE = "capabilities_identifiers"
STORE_POLICY_TABLE = "capabilities_policy"
# The kernel's own tables, migrated in the same ledger: the agent registry, the
# agent's nodes and the one row of facts about the store as a whole. The
# manager alone writes them.
STORE_AGENTS_TABLE = "capabilities_agents"
STORE_AGENT_NODES_TABLE = "capabilities_agent_nodes"
STORE_GLOBAL_TABLE = "capabilities_global"
# Settings of an agent whose records moved into the store: one row per leaf,
# the documents that exist, and the append-only trail of every change.
STORE_SETTINGS_TABLE = "capabilities_settings"
STORE_SETTINGS_DOCUMENTS_TABLE = "capabilities_settings_documents"
STORE_SETTINGS_CHANGES_TABLE = "capabilities_settings_changes"
STORE_TABLES_VERSION = (1, 3)

# Which collections each store mode keeps in the store. The default pointer is
# a setting kept with the grants in both.
STORE_COLLECTIONS = {
    CONNECTIONS_MODE_KEY: frozenset({"connection", "grant"}),
    RECORDS_MODE_KEY: frozenset({"connection", "grant", "identifier", "policy"}),
}
STORE_TABLES = {
    "connection": STORE_CONNECTIONS_TABLE,
    "grant": STORE_GRANTS_TABLE,
    "identifier": STORE_IDENTIFIERS_TABLE,
    "policy": STORE_POLICY_TABLE,
    "setting": STORE_SETTINGS_TABLE,
}

# A store that cannot be reached or is busy is the environment's fault, not
# the configuration's: a capability maps these to its network exit code.
STORE_ENVIRONMENT_SLUGS = frozenset({"store_unreachable", "store_busy"})

# How long one process reuses what it read from the store, and a refusal to
# connect: long enough that one command asks once, short enough that a
# long-running service sees a changed grant, or a store that came back, at once.
STORE_READ_SECONDS = 2.0

# Whether an adapter keeps its connection between operations. Only a migration
# plan sets it: the plan's one link holds a transaction open for the whole run,
# and closing it after an operation would undo what the plan did so far.
_HOLD_RECORDS_CONNECTION = False


def record_table_steps() -> list[tuple[str, str]]:
    """The migration steps of the contract's tables. `enabled` and
    `allow_write` left null decide nothing, as a field absent from a file's
    grant does; `node` null is the grant, or the policy row, for every node;
    `agent_id` null is an identifier or a policy row for every project."""
    return [
        ("0001-connections",
         "CREATE TABLE capabilities_connections ("
         " capability text NOT NULL,"
         " name text NOT NULL,"
         " spec jsonb NOT NULL,"
         " PRIMARY KEY (capability, name),"
         " CHECK (jsonb_typeof(spec) = 'object'))"),
        ("0002-connection-grants",
         "CREATE TABLE capabilities_connection_grants ("
         " agent_id text NOT NULL,"
         " capability text NOT NULL,"
         " connection text NOT NULL,"
         " node text CHECK (node <> ''),"
         " enabled boolean,"
         " allow_write boolean,"
         " is_default boolean NOT NULL DEFAULT false,"
         " FOREIGN KEY (capability, connection)"
         "  REFERENCES capabilities_connections (capability, name)"
         "  ON DELETE CASCADE ON UPDATE CASCADE)"),
        ("0003-connection-grants-key",
         "CREATE UNIQUE INDEX capabilities_connection_grants_key"
         " ON capabilities_connection_grants"
         " (agent_id, capability, connection, (coalesce(node, '')))"),
        ("0004-connection-grants-default",
         "CREATE UNIQUE INDEX capabilities_connection_grants_default"
         " ON capabilities_connection_grants"
         " (agent_id, capability, (coalesce(node, ''))) WHERE is_default"),
        ("0005-identifiers",
         "CREATE TABLE capabilities_identifiers ("
         " agent_id text CHECK (agent_id <> ''),"
         " capability text NOT NULL,"
         " label text NOT NULL,"
         " value jsonb NOT NULL,"
         " note text NOT NULL DEFAULT '',"
         " updated_at timestamptz NOT NULL DEFAULT now())"),
        ("0006-identifiers-key",
         "CREATE UNIQUE INDEX capabilities_identifiers_key"
         " ON capabilities_identifiers"
         " ((coalesce(agent_id, '')), capability, label)"),
        ("0007-policy",
         "CREATE TABLE capabilities_policy ("
         " agent_id text CHECK (agent_id <> ''),"
         " capability text NOT NULL,"
         " node text CHECK (node <> ''),"
         " enabled boolean NOT NULL,"
         " updated_at timestamptz NOT NULL DEFAULT now())"),
        ("0008-policy-key",
         "CREATE UNIQUE INDEX capabilities_policy_key"
         " ON capabilities_policy"
         " ((coalesce(agent_id, '')), capability, (coalesce(node, '')))"),
        # The agent registry. `id` is the project id `project.json` declares;
        # the registry never mints or changes one. An icon and its SHA-256 are
        # present together or absent together.
        ("0009-agents",
         "CREATE TABLE capabilities_agents ("
         " id text PRIMARY KEY CHECK (id <> ''),"
         " name text CHECK (name <> ''),"
         " description text CHECK (description <> ''),"
         " icon bytea,"
         " icon_sha text CHECK (icon_sha ~ '^[0-9a-f]{64}$'),"
         " onboarding_status text CHECK (onboarding_status IN ('pending', 'complete')),"
         " onboarding_routine text CHECK (onboarding_routine <> ''),"
         " migrating_since timestamptz,"
         " migrated_at timestamptz,"
         " created_at timestamptz NOT NULL DEFAULT now(),"
         " updated_at timestamptz NOT NULL DEFAULT now(),"
         " CHECK ((icon IS NULL) = (icon_sha IS NULL)));"
         " COMMENT ON TABLE capabilities_agents IS"
         " 'The agent registry: one row per agent, keyed by the project id its"
         " capabilities/project.json declares. Written by the capabilities manager alone.';"
         " COMMENT ON COLUMN capabilities_agents.id IS"
         " 'The project id from capabilities/project.json (prj_<12 hex>, or an older UUID).';"
         " COMMENT ON COLUMN capabilities_agents.name IS"
         " 'Display name; null while the agent has none of its own.';"
         " COMMENT ON COLUMN capabilities_agents.icon IS"
         " 'The agent''s icon as PNG bytes; null with icon_sha when it has none.';"
         " COMMENT ON COLUMN capabilities_agents.icon_sha IS"
         " 'Lowercase hex SHA-256 of icon.';"
         " COMMENT ON COLUMN capabilities_agents.onboarding_status IS"
         " 'pending while the agent still runs its onboarding routine, complete once"
         " it finished; null when no onboarding is tracked.';"
         " COMMENT ON COLUMN capabilities_agents.onboarding_routine IS"
         " 'The routine the agent runs while onboarding is pending.';"
         " COMMENT ON COLUMN capabilities_agents.migrating_since IS"
         " 'Set while a move of this agent''s records from files into the store is open.';"
         " COMMENT ON COLUMN capabilities_agents.migrated_at IS"
         " 'When this agent''s records and settings moved into the store; null while"
         " they are still read from files.'"),
        # ContextKit's registry rows, taken over once where that table exists in
        # the same schema. An empty name or description meant none there.
        ("0010-agents-from-contextkit",
         "DO $copy$ BEGIN"
         " IF to_regclass('contextkit_agents') IS NOT NULL THEN"
         "  INSERT INTO capabilities_agents"
         "   (id, name, description, icon, icon_sha, created_at, updated_at)"
         "  SELECT id, nullif(name, ''), nullif(description, ''),"
         "   CASE WHEN icon IS NOT NULL AND icon_sha ~ '^[0-9a-f]{64}$' THEN icon END,"
         "   CASE WHEN icon IS NOT NULL AND icon_sha ~ '^[0-9a-f]{64}$' THEN icon_sha END,"
         "   created_at, updated_at"
         "  FROM contextkit_agents WHERE id <> ''"
         "  ON CONFLICT (id) DO NOTHING;"
         " END IF;"
         " END $copy$"),
        # The machines an agent runs on. Exactly one may be its master, which
        # the partial index below enforces; a node is removed with its agent.
        ("0011-agent-nodes",
         "CREATE TABLE capabilities_agent_nodes ("
         " agent_id text NOT NULL REFERENCES capabilities_agents (id)"
         "  ON DELETE CASCADE ON UPDATE CASCADE,"
         " node text NOT NULL CHECK (node <> ''),"
         " kind text NOT NULL CHECK (kind IN ('mac', 'server')),"
         " address text CHECK (address <> ''),"
         " is_master boolean NOT NULL DEFAULT false,"
         " may_write boolean NOT NULL DEFAULT false,"
         " target text CHECK (target <> ''),"
         " last_seen_at timestamptz,"
         " PRIMARY KEY (agent_id, node));"
         " COMMENT ON TABLE capabilities_agent_nodes IS"
         " 'The nodes an agent runs on, one row per (agent, node). Written by the"
         " capabilities manager alone. agent_id references capabilities_agents"
         " because a node exists only as a node of a registered agent.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.node IS"
         " 'The name the node gives itself in AGENT_NODE_NAME.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.address IS"
         " 'Host name or IP address the node is reached at, where it is fixed.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.is_master IS"
         " 'The node that writes the rows every node of the agent inherits; at"
         " most one per agent.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.may_write IS"
         " 'Whether a node that is not the master may write rows of its own, marked"
         " with its node.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.target IS"
         " 'The deployment target this node is deployed by; null for a node that"
         " is not deployed, such as a Mac.';"
         " COMMENT ON COLUMN capabilities_agent_nodes.last_seen_at IS"
         " 'When the node last reported itself.'"),
        ("0012-agent-nodes-master",
         "CREATE UNIQUE INDEX capabilities_agent_nodes_master"
         " ON capabilities_agent_nodes (agent_id) WHERE is_master"),
        # Facts about the store as a whole, in its one row.
        ("0013-global",
         "CREATE TABLE capabilities_global ("
         " singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),"
         " global_migrated_at timestamptz);"
         " INSERT INTO capabilities_global DEFAULT VALUES;"
         " COMMENT ON TABLE capabilities_global IS"
         " 'One row of facts about this store as a whole.';"
         " COMMENT ON COLUMN capabilities_global.singleton IS"
         " 'Always true; keeps the table at one row.';"
         " COMMENT ON COLUMN capabilities_global.global_migrated_at IS"
         " 'When the global records were written into the store; null until then.'"),
        # A grant for every agent: `agent_id` null, beneath each agent's own
        # grant, and a default pointer for every agent the same way. The keys
        # of 0003 and 0004 stay as they are, so code that names them in its
        # ON CONFLICT keeps writing an agent's grants; the keys below coalesce
        # the agent as well, so two global rows can never stand for one grant.
        ("0014-connection-grants-global",
         "ALTER TABLE capabilities_connection_grants ALTER COLUMN agent_id DROP NOT NULL;"
         " ALTER TABLE capabilities_connection_grants"
         "  ADD CONSTRAINT capabilities_connection_grants_agent_id_check"
         "  CHECK (agent_id <> '');"
         " CREATE UNIQUE INDEX capabilities_connection_grants_scope_key"
         "  ON capabilities_connection_grants"
         "  ((coalesce(agent_id, '')), capability, connection, (coalesce(node, '')));"
         " CREATE UNIQUE INDEX capabilities_connection_grants_scope_default"
         "  ON capabilities_connection_grants"
         "  ((coalesce(agent_id, '')), capability, (coalesce(node, ''))) WHERE is_default;"
         " COMMENT ON TABLE capabilities_connection_grants IS"
         " 'What an agent may do with a connection: one row per (agent, capability,"
         " connection, node). A row whose agent_id is null is the grant for every"
         " agent, beneath each agent''s own; it never enables a connection by itself.';"
         " COMMENT ON COLUMN capabilities_connection_grants.agent_id IS"
         " 'The agent the grant is for; null for the grant every agent inherits.';"
         " COMMENT ON COLUMN capabilities_connection_grants.node IS"
         " 'The node the grant is for; null for every node. A node row is read before"
         " the row for every node.';"
         " COMMENT ON COLUMN capabilities_connection_grants.is_default IS"
         " 'Whether this connection is the default pointer at this grant''s scope and"
         " node; at most one per scope and node.'"),
        # An identifier for one node, read before the row for every node.
        ("0015-identifiers-node",
         "ALTER TABLE capabilities_identifiers ADD COLUMN node text CHECK (node <> '');"
         " DROP INDEX capabilities_identifiers_key;"
         " CREATE UNIQUE INDEX capabilities_identifiers_key ON capabilities_identifiers"
         "  ((coalesce(agent_id, '')), capability, label, (coalesce(node, '')));"
         " COMMENT ON TABLE capabilities_identifiers IS"
         " 'Durable non-secret lookups a capability discovered, one row per (agent,"
         " capability, label, node); agent_id null for every agent.';"
         " COMMENT ON COLUMN capabilities_identifiers.node IS"
         " 'The node the identifier is for; null for every node. A node row is read"
         " before the row for every node.'"),
        # Settings: one row per leaf of a capability's settings document, for
        # one agent (or every agent, agent_id null) and one node (or every
        # node). A writer takes, for the whole transaction, the advisory lock
        #   pg_advisory_xact_lock(hashtext(concat_ws(chr(31), schema,
        #     coalesce(agent_id, ''), capability, coalesce(node, ''))))
        # of the document it writes, so two writers of one document queue.
        # agent_id keeps the name the contract's other tables give it.
        ("0016-settings",
         "CREATE TABLE capabilities_settings ("
         " agent_id text CHECK (agent_id <> ''),"
         " capability text NOT NULL CHECK (capability <> ''),"
         " node text CHECK (node <> ''),"
         " path text[] NOT NULL"
         "  CHECK (cardinality(path) > 0 AND array_position(path, NULL) IS NULL),"
         " value jsonb NOT NULL,"
         " updated_at timestamptz NOT NULL DEFAULT now(),"
         " updated_by text NOT NULL CHECK (updated_by <> ''));"
         " CREATE UNIQUE INDEX capabilities_settings_key ON capabilities_settings"
         "  ((coalesce(agent_id, '')), capability, (coalesce(node, '')), path);"
         " CREATE FUNCTION capabilities_settings_guard() RETURNS trigger"
         "  LANGUAGE plpgsql SET search_path FROM CURRENT AS $guard$"
         " BEGIN"
         "  IF jsonb_typeof(NEW.value) = 'object' AND NEW.value <> '{}'::jsonb THEN"
         "   RAISE EXCEPTION 'setting % holds an object with keys; a row holds a scalar,"
         " an array or {}', array_to_string(NEW.path, '.')"
         "    USING ERRCODE = 'check_violation';"
         "  END IF;"
         "  IF EXISTS (SELECT 1 FROM capabilities_settings s"
         "   WHERE coalesce(s.agent_id, '') = coalesce(NEW.agent_id, '')"
         "   AND s.capability = NEW.capability"
         "   AND coalesce(s.node, '') = coalesce(NEW.node, '')"
         "   AND s.path <> NEW.path"
         "   AND (s.path[1:cardinality(NEW.path)] = NEW.path"
         "    OR NEW.path[1:cardinality(s.path)] = s.path)) THEN"
         "   RAISE EXCEPTION 'setting % overlaps another row of its document: a path"
         " never prefixes another', array_to_string(NEW.path, '.')"
         "    USING ERRCODE = 'check_violation';"
         "  END IF;"
         "  RETURN NEW;"
         " END $guard$;"
         " CREATE TRIGGER capabilities_settings_guard"
         "  BEFORE INSERT OR UPDATE ON capabilities_settings"
         "  FOR EACH ROW EXECUTE FUNCTION capabilities_settings_guard();"
         " COMMENT ON TABLE capabilities_settings IS"
         " 'Capability settings, one row per leaf path of a document. A document is"
         " (agent_id, capability, node); agent_id null is every agent''s, node null"
         " every node''s. Writers lock the document with pg_advisory_xact_lock(hashtext("
         "concat_ws(chr(31), schema, coalesce(agent_id, ''''), capability,"
         " coalesce(node, ''''))));"
         " a trigger refuses an object with keys as a value and a path that prefixes"
         " or is prefixed by another row of the same document.';"
         " COMMENT ON COLUMN capabilities_settings.agent_id IS"
         " 'The agent the setting is for; null for the setting every agent inherits.';"
         " COMMENT ON COLUMN capabilities_settings.node IS"
         " 'The node the setting is for; null for every node. A node row is read over"
         " the row for every node.';"
         " COMMENT ON COLUMN capabilities_settings.path IS"
         " 'The keys from the document root to this leaf; a key may contain dots.';"
         " COMMENT ON COLUMN capabilities_settings.value IS"
         " 'The leaf: a JSON scalar (null included), a whole array, or {} for an"
         " object left empty.';"
         " COMMENT ON COLUMN capabilities_settings.updated_by IS"
         " 'Who wrote the row: user@node, migrate@node or <capability>-service@node.'"),
        ("0017-settings-documents",
         "CREATE TABLE capabilities_settings_documents ("
         " agent_id text CHECK (agent_id <> ''),"
         " capability text NOT NULL CHECK (capability <> ''),"
         " node text CHECK (node <> ''),"
         " generation bigint NOT NULL,"
         " created_at timestamptz NOT NULL DEFAULT now());"
         " CREATE UNIQUE INDEX capabilities_settings_documents_key"
         "  ON capabilities_settings_documents"
         "  ((coalesce(agent_id, '')), capability, (coalesce(node, '')));"
         " COMMENT ON TABLE capabilities_settings_documents IS"
         " 'The settings documents that exist: a document without a row here reads as"
         " the capability''s seed; one with a row reads from its rows, even none.';"
         " COMMENT ON COLUMN capabilities_settings_documents.generation IS"
         " 'The id of the last capabilities_settings_changes row applied to the"
         " document; 0 before any.'"),
        ("0018-settings-changes",
         "CREATE TABLE capabilities_settings_changes ("
         " id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
         " agent_id text CHECK (agent_id <> ''),"
         " capability text NOT NULL CHECK (capability <> ''),"
         " node text CHECK (node <> ''),"
         " action text NOT NULL CHECK (action IN ('set', 'unset')),"
         " path text[] NOT NULL CHECK (cardinality(path) > 0),"
         " old_value jsonb,"
         " new_value jsonb,"
         " changed_at timestamptz NOT NULL DEFAULT now(),"
         " changed_by text NOT NULL CHECK (changed_by <> ''));"
         " CREATE FUNCTION capabilities_settings_changes_append_only() RETURNS trigger"
         "  LANGUAGE plpgsql AS $trail$"
         " BEGIN"
         "  RAISE EXCEPTION 'capabilities_settings_changes is append-only'"
         "   USING ERRCODE = 'restrict_violation';"
         " END $trail$;"
         " CREATE TRIGGER capabilities_settings_changes_append_only"
         "  BEFORE UPDATE OR DELETE ON capabilities_settings_changes"
         "  FOR EACH ROW EXECUTE FUNCTION capabilities_settings_changes_append_only();"
         " CREATE TRIGGER capabilities_settings_changes_no_truncate"
         "  BEFORE TRUNCATE ON capabilities_settings_changes"
         "  FOR EACH STATEMENT EXECUTE FUNCTION capabilities_settings_changes_append_only();"
         " COMMENT ON TABLE capabilities_settings_changes IS"
         " 'The append-only trail of settings: one row per path a write set or unset."
         " A document''s generation is the id of its last row here.';"
         " COMMENT ON COLUMN capabilities_settings_changes.action IS"
         " 'set: the path took new_value (old_value null where it was absent);"
         " unset: the path was removed (new_value null).';"
         " COMMENT ON COLUMN capabilities_settings_changes.changed_by IS"
         " 'Who wrote it: user@node, migrate@node or <capability>-service@node.'"),
    ]


def _refuse_record_write(capability: str, collection: str) -> None:
    """A project record is the human's or the manager's, so under the switch
    nothing writes one. Operational state (a capability's state directory and
    its tables in the database) is not a record and keeps working."""
    if read_only_switch():
        raise StoreError(
            "read_only_switch",
            f"writing {capability} {collection} records is refused: "
            f"{READ_ONLY_ENV} is set, so this process changes no project record",
            "Do not lift the switch yourself — ask the user; the change has to "
            f"run in a process without {READ_ONLY_ENV}.")


def _store_check(name: str, value: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StoreError("bad_name", f"{name} {value!r} must match {pattern.pattern}")
    return value


def _semantics(collection: str) -> str:
    spec = COLLECTIONS.get(collection)
    if not spec:
        raise StoreError("bad_collection", f"unknown collection {collection!r}",
                         f"known: {', '.join(sorted(COLLECTIONS))}")
    return spec["resolve"]


def _grant_decisions(cid: str, rows: Sequence[dict[str, Any]]) -> tuple[dict, dict]:
    """One grant assembled field by field, highest scope first.

    This is the line COLLECTIONS already draws between the two records. An
    identity is a fact about the world, so it is atomic: taken whole from
    one scope, never assembled out of two. A grant is a decision, and a
    decision a lower scope made about one field stands until some higher
    scope decides that same field — so a machine-wide `allow_write: false`
    survives a project that only says `enabled: true`, and write permission
    is never acquired by saying something else.

    Returns the decided fields and, per field, the scope that decided it."""
    decided: dict[str, Any] = {}
    decided_at: dict[str, str] = {}
    for row in rows:
        value = row["value"]
        if not isinstance(value, dict):
            raise StoreError("bad_grant",
                             f"grant {cid!r} must be a table, got {type(value).__name__}")
        unknown = set(value) - set(GRANT_FIELDS)
        if unknown:
            raise StoreError("bad_grant",
                             f"grant {cid!r} has unknown field(s): {', '.join(sorted(unknown))}",
                             f"known: {', '.join(GRANT_FIELDS)}")
        for field, decision in value.items():
            if field not in decided:
                decided[field] = decision
                decided_at[field] = row["scope"]
    return decided, decided_at


# --- records: the files a project keeps its configuration in ------------------
#
# Runtime state is not on this axis: runs, queues and cursors a capability's
# processes share live in its tables in the database. What this answers is
# where CONFIGURATION is read and written. A capability calls the verbs below
# and never a path, so the layout is described in one place.

DOCUMENT_LOCATIONS: tuple[tuple[str | None, str, str], ...] = (
    ("reference", "{cap}/reference", ".md"),
    ("context", "{cap}/service/context", ".md"),
    ("script", "automations/scripts", ".py"),
    (None, "{cap}/service", ".md"),
)


def _document_key(prefix: str | None, stem: str) -> str:
    """The key a file answers to. Underscores fold to hyphens and case is lost,
    so the map from file to key is one-way — which is why finding a document
    searches rather than computes."""
    slug = stem.replace("_", "-").lower()
    return f"{prefix}.{slug}" if prefix else slug


class Records:
    """The surface a capability reads and writes its configuration through."""

    mode = "?"
    source = "?"

    def mode_of(self, collection: str, key: str | None = None) -> str:
        """Where one collection is kept: `files`, or `store` for what a
        project in store mode keeps there."""
        return self.mode

    def __enter__(self) -> "Records":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        pass

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        """Resolve exactly one scope, without inheritance."""
        raise NotImplementedError

    def grantees(self, capability: str, connection: str) -> list[str]:
        """Every project a grant enables this connection for, on any node.
        Only the store holds a connection more than one project is granted;
        files keep each project's grants in that project alone."""
        return []

    # Whether settings documents are rows of the store rather than files: true
    # only for an agent whose records moved into the store.
    settings_in_rows = False

    def settings_kept(self) -> str:
        """Where this adapter reads settings documents: `files` or `store`.
        Raises when it cannot tell."""
        return "store" if self.settings_in_rows else "files"

    def guard_write(self, capability: str, collection: str,
                    scope: str | None = None) -> None:
        """Refuse a write this process may not make now. A writer that does
        not go through `set`, such as the settings verb writing a file, asks
        here first."""
        _refuse_record_write(capability, collection)


# A policy record says which node its row names. Files carry no node, so every
# policy record read from them names none: it is the record for every node.
_FILE_NODE = {"policy": {"node": None}}


def _files_carry_no_node(node: Any) -> None:
    if node is not None:
        raise StoreError(
            "node_requires_store",
            "a policy row naming a node is kept only in the store: files carry no "
            "node, so a record written there is for every node",
            "import this project's records into its store with `capabilities "
            "connections import`, then write the row for the node")


class FileRecords(Records):
    """Configuration kept as the files the envelope has always held.

    The layout is not invented here — it is the one the capabilities already
    read, described in one place instead of re-derived at each call site. Two
    directories with that layout make the scope chain: the project's envelope,
    then the user's config home."""

    mode = "files"

    def __init__(self, envelope: Path, global_dir: Path,
                 project_id: str | None = None, project: str | None = None,
                 include_global: bool = True):
        self.envelope, self.global_dir = Path(envelope), Path(global_dir)
        self.project_id, self.project = project_id, project
        self.include_global = bool(include_global)
        self.source = str(self.envelope)
        # The agent whose registry row a project write is checked against, and
        # where its database resolves; set by `open_records` alone.
        self._guard_agent: str | None = None
        self._guard_root: Path | None = None
        self._guard_application = STORE_OWNER
        self.actor: str | None = None

    def _agent_now(self) -> dict | None:
        """This project's registry row as the store holds it now, or None
        where no database answers or it holds none: a project whose records
        are files then writes them as it always has."""
        if not self._guard_agent:
            return None
        try:
            conn, _schema = _store_connect(self._guard_root or self.envelope.parent,
                                           self._guard_application, _PROBE_TIMEOUT)
        except StoreError:
            return None
        try:
            return _agent_state(conn, self._guard_agent)
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def guard_write(self, capability: str, collection: str,
                    scope: str | None = None) -> None:
        """A project record is not written to files while its agent's records
        are moving into the store, nor once they moved."""
        _refuse_record_write(capability, collection)
        if (scope or "project") != "project":
            return
        _refuse_frozen(self._agent_now(), self._guard_agent, self.actor,
                       files=True)

    def setting_document(self, capability: str, scope: str) -> dict | None:
        """One scope's settings document as the adapter's setting collection
        keeps it, or None where there is none."""
        _store_check("capability", capability, CAPABILITY_RE)
        root, _pid = self._scope_root(scope)
        body = self._load(self._file(root, capability, "setting"))
        return body if isinstance(body, dict) else None

    # -- where a record lives --------------------------------------------------

    def _chain(self) -> list[tuple[str, str | None, Path]]:
        chain = [("project", self.project_id, self.envelope)]
        if self.include_global:
            chain.append(("global", None, self.global_dir))
        return chain

    @staticmethod
    def _load(path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise StoreError("bad_config", f"cannot read {path}: {exc}") from None

    @staticmethod
    def _save(path: Path, body: Any, sort: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False,
                                   sort_keys=sort) + "\n")

    def _policy_file(self, root: Path) -> Path:
        """The gate. A project keeps it at the envelope root; the config home
        keeps it under the manager's own name, which is the same rule seen from
        outside -- there, `capabilities` is a capability like any other."""
        return (root / "settings.json" if root == self.envelope
                else root / "capabilities" / "settings.json")

    def _file(self, root: Path, capability: str, collection: str,
              key: str | None = None) -> Path:
        if collection == "policy":
            return self._policy_file(root)
        if collection == "identifier":
            return root / capability / "identifiers.json"
        if collection in ("connection", "grant"):
            return root / capability / "connections.json"
        if collection == "setting":
            if key == "connection.default":
                return root / capability / "connections.json"
            return root / capability / "service" / "settings.json"
        raise StoreError("bad_collection",
                         f"collection {collection!r} is not kept in files")

    # -- reading ---------------------------------------------------------------

    def _entries(self, root: Path, capability: str, collection: str) -> dict[str, Any]:
        """Every entry of one collection held at one scope, keyed by the name a
        record answers to."""
        if collection == "policy":
            body = self._load(self._policy_file(root)) or {}
            return dict((body.get("capabilities") or {}))
        if collection == "identifier":
            body = self._load(root / capability / "identifiers.json") or {}
            raw = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
            out = {}
            for label, entry in raw.items():
                out[label] = entry["value"] if isinstance(entry, dict) and "value" in entry else entry
            return out
        registry = self._load(root / capability / "connections.json") or {}
        if collection in ("connection", "grant"):
            declared = registry.get("connections")
            if not isinstance(declared, dict):
                return {}
            out = {}
            for cid, entry in declared.items():
                if not isinstance(entry, dict):
                    continue
                if collection == "connection":
                    out[cid] = {k: v for k, v in entry.items() if k not in GRANT_FIELDS}
                else:
                    grant = {k: entry[k] for k in GRANT_FIELDS if k in entry}
                    if grant:
                        out[cid] = grant
            return out
        if collection == "setting":
            out = {}
            if registry.get("default"):
                out["connection.default"] = registry["default"]
            service = self._load(root / capability / "service" / "settings.json")
            if isinstance(service, dict):
                out.update(service)
            return out
        raise StoreError("bad_collection", f"unknown collection {collection!r}",
                         f"known: {', '.join(sorted(COLLECTIONS))}")

    def _note(self, root: Path, capability: str, collection: str, key: str) -> str | None:
        if collection != "identifier":
            return None
        body = self._load(root / capability / "identifiers.json") or {}
        raw = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
        entry = raw.get(key)
        return entry.get("note") if isinstance(entry, dict) else None

    def config_layers(self, capability: str, collection: str,
                      _scopes: Any = None) -> list[dict[str, dict[str, Any]]]:
        """Each scope's own entries, highest scope first, uncollapsed."""
        _store_check("capability", capability, CAPABILITY_RE)
        _semantics(collection)
        layers: list[dict[str, dict[str, Any]]] = []
        for scope, pid, root in self._chain():
            entries = self._entries(root, capability, collection)
            if not entries:
                continue
            layers.append({
                key: {"id": f"{scope}:{capability}/{collection}/{key}", "scope": scope,
                      "project_id": pid, "capability": capability,
                      "collection": collection, "key": key, "value": value,
                      "note": self._note(root, capability, collection, key),
                      "updated_at": None, "updated_by": None,
                      **_FILE_NODE.get(collection, {})}
                for key, value in entries.items()
            })
        return layers

    def resolve(self, capability: str, collection: str) -> dict[str, dict[str, Any]]:
        semantics = _semantics(collection)
        out: dict[str, dict[str, Any]] = {}
        for rows in self.config_layers(capability, collection):
            if semantics == FIRST:
                return rows
            for key, row in rows.items():
                prior = out.get(key)
                if prior is None:
                    out[key] = row
                elif (collection == "connection" and not prior["value"]
                        and row["value"]):
                    # One file holds both records here, so an entry carrying
                    # only grant fields yields an identity with no fields. That
                    # is a decision about an inherited connection, never a
                    # replacement for it: it must not blank the identity the
                    # lower scope declared. Where nothing is inherited the
                    # fieldless identity stands, which is the only reading a
                    # glued file leaves for it.
                    out[key] = row
        return out

    def _scope_root(self, scope: str) -> tuple[Path, str | None]:
        """The directory one named scope keeps its records in, and the project
        it answers for. Named once so asking a scope for its rows and asking it
        for its file can never land in different trees."""
        if scope == "project":
            return self.envelope, self.project_id
        if scope == "global":
            return self.global_dir, None
        raise StoreError("bad_scope",
                         f"scope {scope!r} must be one of {', '.join(SCOPE_KINDS)}")

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        _store_check("capability", capability, CAPABILITY_RE)
        root, pid = self._scope_root(scope)
        return {
            key: {"id": f"{scope}:{capability}/{collection}/{key}",
                  "scope": scope, "project_id": pid,
                  "capability": capability, "collection": collection,
                  "key": key, "value": value,
                  "note": self._note(root, capability, collection, key),
                  "updated_at": None, "updated_by": None,
                  **_FILE_NODE.get(collection, {})}
            for key, value in self._entries(root, capability, collection).items()
        }

    def get(self, capability: str, collection: str, key: str) -> Any:
        entry = self.resolve(capability, collection).get(key)
        return entry["value"] if entry else None

    def collection_source(self, capability: str, collection: str) -> str:
        """The file that answered, named exactly. A report that says only
        "files" makes the reader go and find out which one, and the scope a
        record resolved at is half of what they came to learn."""
        for _scope, _pid, root in self._chain():
            if self._entries(root, capability, collection):
                return str(self._file(root, capability, collection))
        return str(self._file(self.envelope, capability, collection))

    def scope_source(self, capability: str, collection: str, scope: str) -> str:
        """The file one named scope keeps this collection in. What a record
        resolved *to* is a question about that record, so the caller that knows
        which scope answered asks here rather than for the collection's own
        file: one file per scope, and the chain is not walked again."""
        _store_check("capability", capability, CAPABILITY_RE)
        root, _pid = self._scope_root(scope)
        return str(self._file(root, capability, collection))

    def connections(self, capability: str, write_default: bool = False,
                    include_disabled: bool = False,
                    machine_read: bool = False) -> dict[str, dict[str, Any]]:
        """Every connection this project may use, already carrying the decision
        made about it — the one read a capability needs before it acts.

        `machine_read` is the caller's word that this is one of the
        capability's declared machine reads outside any project: an identity
        the machine itself declares is then usable, read-only, unless its grant
        resolves to `enabled: false`."""
        identities = self.resolve(capability, "connection")
        layers = self.config_layers(capability, "grant")
        # Under the read-only switch every connection is a source, whatever its
        # grant or the capability's WRITE_DEFAULT says.
        writable = not read_only_switch()
        out: dict[str, dict[str, Any]] = {}
        for cid, entry in identities.items():
            rows = [layer[cid] for layer in layers if cid in layer]
            decided, decided_at = _grant_decisions(cid, rows)
            # Where the identity resolves decides whether it is this project's
            # to use at all. A project that declares an identity locally has
            # already said yes by declaring it; an identity inherited from the
            # global scope is withheld until a grant *resolving at project
            # scope* says `enabled: true`. A global `enabled: true` can never be
            # the blessing — one global line would otherwise hand every project
            # on the machine the same connection back.
            declared = decided.get("enabled")
            identity_is_project = entry["scope"] == "project"
            blessed_here = decided_at.get("enabled") == "project"
            if declared is None:
                enabled = identity_is_project
            else:
                enabled = bool(declared) and (identity_is_project or blessed_here)
            # A machine read lends the machine's own connection and nothing
            # more: never a write, and never one a grant switched off.
            lent = (machine_read and not enabled and entry["scope"] == "global"
                    and (declared is None or bool(declared)))
            if lent:
                enabled = True
            if not enabled and not include_disabled:
                continue
            grant = rows[0] if rows else None
            out[cid] = {
                "id": cid,
                "value": entry["value"],
                "scope": (entry["scope"], entry["project_id"]),
                "enabled": enabled,
                "allow_write": (writable and not lent
                                and bool(decided.get("allow_write", write_default))),
                "grant_scope": (grant["scope"], grant["project_id"]) if grant else None,
            }
            if lent:
                out[cid]["machine_read"] = True
        return out

    # -- writing ---------------------------------------------------------------

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None,
            scope: str | None = None, node: str | None = None) -> None:
        _store_check("capability", capability, CAPABILITY_RE)
        _store_check("key", key, KEY_RE)
        _semantics(collection)
        _files_carry_no_node(node)
        target_scope = scope or "project"
        if target_scope not in SCOPE_KINDS:
            _refuse_record_write(capability, collection)
            raise StoreError("bad_scope",
                             f"scope {target_scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        self.guard_write(capability, collection, target_scope)
        root = self.envelope if target_scope == "project" else self.global_dir
        if target_scope == "project" and not self.envelope.is_dir():
            raise StoreError("no_envelope", "no capabilities/ envelope in this project",
                             "run `capabilities init` first")
        path = self._file(root, capability, collection, key)
        body = self._load(path)
        body = body if isinstance(body, dict) else {}
        if collection == "policy":
            body.setdefault("capabilities", {})[key] = value
        elif collection == "identifier":
            # Flat at the top level, which is the shape the envelope has always
            # had and the shape `audit` holds this to. A file already carrying
            # the wrapper keeps it: reshaping someone's file on an unrelated
            # write is not this writer's business.
            holder = (body["identifiers"] if isinstance(body.get("identifiers"), dict)
                      else body)
            holder[key] = {"value": value, "note": note or ""}
        elif collection in ("connection", "grant"):
            entry = body.setdefault("connections", {}).setdefault(key, {})
            if collection == "connection":
                for field in list(entry):
                    if field not in GRANT_FIELDS:
                        entry.pop(field)
                entry.update(value if isinstance(value, dict) else {})
            else:
                entry.update({k: v for k, v in (value or {}).items() if k in GRANT_FIELDS})
        elif key == "connection.default":
            body["default"] = value
        else:
            body[key] = value
        self._save(path, body, sort=collection == "identifier")

    def delete(self, capability: str, collection: str, key: str,
               scope: str | None = None, node: str | None = None) -> bool:
        _files_carry_no_node(node)
        target_scope = scope or "project"
        if target_scope not in SCOPE_KINDS:
            _refuse_record_write(capability, collection)
            raise StoreError("bad_scope",
                             f"scope {target_scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        self.guard_write(capability, collection, target_scope)
        root = self.envelope if target_scope == "project" else self.global_dir
        path = self._file(root, capability, collection, key)
        body = self._load(path)
        if not isinstance(body, dict):
            return False
        if collection == "policy":
            gone = (body.get("capabilities") or {}).pop(key, None) is not None
        elif collection == "identifier":
            holder = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
            gone = holder.pop(key, None) is not None
        elif collection in ("connection", "grant"):
            entry = (body.get("connections") or {}).get(key)
            if not isinstance(entry, dict):
                return False
            if collection == "connection":
                gone = body["connections"].pop(key, None) is not None
            else:
                gone = any(entry.pop(field, None) is not None for field in GRANT_FIELDS)
        elif key == "connection.default":
            gone = body.pop("default", None) is not None
        else:
            gone = body.pop(key, None) is not None
        if gone:
            self._save(path, body, sort=collection == "identifier")
        return gone

    # -- documents -------------------------------------------------------------

    def _documents(self, root: Path, capability: str) -> dict[str, Path]:
        """Every long-text file this scope holds, by the key it answers to. The
        map is built by walking rather than by computing a path from a key,
        because the key loses case and underscores on the way in."""
        found: dict[str, Path] = {}
        for prefix, shape, suffix in DOCUMENT_LOCATIONS:
            if prefix == "script" and capability != "automations":
                continue
            folder = root / shape.format(cap=capability)
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob(f"*{suffix}")):
                if path.is_file():
                    found.setdefault(_document_key(prefix, path.stem), path)
        return found

    def document_path(self, capability: str, key: str) -> Path | None:
        """The file this key names. It is the truth here, not a copy of it, so
        an edit to what this returns is the edit — which is why `put` validates
        rather than transports."""
        for _scope, _pid, root in self._chain():
            path = self._documents(root, capability).get(key)
            if path:
                return path
        return None

    def document_read(self, capability: str, key: str) -> dict[str, Any] | None:
        for scope, pid, root in self._chain():
            path = self._documents(root, capability).get(key)
            if not path:
                continue
            body = path.read_text()
            return {"id": str(path), "key": key,
                    "hash": hashlib.sha256(body.encode()).hexdigest()[:HASH_LENGTH],
                    "body": body, "media_type": None, "author": None,
                    "created_at": None, "scope": (scope, pid), "path": str(path)}
        return None

    def document_keys(self, capability: str) -> list[str]:
        keys: set[str] = set()
        for _scope, _pid, root in self._chain():
            keys.update(self._documents(root, capability))
        return sorted(keys)

    def document_put(self, capability: str, key: str, body: str,
                     author: str | None = None, media_type: str | None = None,
                     base: str | None = None) -> str:
        self.guard_write(capability, "document")
        path = self._documents(self.envelope, capability).get(key)
        if path is None:
            path = self._new_document_path(capability, key)
        if base is not None and path.is_file():
            current = path.read_text()
            live = hashlib.sha256(current.encode()).hexdigest()[:HASH_LENGTH]
            # In files mode `context edit` deliberately hands out the truth,
            # not a copy. By put-time the live hash is therefore expected to
            # differ from the checkout hash; equality of the submitted body and
            # the file proves the edit landed at the path the adapter named.
            if live != base and current != body:
                raise StoreError(
                    "stale_edit",
                    f"{path} is {live} now, not {base} as it was when the edit began",
                    "re-read it and apply the change again")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return hashlib.sha256(body.encode()).hexdigest()[:HASH_LENGTH]

    def _new_document_path(self, capability: str, key: str) -> Path:
        """Where a document this project does not have yet would go. The prefix
        chooses the folder; what follows it is the file."""
        for prefix, shape, suffix in DOCUMENT_LOCATIONS:
            if prefix and key.startswith(prefix + "."):
                return self.envelope / shape.format(cap=capability) / (key[len(prefix) + 1:] + suffix)
        prefix, shape, suffix = DOCUMENT_LOCATIONS[-1]
        return self.envelope / shape.format(cap=capability) / (key + suffix)

    # -- what a directory cannot answer ---------------------------------------

    def _refuse(self, what: str) -> None:
        raise StoreError("files_mode", f"{what} is not kept: records are files",
                         "their history is the repository's; read it with git")

    def revisions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a record's history")
        return []

    def document_versions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a document's earlier versions")
        return []


class StoreRecords(FileRecords):
    """A project in store mode: its connections, its grants, its default
    pointer, its identifiers and its policy are read from and written to the
    contract's tables in the project's store, and every other record stays in
    the files `FileRecords` reads. A project that moved only its connections
    and grants (`held="connections"`) keeps its identifiers and policy in
    files. The two homes never compose: what the store holds is not read from
    the files at all.

    A connection row belongs to no project, so it resolves as an identity the
    project did not declare itself would: withheld until this project's grant
    enables it. This project's grants are the rows whose `agent_id` is its id,
    the row naming this node read before the row for every node, field by
    field. A row never holds a secret value: `spec` names each secret by its
    `*_env` key, which resolves on each node through that node's own cascade.

    An identifier or a policy row whose `agent_id` is null is global: it
    applies to every project reading this store, beneath the project's own
    row. Identifiers resolve label by label, the project's row over the global
    one. Policy resolves per capability, from the first of: the project's row
    for this node, its row for every node, the global row for this node, the
    global row for every node; none is default deny. A policy record carries
    `node`, the node the row that answered names (None for every node), and
    `set`/`delete` of a policy row take `node` to write or remove the row for
    one node.

    The store is reached through the shared database library, on first use
    only, so a call that reads nothing the store holds never connects, and
    each read or write borrows the process's working connection while it
    runs and gives it back after. A connection handed in belongs to the
    caller and stays open."""

    def __init__(self, envelope: Path, global_dir: Path,
                 project_id: str | None = None, project: str | None = None,
                 include_global: bool = True, project_root: Path | None = None,
                 application_name: str = STORE_OWNER, connection: Any = None,
                 schema: str | None = None, held: str = RECORDS_MODE_KEY):
        super().__init__(envelope, global_dir, project_id, project, include_global)
        if held not in STORE_COLLECTIONS:
            raise StoreError("bad_records_mode",
                             f"a store holds {', '.join(sorted(STORE_COLLECTIONS))}, "
                             f"not {held!r}")
        self.held = held
        self.collections = STORE_COLLECTIONS[held]
        self.project_root = (Path(project_root) if project_root is not None
                             else self.envelope.parent)
        self.application_name = application_name
        self.node = node_name()
        self._conn = connection
        self._owns_conn = connection is None
        # How many operations are running on the owned connection: it is closed
        # when the last of them ends.
        self._depth = 0
        self._schema = schema
        self._failure: StoreError | None = None
        self._failed_at = 0.0
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        # Why this adapter could not ask the store whether the agent's records
        # moved into it. Its settings are then not read from files, which
        # would be stale for an agent that did.
        self._settings_blocked: StoreError | None = None

    def settings_kept(self) -> str:
        if self._settings_blocked is not None:
            raise self._settings_blocked
        return super().settings_kept()

    def setting_document(self, capability: str, scope: str) -> dict | None:
        if self._settings_blocked is not None:
            raise self._settings_blocked
        return super().setting_document(capability, scope)

    def _agent_now(self) -> dict | None:
        # A connection handed in carries its caller's own transaction, which
        # the manager holds for a write of its own.
        if not self._guard_agent or not self._owns_conn:
            return None
        try:
            conn = self._begin()
        except StoreError:
            return None
        import psycopg
        try:
            return _agent_state(conn, self._guard_agent)
        except psycopg.OperationalError as exc:
            raise self._unreachable(exc) from None
        finally:
            if self._conn is not None and self._depth == 1:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            self._end()

    def _held(self, collection: str, key: str | None = None) -> bool:
        return (collection in self.collections
                or (collection == "setting" and key == "connection.default"))

    def mode_of(self, collection: str, key: str | None = None) -> str:
        return "store" if self._held(collection, key) else "files"

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # -- reaching the store ----------------------------------------------------

    def _fail(self, error: StoreError) -> StoreError:
        self._failure, self._failed_at = error, time.monotonic()
        return error

    def _unreachable(self, exc: Exception) -> StoreError:
        """A connection that broke is dropped, so the next use opens a new one."""
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        return self._fail(StoreError(
            "store_unreachable", f"cannot reach the store: {exc}",
            "check the database setting with `capabilities store doctor`"))

    def _fresh(self, key: tuple[str, str]) -> Any:
        held = self._cache.get(key)
        if held is not None and time.monotonic() - held[0] < STORE_READ_SECONDS:
            return held[1]
        return None

    def _begin(self) -> Any:
        """The connection for one operation, borrowed for it unless one is
        running; `_end` gives it back once the last operation on it ends."""
        self._depth += 1
        try:
            return self._db()
        except BaseException:
            self._end()
            raise

    def _end(self) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth == 0 and not _HOLD_RECORDS_CONNECTION:
            self.close()

    def _db(self) -> Any:
        """The connection, opened on first use. A refusal to connect stands for
        STORE_READ_SECONDS, so one command does not wait on it twice."""
        if self._failure is not None:
            if time.monotonic() - self._failed_at < STORE_READ_SECONDS:
                raise self._failure
            self._failure = None
        if self._conn is not None:
            return self._conn
        try:
            from capabilities_contract import db
        except ImportError:
            raise self._fail(StoreError(
                "driver_missing",
                "the shared database library capabilities-contract is not installed",
                "run the capability through its own script header, which pins it")) from None
        try:
            setting = db.resolve_setting(self.project_root)
            self._conn = db.connect(application_name=self.application_name,
                                    setting=setting, transaction=True)
        except db.DbError as exc:
            raise self._fail(StoreError(exc.slug, exc.message, exc.hint)) from None
        self._schema = setting.schema
        return self._conn

    def _read(self, query: str, params: Sequence[Any]) -> list[tuple]:
        conn = self._begin()
        try:
            return self._read_on(conn, query, params)
        finally:
            self._end()

    def _read_on(self, conn: Any, query: str, params: Sequence[Any]) -> list[tuple]:
        import psycopg
        try:
            rows = conn.execute(query, params).fetchall()
        except psycopg.errors.UndefinedTable:
            # A store nothing was ever imported into holds no connection.
            if self._owns_conn:
                conn.rollback()
            return []
        except psycopg.OperationalError as exc:
            raise self._unreachable(exc) from None
        except psycopg.Error as exc:
            if self._owns_conn:
                conn.rollback()
            raise StoreError("store_read_failed",
                             f"reading records from the store failed: {exc}") from None
        if self._owns_conn:
            conn.rollback()
        return rows

    def _read_many(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[list]:
        """The rows of each of `statements`, independent reads, as `_read` gives
        them, sent to the store together on one connection. A table that is
        not there is read as `_read` reads it, one statement at a time."""
        import psycopg
        conn = self._begin()
        try:
            if not self._owns_conn:
                # A caller's transaction: read as `_read` reads in it.
                return [self._read_on(conn, query, params) for query, params in statements]
            try:
                found = _read_together(conn, statements)
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                return [self._read_on(conn, query, params) for query, params in statements]
            except psycopg.OperationalError as exc:
                raise self._unreachable(exc) from None
            except psycopg.Error as exc:
                conn.rollback()
                raise StoreError("store_read_failed",
                                 f"reading records from the store failed: {exc}") from None
            conn.rollback()
            return found
        finally:
            self._end()

    def _write(self, work: Any) -> Any:
        """Run `work(conn)` in one transaction, the tables migrated first. A
        connection handed in belongs to a caller holding its own transaction,
        which this neither migrates nor commits. A write whose connection was
        lost before its commit was sent runs once more on a new one; one lost
        while committing is never written again."""
        conn = self._begin()
        import psycopg
        from capabilities_contract import db
        try:
            try:
                if not self._owns_conn:
                    return work(conn)
                major, minor = STORE_TABLES_VERSION
                for attempt in (0, 1):
                    committing = False
                    try:
                        db.migrate(conn, STORE_OWNER, record_table_steps(),
                                   major=major, minor=minor)
                        with conn.transaction():
                            done = work(conn)
                            committing = True
                        return done
                    except psycopg.OperationalError:
                        if attempt or committing or self._depth != 1 \
                                or not getattr(conn, "broken", False):
                            raise
                    self.close()
                    conn = self._db()
            except db.DbError as exc:
                raise StoreError(exc.slug, exc.message, exc.hint) from None
            except psycopg.OperationalError as exc:
                raise self._unreachable(exc) from None
            except psycopg.Error as exc:
                raise StoreError("store_write_failed",
                                 f"writing records to the store failed: {exc}") from None
        finally:
            self._cache.clear()
            self._end()

    def _table_source(self, collection: str) -> str:
        if self._schema is None:
            self._begin()
            self._end()
        return f"store:{self._schema}.{STORE_TABLES.get(collection, STORE_CONNECTIONS_TABLE)}"

    # -- reading ---------------------------------------------------------------

    def _connection_rows(self, capability: str) -> dict[str, dict[str, Any]]:
        key = ("connection", capability)
        held = self._fresh(key)
        if held is None:
            rows = self._read(
                "SELECT name, spec FROM capabilities_connections "
                "WHERE capability = %s ORDER BY name", (capability,))
            held = {name: spec if isinstance(spec, dict) else {} for name, spec in rows}
            self._cache[key] = (time.monotonic(), held)
        return held

    def _grant_rows(self, capability: str) -> dict[str, list[dict[str, Any]]]:
        """This project's grants on this node, per connection: the row naming
        this node first, then the row for every node."""
        key = ("grant", capability)
        held = self._fresh(key)
        if held is None:
            out: dict[str, list[dict[str, Any]]] = {}
            if self.project_id:
                rows = self._read(
                    "SELECT connection, node, enabled, allow_write, is_default "
                    "FROM capabilities_connection_grants "
                    "WHERE agent_id = %s AND capability = %s "
                    "AND (node IS NULL OR node = %s) "
                    "ORDER BY connection, node NULLS LAST",
                    (self.project_id, capability, self.node))
                for cid, node, enabled, allow_write, is_default in rows:
                    out.setdefault(cid, []).append({
                        "node": node, "enabled": enabled,
                        "allow_write": allow_write, "is_default": bool(is_default)})
            self._cache[key] = (time.monotonic(), out)
            held = out
        return held

    def grantees(self, capability: str, connection: str) -> list[str]:
        _store_check("capability", capability, CAPABILITY_RE)
        if "grant" not in self.collections:
            return []
        rows = self._read(
            "SELECT DISTINCT agent_id FROM capabilities_connection_grants "
            "WHERE capability = %s AND connection = %s AND enabled "
            "AND agent_id IS NOT NULL ORDER BY agent_id", (capability, connection))
        return [agent for (agent,) in rows]

    def _store_default(self, capability: str) -> str | None:
        """The default pointer: this node's, else the one for every node."""
        grants = self._grant_rows(capability)
        for node in (self.node, None):
            for cid in sorted(grants):
                if any(r["is_default"] and r["node"] == node for r in grants[cid]):
                    return cid
        return None

    def _identifier_rows(self, capability: str) -> dict[str, dict[str, tuple]]:
        """This capability's identifiers in the store, per scope: this
        project's rows and the global ones, each label to its value, note and
        time of writing."""
        key = ("identifier", capability)
        held = self._fresh(key)
        if held is None:
            held = {"project": {}, "global": {}}
            # A row for one node is an agent's whose records moved into the
            # store; this project reads the rows for every node. The node is
            # read through the row's JSON, so a store whose identifiers carry
            # no node yet is read the same way.
            rows = self._read(
                "SELECT agent_id, label, value, note, updated_at, "
                "to_jsonb(i) ->> 'node' "
                "FROM capabilities_identifiers i "
                "WHERE capability = %s AND (agent_id IS NULL OR agent_id = %s) "
                "ORDER BY label", (capability, self.project_id))
            for agent_id, label, value, note, updated_at, node in rows:
                if node is not None:
                    continue
                scope = "global" if agent_id is None else "project"
                held[scope][label] = (value, note or "", _stamp(updated_at))
            self._cache[key] = (time.monotonic(), held)
        return held

    def _policy_rows(self) -> list[tuple[str, str | None, dict[str, tuple]]]:
        """The policy rows that apply to this project on this node, as the
        layers they resolve in, highest first: (scope, node, rows by name)."""
        key = ("policy", "")
        held = self._fresh(key)
        if held is None:
            found: dict[tuple[str, str | None], dict[str, tuple]] = {}
            rows = self._read(
                "SELECT agent_id, capability, node, enabled, updated_at "
                "FROM capabilities_policy "
                "WHERE (agent_id IS NULL OR agent_id = %s) "
                "AND (node IS NULL OR node = %s) ORDER BY capability",
                (self.project_id, self.node))
            for agent_id, name, node, enabled, updated_at in rows:
                scope = "global" if agent_id is None else "project"
                found.setdefault((scope, node), {})[name] = (
                    {"enabled": bool(enabled)}, _stamp(updated_at))
            held = [(scope, node, found.get((scope, node), {}))
                    for scope in ("project", "global") for node in (self.node, None)]
            self._cache[key] = (time.monotonic(), held)
        return held

    def _record(self, scope: str, capability: str, collection: str, key: str,
                value: Any, ref: str, note: str | None = None,
                updated_at: str | None = None, node: str | None = None) -> dict[str, Any]:
        record = {"id": f"store:{capability}/{collection}/{ref}", "scope": scope,
                  "project_id": self.project_id if scope == "project" else None,
                  "capability": capability, "collection": collection, "key": key,
                  "value": value, "note": note, "updated_at": updated_at,
                  "updated_by": None}
        if collection == "policy":
            # Which node the row that answered names: this node's name, or None
            # for the row for every node.
            record["node"] = node
        return record

    def _scope_layers(self, capability: str, collection: str,
                      scope: str) -> list[dict[str, dict[str, Any]]]:
        """One scope's identifier or policy layers in the store, highest first:
        for policy, the row for this node before the row for every node."""
        if collection == "identifier":
            rows = self._identifier_rows(capability)[scope]
            return [{label: self._record(scope, capability, collection, label, value,
                                         f"{scope}/{label}", note=note,
                                         updated_at=updated_at)
                     for label, (value, note, updated_at) in rows.items()}]
        return [{name: self._record(scope, capability, collection, name, value,
                                    f"{scope}/{name}@{node or '*'}",
                                    updated_at=updated_at, node=node)
                 for name, (value, updated_at) in rows.items()}
                for at, node, rows in self._policy_rows() if at == scope]

    def _entries(self, root: Path, capability: str, collection: str) -> dict[str, Any]:
        if collection in self.collections:
            return {}
        if collection == "setting" and self._settings_blocked is not None:
            raise self._settings_blocked
        entries = super()._entries(root, capability, collection)
        if collection == "setting":
            entries.pop("connection.default", None)
        return entries

    def config_layers(self, capability: str, collection: str,
                      _scopes: Any = None) -> list[dict[str, dict[str, Any]]]:
        _store_check("capability", capability, CAPABILITY_RE)
        if collection == "connection":
            rows = self._connection_rows(capability)
            return [{name: self._record("store", capability, collection, name, spec, name)
                     for name, spec in rows.items()}] if rows else []
        if collection == "grant":
            layers: list[dict[str, dict[str, Any]]] = []
            for node in (self.node, None):
                layer = {}
                for cid, rows in self._grant_rows(capability).items():
                    for row in rows:
                        if row["node"] == node:
                            value = {f: row[f] for f in GRANT_FIELDS if row[f] is not None}
                            layer[cid] = self._record("project", capability, collection,
                                                      cid, value, f"{cid}@{node or '*'}")
                if layer:
                    layers.append(layer)
            return layers
        if collection in self.collections:
            scopes = ("project", "global") if self.include_global else ("project",)
            return [layer for scope in scopes
                    for layer in self._scope_layers(capability, collection, scope)
                    if layer]
        layers = super().config_layers(capability, collection)
        if collection == "setting":
            default = self._store_default(capability)
            if default is not None:
                layers.insert(0, {"connection.default": self._record(
                    "project", capability, collection, "connection.default", default,
                    "connection.default")})
        return layers

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        if collection in ("connection", "grant") and scope in ("project", "store"):
            return self.resolve(capability, collection)
        if collection in self.collections and collection in ("identifier", "policy"):
            _store_check("capability", capability, CAPABILITY_RE)
            self._scope_root(scope)
            out: dict[str, dict[str, Any]] = {}
            for layer in self._scope_layers(capability, collection, scope):
                for key, row in layer.items():
                    out.setdefault(key, row)
            return out
        return super().resolve_scope(capability, collection, scope)

    def get(self, capability: str, collection: str, key: str) -> Any:
        """A grant answers with the row that applies on this node, and says
        which node it names (null for every node) and whether it is the
        default pointer."""
        if collection == "grant":
            _store_check("capability", capability, CAPABILITY_RE)
            rows = self._grant_rows(capability).get(key)
            if not rows:
                return None
            row = rows[0]
            out = {f: row[f] for f in GRANT_FIELDS if row[f] is not None}
            out.update(node=row["node"], is_default=row["is_default"])
            return out
        return super().get(capability, collection, key)

    def collection_source(self, capability: str, collection: str) -> str:
        if collection in self.collections:
            return self._table_source(collection)
        return super().collection_source(capability, collection)

    def scope_source(self, capability: str, collection: str, scope: str) -> str:
        if collection in self.collections:
            return self._table_source(collection)
        return super().scope_source(capability, collection, scope)

    # -- writing ---------------------------------------------------------------

    def _grant_value(self, cid: str, value: Any) -> tuple[dict[str, Any], str | None]:
        if not isinstance(value, dict):
            raise StoreError("bad_grant",
                             f"grant {cid!r} must be a table, got {type(value).__name__}")
        known = (*GRANT_FIELDS, "node", "is_default")
        unknown = set(value) - set(known)
        if unknown:
            raise StoreError("bad_grant",
                             f"grant {cid!r} has unknown field(s): {', '.join(sorted(unknown))}",
                             f"known: {', '.join(known)}")
        for field in (*GRANT_FIELDS, "is_default"):
            if field in value and not isinstance(value[field], bool) \
                    and not (field in GRANT_FIELDS and value[field] is None):
                raise StoreError("bad_grant", f"grant {cid!r} {field} must be true or false")
        node = value.get("node")
        if node is not None and (not isinstance(node, str) or not node.strip()):
            raise StoreError("bad_grant",
                             f"grant {cid!r} node must name a node, or be null for every node")
        return {f: value[f] for f in (*GRANT_FIELDS, "is_default") if f in value}, node

    def _put_grant(self, conn: Any, capability: str, cid: str,
                   fields: dict[str, Any], node: str | None) -> None:
        if not self.project_id:
            raise StoreError("no_project_id",
                             "this project has no id a grant could name",
                             "run `capabilities init` in the project")
        known = conn.execute(
            "SELECT 1 FROM capabilities_connections WHERE capability = %s AND name = %s",
            (capability, cid)).fetchone()
        if known is None:
            raise StoreError("unknown_connection",
                             f"the store holds no {capability} connection {cid!r}",
                             f"declare it first: `capabilities set {capability} "
                             f"connection {cid} '{{...}}'`")
        if fields.get("is_default"):
            conn.execute(
                "UPDATE capabilities_connection_grants SET is_default = false "
                "WHERE agent_id = %s AND capability = %s AND connection <> %s "
                "AND coalesce(node, '') = coalesce(%s, '') AND is_default",
                (self.project_id, capability, cid, node))
        columns = ["agent_id", "capability", "connection", "node", *fields]
        values = [self.project_id, capability, cid, node, *fields.values()]
        updates = ", ".join(f"{field} = EXCLUDED.{field}" for field in fields)
        conn.execute(
            f"INSERT INTO capabilities_connection_grants ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(values))}) "
            "ON CONFLICT (agent_id, capability, connection, (coalesce(node, ''))) "
            + (f"DO UPDATE SET {updates}" if updates else "DO NOTHING"),
            values)

    def _put_default(self, conn: Any, capability: str, cid: str | None,
                     node: str | None) -> None:
        if not self.project_id:
            raise StoreError("no_project_id",
                             "this project has no id a grant could name",
                             "run `capabilities init` in the project")
        conn.execute(
            "UPDATE capabilities_connection_grants SET is_default = false "
            "WHERE agent_id = %s AND capability = %s "
            "AND coalesce(node, '') = coalesce(%s, '') AND is_default",
            (self.project_id, capability, node))
        if cid is not None:
            self._put_grant(conn, capability, cid, {"is_default": True}, node)

    def _scoped(self, collection: str, key: str | None, scope: str | None) -> bool:
        """Whether a write at `scope` lands in the store. Identifiers and
        policy keep both scopes there; a global connection, grant or default
        pointer stays the config home's."""
        if not self._held(collection, key):
            return False
        return scope != "global" or collection in ("identifier", "policy")

    def _agent(self, scope: str | None) -> str | None:
        """The `agent_id` a row at `scope` carries: this project's id, or null
        for a global row."""
        if scope not in (None, "project", "global"):
            raise StoreError("bad_scope",
                             f"scope {scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        if scope == "global":
            return None
        if not self.project_id:
            raise StoreError("no_project_id",
                             "this project has no id a store row could name",
                             "run `capabilities init` in the project")
        return self.project_id

    def _put_identifier(self, capability: str, label: str, value: Any,
                        note: str | None, scope: str | None) -> None:
        agent = self._agent(scope)
        self._write(lambda conn: conn.execute(
            "INSERT INTO capabilities_identifiers "
            "(agent_id, capability, label, value, note) "
            "VALUES (%s, %s, %s, %s::jsonb, %s) "
            "ON CONFLICT ((coalesce(agent_id, '')), capability, label, "
            "(coalesce(node, ''))) "
            "DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note, "
            "updated_at = now()",
            (agent, capability, label, json.dumps(value, ensure_ascii=False),
             note or "")))

    def _put_policy(self, name: str, value: Any, scope: str | None,
                    node: str | None = None) -> None:
        if (not isinstance(value, dict) or set(value) != {"enabled"}
                or not isinstance(value["enabled"], bool)):
            raise StoreError("bad_policy", f"policy for {name!r} must be "
                             '{"enabled": true|false}')
        agent = self._agent(scope)
        node = _node_check(node)
        self._write(lambda conn: conn.execute(
            "INSERT INTO capabilities_policy (agent_id, capability, node, enabled) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT ((coalesce(agent_id, '')), capability, (coalesce(node, ''))) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
            (agent, name, node, value["enabled"])))

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None,
            scope: str | None = None, node: str | None = None) -> None:
        if not self._scoped(collection, key, scope):
            return super().set(capability, collection, key, value,
                               actor=actor, note=note, scope=scope, node=node)
        _store_check("capability", capability, CAPABILITY_RE)
        _store_check("key", key, KEY_RE)
        _only_policy_names_a_node(collection, node)
        self.guard_write(capability, collection, scope)
        if collection == "identifier":
            return self._put_identifier(capability, key, value, note, scope)
        if collection == "policy":
            return self._put_policy(key, value, scope, node)
        if scope not in (None, "project"):
            raise StoreError("bad_scope",
                             f"scope {scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        if collection == "connection":
            if not isinstance(value, dict):
                raise StoreError("bad_connection",
                                 f"connection {key!r} must be a table, got "
                                 f"{type(value).__name__}")
            spec = {k: v for k, v in value.items() if k not in GRANT_FIELDS}
            grant = {k: value[k] for k in GRANT_FIELDS if k in value}

            def work(conn: Any) -> None:
                conn.execute(
                    "INSERT INTO capabilities_connections (capability, name, spec) "
                    "VALUES (%s, %s, %s::jsonb) ON CONFLICT (capability, name) "
                    "DO UPDATE SET spec = EXCLUDED.spec",
                    (capability, key, json.dumps(spec, ensure_ascii=False)))
                if grant:
                    fields, node = self._grant_value(key, grant)
                    self._put_grant(conn, capability, key, fields, node)
            self._write(work)
        elif collection == "grant":
            fields, node = self._grant_value(key, value)
            self._write(lambda conn: self._put_grant(conn, capability, key, fields, node))
        else:
            cid = value if isinstance(value, str) and value else None
            if value not in (None, "") and cid is None:
                raise StoreError("bad_default", "the default pointer names a connection id")
            self._write(lambda conn: self._put_default(conn, capability, cid, None))

    def delete(self, capability: str, collection: str, key: str,
               scope: str | None = None, node: str | None = None) -> bool:
        if not self._scoped(collection, key, scope):
            return super().delete(capability, collection, key, scope=scope, node=node)
        _only_policy_names_a_node(collection, node)
        self.guard_write(capability, collection, scope)
        if collection == "identifier":
            agent = self._agent(scope)

            def work(conn: Any) -> bool:
                return conn.execute(
                    "DELETE FROM capabilities_identifiers "
                    "WHERE coalesce(agent_id, '') = coalesce(%s, '') "
                    "AND capability = %s AND label = %s AND node IS NULL",
                    (agent, capability, key)).rowcount > 0
        elif collection == "policy":
            agent = self._agent(scope)
            node = _node_check(node)

            def work(conn: Any) -> bool:
                return conn.execute(
                    "DELETE FROM capabilities_policy "
                    "WHERE coalesce(agent_id, '') = coalesce(%s, '') "
                    "AND capability = %s AND coalesce(node, '') = coalesce(%s, '')",
                    (agent, key, node)).rowcount > 0
        elif collection == "connection":
            def work(conn: Any) -> bool:
                return conn.execute(
                    "DELETE FROM capabilities_connections "
                    "WHERE capability = %s AND name = %s", (capability, key)).rowcount > 0
        elif collection == "grant":
            def work(conn: Any) -> bool:
                return conn.execute(
                    "DELETE FROM capabilities_connection_grants WHERE agent_id = %s "
                    "AND capability = %s AND connection = %s AND node IS NULL",
                    (self.project_id, capability, key)).rowcount > 0
        else:
            def work(conn: Any) -> bool:
                return conn.execute(
                    "UPDATE capabilities_connection_grants SET is_default = false "
                    "WHERE agent_id = %s AND capability = %s AND node IS NULL "
                    "AND is_default", (self.project_id, capability)).rowcount > 0
        return bool(self._write(work))


# --- an agent whose records moved into the store --------------------------------

# How long asking the store whether an agent's records moved may take where the
# answer only decides whether its files are still the truth: a store that does
# not answer by then leaves a project whose records are files on its files.
_PROBE_TIMEOUT = 2

# The writers that name themselves: the migration, and a capability's service.
# Every other writer is the person running the command, `<user>@<node>`.
_ACTOR_RE = re.compile(r"migrate|[a-z][a-z0-9-]{0,62}-service")


def _store_connect(project_root: Path, application_name: str,
                   timeout: int | None = None) -> tuple[Any, str]:
    """A transaction connection to the database resolved for `project_root`,
    and its schema, for one operation the caller closes. Raises StoreError."""
    try:
        from capabilities_contract import db
    except ImportError:
        raise StoreError(
            "driver_missing",
            "the shared database library capabilities-contract is not installed",
            "run the capability through its own script header, which pins it") from None
    try:
        setting = db.resolve_setting(project_root)
        extra = {"connect_timeout": timeout} if timeout else {}
        conn = db.connect(application_name=application_name, setting=setting,
                          transaction=True, **extra)
    except db.DbError as exc:
        raise StoreError(exc.slug, exc.message, exc.hint) from None
    return conn, setting.schema


def _read_together(conn: Any, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[list]:
    """The rows of each of `statements`, independent reads on `conn`, sent to the
    store together and answered in one round trip where the shared database
    library offers it (`db.batch`), else one after another."""
    from capabilities_contract import db
    together = getattr(db, "batch", None)
    if together is None:
        return [conn.execute(query, params).fetchall() for query, params in statements]
    return together(conn, list(statements))


def _agent_state(conn: Any, agent_id: str) -> dict | None:
    """The agent's registry row and its nodes, or None where the store holds
    no such row or no registry at all."""
    import psycopg
    from capabilities_contract import db
    agent = ("SELECT migrating_since, migrated_at FROM capabilities_agents WHERE id = %s",
             (agent_id,))
    nodes_of = ("SELECT node, is_master, may_write FROM capabilities_agent_nodes "
                "WHERE agent_id = %s ORDER BY node", (agent_id,))
    try:
        if getattr(db, "batch", None) is not None:
            # Both in one round trip; the nodes are read whether or not the
            # row is there, and count only where it is.
            rows, nodes = db.batch(conn, [agent, nodes_of])
            row = rows[0] if rows else None
        else:
            row = conn.execute(*agent).fetchone()
            nodes = conn.execute(*nodes_of).fetchall() if row else []
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    if row is None:
        return None
    # Stamped here rather than through `_stamp`, a name a capability may also
    # define for itself: a moment that is not there stays None.
    since, moved = (None if moment is None else moment.isoformat() for moment in row)
    return {"id": agent_id, "migrating_since": since, "migrated_at": moved,
            "nodes": {node: {"is_master": bool(master), "may_write": bool(may_write)}
                      for node, master, may_write in nodes},
            "master": next((node for node, master, _w in nodes if master), None)}


def _master_hint(state: dict) -> str:
    master = state.get("master")
    if master:
        return (f"the agent's master node is {master}: register this node there with "
                "`capabilities node add <node> --kind mac|server`, and run here with "
                f"{NODE_NAME_ENV} set to that name")
    return ("the agent has no master node registered: register one with "
            "`capabilities node add <node> --kind mac|server --master` on the machine "
            f"that is its master, and run there with {NODE_NAME_ENV} set to that name")


def _refuse_frozen(state: dict | None, agent_id: str | None, role: str | None,
                   node: str | None = None, files: bool = False) -> None:
    """A write to an agent whose records are moving into the store comes from
    the migration on its master alone; a file write for an agent whose records
    already moved is never made."""
    if not state:
        return
    if files and state.get("migrated_at"):
        raise StoreError(
            "agent_migrated",
            f"agent {agent_id} keeps its records in the store since "
            f"{state['migrated_at']}, and this process opened them as files",
            "run the command again: a new process reads and writes them in the store")
    since = state.get("migrating_since")
    if not since:
        return
    here = node if node is not None else os.environ.get(NODE_NAME_ENV, "").strip()
    if role == "migrate" and here and here == state.get("master"):
        return
    raise StoreError(
        "agent_migrating",
        f"agent {agent_id}'s records are being moved into the store (since {since}); "
        f"until that finishes only the migration on its master "
        f"{state.get('master') or '(none registered)'} writes them",
        "wait for `capabilities migrate` to finish on the master, then run it again")


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise StoreError("bad_setting", f"a setting holds JSON only: {exc}") from None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _tree_put(document: dict, path: Sequence[str], value: Any) -> None:
    """Put `value` at `path`, making each missing or non-object parent an
    object; an object given replaces the subtree there."""
    here = document
    for part in path[:-1]:
        if not isinstance(here.get(part), dict):
            here[part] = {}
        here = here[part]
    here[path[-1]] = _json_copy(value)


def settings_tree(rows: Sequence[tuple[Sequence[str], Any]]) -> dict:
    """A settings document assembled from its leaf rows."""
    document: dict = {}
    for path, value in sorted(rows, key=lambda row: list(row[0])):
        _tree_put(document, list(path), value)
    return document


def settings_leaves(document: dict, prefix: tuple = ()) -> dict[tuple, Any]:
    """A settings document as its leaf rows: every path to a scalar, an array
    or an empty object. A document's root is never a leaf."""
    out: dict[tuple, Any] = {}
    for key, value in document.items():
        if not isinstance(key, str):
            raise StoreError("bad_setting", f"a settings key is text, not {key!r}")
        path = (*prefix, key)
        if isinstance(value, dict) and value:
            out.update(settings_leaves(value, path))
        else:
            out[path] = value
    return out


class _Unchanged(Exception):
    """Raised by a settings change that finds nothing to do, so the write
    it runs in leaves the document as it was."""


def _user_name() -> str:
    name = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not name:
        try:
            import getpass
            name = getpass.getuser()
        except Exception:
            name = "unknown"
    return name


class AgentRecords(StoreRecords):
    """An agent whose records moved into the store: its `capabilities_agents`
    row has `migrated_at`. Every record it keeps - connections, grants, the
    default pointer, identifiers, policy - and its settings documents are rows
    of the contract's tables; its files are not read. Documents stay files.

    Each record resolves the agent's own rows over the rows for every agent
    (`agent_id` null), and at each of those scopes the row for this node over
    the row for every node. This node is AGENT_NODE_NAME, registered among the
    agent's nodes; `open_records` refuses an agent that has moved without it.

    Who writes what: the agent's master writes the rows every node shares
    (node null); another node writes only where the master let it
    (`may_write`), and what it writes carries its own node. While the agent's
    `migrating_since` is set, only the migration on its master writes. A
    settings document is written in one transaction under its advisory lock:
    assembled, changed, checked whole, then stored as the paths that changed,
    removals first, each with a trail row, and its generation moved to the
    last of them."""

    settings_in_rows = True

    def __init__(self, envelope: Path, global_dir: Path, project_id: str,
                 project: str | None, *, agent: dict, node: str,
                 include_global: bool = True, project_root: Path | None = None,
                 application_name: str = STORE_OWNER, actor: str | None = None):
        super().__init__(envelope, global_dir, project_id, project,
                         include_global=include_global, project_root=project_root,
                         application_name=application_name, held=RECORDS_MODE_KEY)
        self.collections = frozenset({"connection", "grant", "identifier", "policy",
                                      "setting"})
        self.agent = agent
        self.node = node
        self.actor = actor
        self._guard_agent = project_id
        self._ledger_current = False
        self._txn = False

    def _held(self, collection: str, key: str | None = None) -> bool:
        return collection in self.collections

    def settings_kept(self) -> str:
        return "store"

    # -- reaching the store ----------------------------------------------------

    def _db(self) -> Any:
        """The connection, its tables brought to this code's version once."""
        conn = super()._db()
        if not self._ledger_current and self._owns_conn and not self._txn:
            import psycopg
            from capabilities_contract import db
            major, minor = STORE_TABLES_VERSION
            try:
                db.migrate(conn, STORE_OWNER, record_table_steps(), major=major, minor=minor)
            except db.DbError as exc:
                raise StoreError(exc.slug, exc.message, exc.hint) from None
            except psycopg.OperationalError as exc:
                raise self._unreachable(exc) from None
            self._ledger_current = True
        return conn

    def _role(self, actor: str | None) -> str | None:
        for named in (actor, self.actor):
            if isinstance(named, str) and _ACTOR_RE.fullmatch(named):
                return named
        return None

    def actor_name(self, actor: str | None = None) -> str:
        """Who a write is recorded as: `migrate@<node>`,
        `<capability>-service@<node>`, or the person, `<user>@<node>`."""
        return f"{self._role(actor) or _user_name()}@{self.node}"

    def _authority(self, conn: Any, scope: str | None, wanted: str | None,
                   actor: str | None) -> str | None:
        """The node a write lands on, decided from the registry as it stands
        in this transaction: the master writes the shared rows (or a row for
        the node it names); another node writes its own rows where it may, and
        nothing else."""
        state = _agent_state(conn, self.project_id) or {}
        if scope != "global":
            _refuse_frozen(state, self.project_id, self._role(actor), node=self.node)
        mine = (state.get("nodes") or {}).get(self.node)
        if mine is None:
            raise StoreError("node_unregistered",
                             f"node {self.node} is not a node of agent {self.project_id}",
                             _master_hint(state))
        if mine["is_master"]:
            return wanted
        if not mine["may_write"]:
            raise StoreError(
                "node_read_only",
                f"node {self.node} is not the master of agent {self.project_id} and "
                "writes nothing: the rows every node shares are written on the master "
                f"{state.get('master') or '(none registered)'}",
                "write it on the master, or let the master allow this node its own "
                f"rows with `capabilities node set {self.node} --may-write`")
        if wanted not in (None, self.node):
            raise StoreError(
                "node_read_only",
                f"node {self.node} is not the master of agent {self.project_id} and "
                f"writes only rows of its own, not rows for node {wanted}",
                f"write it on the master {state.get('master') or '(none registered)'}")
        return self.node

    def _write(self, work: Any) -> Any:
        """One transaction, as the store tier's own, during which reads made
        on the same connection (a settings check reading the other scope)
        stay inside it."""
        def inside(conn: Any) -> Any:
            self._txn = True
            try:
                return work(conn)
            finally:
                self._txn = False
        return super()._write(inside)

    def _read(self, query: str, params: Sequence[Any]) -> list[tuple]:
        if not self._txn:
            return super()._read(query, params)
        import psycopg
        try:
            return self._db().execute(query, params).fetchall()
        except psycopg.OperationalError as exc:
            raise self._unreachable(exc) from None
        except psycopg.Error as exc:
            raise StoreError("store_read_failed",
                             f"reading records from the store failed: {exc}") from None

    def _read_many(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[list]:
        if not self._txn:
            return super()._read_many(statements)
        import psycopg
        try:
            return _read_together(self._db(), statements)
        except psycopg.OperationalError as exc:
            raise self._unreachable(exc) from None
        except psycopg.Error as exc:
            raise StoreError("store_read_failed",
                             f"reading records from the store failed: {exc}") from None

    def guard_write(self, capability: str, collection: str,
                    scope: str | None = None) -> None:
        _refuse_record_write(capability, collection)
        if (scope or "project") == "project" and collection == "document":
            state = self._agent_now()
            _refuse_frozen(state, self.project_id, self._role(None), node=self.node)

    # -- reading ---------------------------------------------------------------

    def _record(self, scope: str, capability: str, collection: str, key: str,
                value: Any, ref: str, note: str | None = None,
                updated_at: str | None = None, node: str | None = None) -> dict[str, Any]:
        record = super()._record(scope, capability, collection, key, value, ref,
                                 note=note, updated_at=updated_at, node=node)
        if collection in ("identifier", "grant"):
            record["node"] = node
        return record

    def _grant_rows(self, capability: str) -> dict[str, list[dict[str, Any]]]:
        """This agent's grants and the grants for every agent, per connection,
        in the order they decide: the agent's before every agent's, and at
        each the row naming this node before the row for every node."""
        key = ("grant", capability)
        held = self._fresh(key)
        if held is None:
            out: dict[str, list[dict[str, Any]]] = {}
            rows = self._read(
                "SELECT agent_id, connection, node, enabled, allow_write, is_default "
                "FROM capabilities_connection_grants "
                "WHERE capability = %s AND (agent_id = %s OR (agent_id IS NULL AND %s)) "
                "AND (node IS NULL OR node = %s) "
                "ORDER BY connection, agent_id NULLS LAST, node NULLS LAST",
                (capability, self.project_id, self.include_global, self.node))
            for agent_id, cid, node, enabled, allow_write, is_default in rows:
                out.setdefault(cid, []).append({
                    "scope": "global" if agent_id is None else "project",
                    "node": node, "enabled": enabled, "allow_write": allow_write,
                    "is_default": bool(is_default)})
            self._cache[key] = (time.monotonic(), out)
            held = out
        return held

    def _defaults(self, capability: str) -> dict[str, str]:
        """The default pointer at each scope: this node's, else every node's."""
        grants = self._grant_rows(capability)
        found: dict[str, str] = {}
        for scope in ("project", "global"):
            for node in (self.node, None):
                for cid in sorted(grants):
                    if scope not in found and any(
                            r["is_default"] and r["node"] == node and r["scope"] == scope
                            for r in grants[cid]):
                        found[scope] = cid
        return found

    def _store_default(self, capability: str) -> str | None:
        found = self._defaults(capability)
        return found.get("project") or found.get("global")

    def _identifier_rows(self, capability: str) -> dict:
        key = ("identifier", capability)
        held = self._fresh(key)
        if held is None:
            held = {(scope, node): {} for scope in ("project", "global")
                    for node in (self.node, None)}
            rows = self._read(
                "SELECT agent_id, node, label, value, note, updated_at "
                "FROM capabilities_identifiers "
                "WHERE capability = %s AND (agent_id IS NULL OR agent_id = %s) "
                "AND (node IS NULL OR node = %s) ORDER BY label",
                (capability, self.project_id, self.node))
            for agent_id, node, label, value, note, updated_at in rows:
                scope = "global" if agent_id is None else "project"
                held[(scope, node)][label] = (value, note or "", _stamp(updated_at))
            self._cache[key] = (time.monotonic(), held)
        return held

    def _scope_layers(self, capability: str, collection: str,
                      scope: str) -> list[dict[str, dict[str, Any]]]:
        if collection != "identifier":
            return super()._scope_layers(capability, collection, scope)
        rows = self._identifier_rows(capability)
        return [{label: self._record(scope, capability, collection, label, value,
                                     f"{scope}/{label}@{node or '*'}", note=note,
                                     updated_at=updated_at, node=node)
                 for label, (value, note, updated_at) in rows[(scope, node)].items()}
                for node in (self.node, None)]

    def _settings_rows(self, capability: str) -> tuple[dict, dict]:
        """The settings documents that exist for this agent and every agent,
        on this node and every node, with their generations, and their rows."""
        key = ("setting", capability)
        held = self._fresh(key)
        if held is None:
            docs: dict[tuple, int] = {}
            rows: dict[tuple, list] = {}
            where = (capability, self.project_id, self.node)
            documents, leaves = self._read_many([
                ("SELECT agent_id, node, generation FROM capabilities_settings_documents "
                 "WHERE capability = %s AND (agent_id IS NULL OR agent_id = %s) "
                 "AND (node IS NULL OR node = %s)", where),
                ("SELECT agent_id, node, path, value FROM capabilities_settings "
                 "WHERE capability = %s AND (agent_id IS NULL OR agent_id = %s) "
                 "AND (node IS NULL OR node = %s) ORDER BY path", where)])
            for agent_id, node, generation in documents:
                docs[("global" if agent_id is None else "project", node)] = int(generation)
            for agent_id, node, path, value in leaves:
                rows.setdefault(("global" if agent_id is None else "project", node),
                                []).append((list(path), value))
            held = (docs, rows)
            self._cache[key] = (time.monotonic(), held)
        return held

    def setting_document(self, capability: str, scope: str) -> dict | None:
        """One scope's settings document as this node reads it: the rows for
        every node with this node's rows over them. None where neither
        document exists, which a reader takes as the capability's seed."""
        _store_check("capability", capability, CAPABILITY_RE)
        self._scope_root(scope)
        docs, rows = self._settings_rows(capability)
        shared, mine = (scope, None), (scope, self.node)
        if not any(k in docs or k in rows for k in (shared, mine)):
            return None
        document = settings_tree(rows.get(shared, []))
        for path, value in rows.get(mine, []):
            _tree_put(document, path, value)
        return document

    def setting_generations(self, capability: str) -> dict[str, int | None]:
        """The generation each scope's document was assembled from: the
        latest of its shared and this node's documents, None where none
        exists."""
        docs, _rows = self._settings_rows(capability)
        out: dict[str, int | None] = {}
        for scope in ("project", "global"):
            found = [docs[(scope, node)] for node in (None, self.node) if (scope, node) in docs]
            out[scope] = max(found) if found else None
        return out

    def config_layers(self, capability: str, collection: str,
                      _scopes: Any = None) -> list[dict[str, dict[str, Any]]]:
        _store_check("capability", capability, CAPABILITY_RE)
        if collection == "grant":
            layers: list[dict[str, dict[str, Any]]] = []
            for scope in ("project", "global"):
                for node in (self.node, None):
                    layer = {}
                    for cid, rows in self._grant_rows(capability).items():
                        for row in rows:
                            if row["scope"] == scope and row["node"] == node:
                                value = {f: row[f] for f in GRANT_FIELDS if row[f] is not None}
                                layer[cid] = self._record(
                                    scope, capability, collection, cid, value,
                                    f"{scope}/{cid}@{node or '*'}", node=node)
                    if layer:
                        layers.append(layer)
            return layers
        if collection == "setting":
            scopes = ("project", "global") if self.include_global else ("project",)
            return [layer for layer in (self.resolve_scope(capability, "setting", scope)
                                        for scope in scopes) if layer]
        return super().config_layers(capability, collection)

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        if collection == "setting":
            _store_check("capability", capability, CAPABILITY_RE)
            self._scope_root(scope)
            out = {key: self._record(scope, capability, collection, key, value,
                                     f"{scope}/{key}")
                   for key, value in (self.setting_document(capability, scope) or {}).items()}
            default = self._defaults(capability).get(scope)
            if default is not None:
                out["connection.default"] = self._record(
                    scope, capability, collection, "connection.default", default,
                    f"{scope}/connection.default")
            return out
        if collection == "grant":
            if scope == "store":
                scope = "project"
            self._scope_root(scope)
            out = {}
            for layer in self.config_layers(capability, "grant"):
                for key, row in layer.items():
                    if row["scope"] == scope:
                        out.setdefault(key, row)
            return out
        if collection == "connection" and scope == "global":
            return {}
        return super().resolve_scope(capability, collection, scope)

    def get(self, capability: str, collection: str, key: str) -> Any:
        if collection == "grant":
            _store_check("capability", capability, CAPABILITY_RE)
            rows = self._grant_rows(capability).get(key)
            if not rows:
                return None
            row = rows[0]
            out = {f: row[f] for f in GRANT_FIELDS if row[f] is not None}
            out.update(node=row["node"], is_default=row["is_default"], scope=row["scope"])
            return out
        return super().get(capability, collection, key)

    # -- writing ---------------------------------------------------------------

    def _lock_document(self, conn: Any, agent: str | None, capability: str,
                       node: str | None) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(concat_ws(chr(31), %s::text, "
            "coalesce(%s::text, ''), %s::text, coalesce(%s::text, ''))))",
            (self._schema, agent, capability, node))

    def _document_rows(self, conn: Any, agent: str | None, capability: str,
                       node: str | None) -> tuple[dict[tuple, Any], int | None]:
        rows, documents = _read_together(conn, [
            ("SELECT path, value FROM capabilities_settings "
             "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
             "AND coalesce(node, '') = coalesce(%s::text, '')", (agent, capability, node)),
            ("SELECT generation FROM capabilities_settings_documents "
             "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
             "AND coalesce(node, '') = coalesce(%s::text, '')", (agent, capability, node))])
        found = documents[0] if documents else None
        return ({tuple(path): value for path, value in rows},
                int(found[0]) if found else None)

    def settings_write(self, capability: str, scope: str, change: Any,
                       check: Any = None, seed: dict | None = None,
                       actor: str | None = None) -> dict[str, Any]:
        """Change one scope's settings document in one transaction: lock it,
        assemble it (the seed where no document exists yet), apply `change`
        (a function from the document to the new one), check the result as
        this node will read it with `check` (a function returning problems),
        then write the paths that differ, removals first, one trail row each,
        and move the document's generation to the last of them. A refused
        write changes nothing."""
        _store_check("capability", capability, CAPABILITY_RE)
        _refuse_record_write(capability, "setting")
        agent = self._agent(scope)
        who = self.actor_name(actor)

        def work(conn: Any) -> dict[str, Any]:
            node = self._authority(conn, scope, None, actor)
            self._lock_document(conn, agent, capability, node)
            old, generation = self._document_rows(conn, agent, capability, node)
            exists = generation is not None or bool(old)
            current = (settings_tree(list(old.items())) if exists
                       else _json_copy(seed or {}) if node is None else {})
            before = _json_copy(current)
            after = change(_json_copy(current))
            if not isinstance(after, dict):
                raise StoreError("bad_setting", "a settings document is an object")
            after = _json_copy(after)
            if check is not None:
                problems = check(self._as_read(conn, agent, capability, node, after,
                                               seed)) or []
                if problems:
                    raise StoreError("settings_invalid", str(problems[0]))
            new = settings_leaves(after)
            changed = 0

            def trail(action: str, path: tuple, was: Any, now: Any) -> int:
                return conn.execute(
                    "INSERT INTO capabilities_settings_changes "
                    "(agent_id, capability, node, action, path, old_value, new_value, "
                    "changed_by) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) "
                    "RETURNING id",
                    (agent, capability, node, action, list(path),
                     None if action == "set" and path not in old else _canonical(was),
                     None if action == "unset" else _canonical(now), who)).fetchone()[0]

            for path in sorted(set(old) - set(new)):
                conn.execute(
                    "DELETE FROM capabilities_settings "
                    "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') "
                    "AND capability = %s AND coalesce(node, '') = coalesce(%s::text, '') "
                    "AND path = %s", (agent, capability, node, list(path)))
                generation = trail("unset", path, old[path], None)
                changed += 1
            for path in sorted(new):
                if path in old and _canonical(old[path]) == _canonical(new[path]):
                    continue
                if path in old:
                    conn.execute(
                        "UPDATE capabilities_settings SET value = %s::jsonb, "
                        "updated_at = now(), updated_by = %s "
                        "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') "
                        "AND capability = %s AND coalesce(node, '') = coalesce(%s::text, '') "
                        "AND path = %s",
                        (_canonical(new[path]), who, agent, capability, node, list(path)))
                else:
                    conn.execute(
                        "INSERT INTO capabilities_settings "
                        "(agent_id, capability, node, path, value, updated_by) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                        (agent, capability, node, list(path), _canonical(new[path]), who))
                generation = trail("set", path, old.get(path), new[path])
                changed += 1
            conn.execute(
                "INSERT INTO capabilities_settings_documents "
                "(agent_id, capability, node, generation) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT ((coalesce(agent_id, '')), capability, (coalesce(node, ''))) "
                "DO UPDATE SET generation = EXCLUDED.generation",
                (agent, capability, node, generation or 0))
            return {"before": before, "after": after, "generation": generation or 0,
                    "changed": changed, "node": node, "actor": who,
                    "source": self._table_source("setting")}
        return self._write(work)

    def _as_read(self, conn: Any, agent: str | None, capability: str,
                 node: str | None, document: dict, seed: dict | None) -> dict:
        """`document`, written for `node`, as this node will read its scope:
        the shared document with this node's rows over it."""
        if node is None:
            mine, _gen = self._document_rows(conn, agent, capability, self.node)
            composed = _json_copy(document)
        else:
            shared, generation = self._document_rows(conn, agent, capability, None)
            composed = (settings_tree(list(shared.items()))
                        if shared or generation is not None else _json_copy(seed or {}))
            mine = settings_leaves(document)
        for path, value in sorted(mine.items()):
            _tree_put(composed, list(path), value)
        return composed

    def _put_grant_at(self, conn: Any, capability: str, cid: str,
                      fields: dict[str, Any], agent: str | None, node: str | None) -> None:
        known = conn.execute(
            "SELECT 1 FROM capabilities_connections WHERE capability = %s AND name = %s",
            (capability, cid)).fetchone()
        if known is None:
            raise StoreError("unknown_connection",
                             f"the store holds no {capability} connection {cid!r}",
                             f"declare it first: `capabilities set {capability} "
                             f"connection {cid} '{{...}}'`")
        if fields.get("is_default"):
            conn.execute(
                "UPDATE capabilities_connection_grants SET is_default = false "
                "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
                "AND connection <> %s AND coalesce(node, '') = coalesce(%s::text, '') "
                "AND is_default", (agent, capability, cid, node))
        columns = ["agent_id", "capability", "connection", "node", *fields]
        values = [agent, capability, cid, node, *fields.values()]
        updates = ", ".join(f"{field} = EXCLUDED.{field}" for field in fields)
        conn.execute(
            f"INSERT INTO capabilities_connection_grants ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(values))}) "
            "ON CONFLICT ((coalesce(agent_id, '')), capability, connection, "
            "(coalesce(node, ''))) "
            + (f"DO UPDATE SET {updates}" if updates else "DO NOTHING"),
            values)

    def _put_default_at(self, conn: Any, capability: str, cid: str | None,
                        agent: str | None, node: str | None) -> bool:
        cleared = conn.execute(
            "UPDATE capabilities_connection_grants SET is_default = false "
            "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
            "AND coalesce(node, '') = coalesce(%s::text, '') AND is_default",
            (agent, capability, node)).rowcount
        if cid is not None:
            self._put_grant_at(conn, capability, cid, {"is_default": True}, agent, node)
        return cleared > 0

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None,
            scope: str | None = None, node: str | None = None) -> None:
        if collection not in self.collections:
            return FileRecords.set(self, capability, collection, key, value,
                                   actor=actor, note=note, scope=scope, node=node)
        _store_check("capability", capability, CAPABILITY_RE)
        _store_check("key", key, KEY_RE)
        _only_policy_names_a_node(collection, node)
        _refuse_record_write(capability, collection)
        agent = self._agent(scope)
        if collection == "setting" and key != "connection.default":
            def put(document: dict) -> dict:
                document[key] = value
                return document
            self.settings_write(capability, scope or "project", put, actor=actor)
            return None
        if collection == "setting":
            cid = value if isinstance(value, str) and value else None
            if value not in (None, "") and cid is None:
                raise StoreError("bad_default", "the default pointer names a connection id")
            self._write(lambda conn: self._put_default_at(
                conn, capability, cid, agent, self._authority(conn, scope, None, actor)))
            return None
        if collection == "identifier":
            def work(conn: Any) -> None:
                at = self._authority(conn, scope, None, actor)
                conn.execute(
                    "INSERT INTO capabilities_identifiers "
                    "(agent_id, capability, node, label, value, note) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT ((coalesce(agent_id, '')), capability, label, "
                    "(coalesce(node, ''))) "
                    "DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note, "
                    "updated_at = now()",
                    (agent, capability, at, key, json.dumps(value, ensure_ascii=False),
                     note or ""))
            self._write(work)
            return None
        if collection == "policy":
            if (not isinstance(value, dict) or set(value) != {"enabled"}
                    or not isinstance(value["enabled"], bool)):
                raise StoreError("bad_policy", f"policy for {key!r} must be "
                                 '{"enabled": true|false}')
            wanted = _node_check(node)
            self._write(lambda conn: conn.execute(
                "INSERT INTO capabilities_policy (agent_id, capability, node, enabled) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT ((coalesce(agent_id, '')), capability, (coalesce(node, ''))) "
                "DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
                (agent, key, self._authority(conn, scope, wanted, actor), value["enabled"])))
            return None
        if collection == "connection":
            if not isinstance(value, dict):
                raise StoreError("bad_connection",
                                 f"connection {key!r} must be a table, got "
                                 f"{type(value).__name__}")
            spec = {k: v for k, v in value.items() if k not in GRANT_FIELDS}
            grant = {k: value[k] for k in GRANT_FIELDS if k in value}

            def work(conn: Any) -> None:
                at = self._authority(conn, scope, None, actor)
                if at is None:
                    conn.execute(
                        "INSERT INTO capabilities_connections (capability, name, spec) "
                        "VALUES (%s, %s, %s::jsonb) ON CONFLICT (capability, name) "
                        "DO UPDATE SET spec = EXCLUDED.spec",
                        (capability, key, json.dumps(spec, ensure_ascii=False)))
                else:
                    # A connection is one row for every agent and node, so a
                    # node that writes only its own rows may add one, never
                    # change one another node may use.
                    added = conn.execute(
                        "INSERT INTO capabilities_connections (capability, name, spec) "
                        "VALUES (%s, %s, %s::jsonb) ON CONFLICT (capability, name) "
                        "DO NOTHING", (capability, key,
                                       json.dumps(spec, ensure_ascii=False))).rowcount
                    if not added:
                        raise StoreError(
                            "node_read_only",
                            f"node {self.node} may add a connection but not change "
                            f"{capability} connection {key!r}, which every node shares",
                            f"change it on the master {self.agent.get('master')}")
                if grant:
                    fields, wanted = self._grant_value(key, grant)
                    self._put_grant_at(conn, capability, key, fields, agent,
                                       wanted if at is None else at)
            self._write(work)
            return None
        fields, wanted = self._grant_value(key, value)
        self._write(lambda conn: self._put_grant_at(
            conn, capability, key, fields, agent,
            self._authority(conn, scope, wanted, actor)))
        return None

    def delete(self, capability: str, collection: str, key: str,
               scope: str | None = None, node: str | None = None) -> bool:
        if collection not in self.collections:
            return FileRecords.delete(self, capability, collection, key,
                                      scope=scope, node=node)
        _only_policy_names_a_node(collection, node)
        _refuse_record_write(capability, collection)
        agent = self._agent(scope)
        if collection == "setting" and key != "connection.default":
            def drop(document: dict) -> dict:
                if key not in document:
                    # Nothing to remove leaves the document as it was, even
                    # one that does not exist yet.
                    raise _Unchanged()
                document.pop(key)
                return document
            try:
                self.settings_write(capability, scope or "project", drop)
            except _Unchanged:
                return False
            return True
        if collection == "setting":
            return bool(self._write(lambda conn: self._put_default_at(
                conn, capability, None, agent, self._authority(conn, scope, None, None))))
        wanted = _node_check(node) if collection == "policy" else None
        if collection == "identifier":
            sql = ("DELETE FROM capabilities_identifiers "
                   "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
                   "AND label = %s AND coalesce(node, '') = coalesce(%s::text, '')")
            target = (agent, capability, key)
        elif collection == "policy":
            sql = ("DELETE FROM capabilities_policy "
                   "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
                   "AND coalesce(node, '') = coalesce(%s::text, '')")
            target = (agent, key)
        elif collection == "grant":
            sql = ("DELETE FROM capabilities_connection_grants "
                   "WHERE coalesce(agent_id, '') = coalesce(%s::text, '') AND capability = %s "
                   "AND connection = %s AND coalesce(node, '') = coalesce(%s::text, '')")
            target = (agent, capability, key)
        else:
            def work(conn: Any) -> bool:
                if self._authority(conn, scope, None, None) is not None:
                    raise StoreError(
                        "node_read_only",
                        f"node {self.node} may not remove {capability} connection "
                        f"{key!r}, which every node shares",
                        f"remove it on the master {self.agent.get('master')}")
                return conn.execute(
                    "DELETE FROM capabilities_connections WHERE capability = %s "
                    "AND name = %s", (capability, key)).rowcount > 0
            return bool(self._write(work))
        return bool(self._write(lambda conn: conn.execute(
            sql, (*target, self._authority(conn, scope, wanted, None))).rowcount > 0))


def _only_policy_names_a_node(collection: str, node: Any) -> None:
    """A grant names its node inside its value; a policy row is the one record
    written for a node through `node`."""
    if node is not None and collection != "policy":
        raise StoreError("bad_node", f"a {collection} record is not written for a "
                         "node this way; only a policy row is")


def _stamp(moment: Any) -> str | None:
    """A row's time of writing as the text a report carries."""
    return moment.isoformat() if hasattr(moment, "isoformat") else moment


def _identity_record(envelope: Path) -> dict[str, Any]:
    try:
        identity = json.loads((Path(envelope) / "project.json").read_text())
    except (OSError, ValueError):
        return {}
    return identity if isinstance(identity, dict) else {}


def records_mode(envelope: Path | str) -> str:
    """Where this project keeps its records: `files`, unless its
    `project.json` says `"records": "store"`. Only that record decides it."""
    identity_file = Path(envelope) / "project.json"
    declared = _identity_record(Path(envelope)).get(RECORDS_MODE_KEY, "files")
    if declared not in RECORDS_MODES:
        raise StoreError("bad_records_mode",
                         f"{identity_file} declares records {declared!r}",
                         f"one of: {', '.join(RECORDS_MODES)}; the manager writes it")
    return declared


def connections_mode(envelope: Path | str) -> str:
    """Where this project keeps its connections and grants: `store` when its
    records are in the store or its `project.json` says `"connections":
    "store"`, `files` otherwise. Only that record decides it."""
    if records_mode(envelope) == "store":
        return "store"
    identity_file = Path(envelope) / "project.json"
    declared = _identity_record(Path(envelope)).get(CONNECTIONS_MODE_KEY, "files")
    if declared not in CONNECTIONS_MODES:
        raise StoreError("bad_connections_mode",
                         f"{identity_file} declares connections {declared!r}",
                         f"one of: {', '.join(CONNECTIONS_MODES)}; the manager writes it")
    return declared


def open_records(envelope: Path | str, global_dir: Path | str,
                 project_only: bool = False, project_root: Path | str | None = None,
                 project_id: str | None = None,
                 application_name: str = STORE_OWNER,
                 actor: str | None = None) -> Records:
    """The adapter this project's configuration is read and written through:
    the project's envelope, then `global_dir` unless `project_only`; in store
    mode a project's connections, grants, identifiers and policy come from its
    store instead (only its connections and grants where it moved only those),
    for the project id given (else project.json's), its database resolved for
    `project_root` (else the envelope's parent).

    Where a database resolves, the agent's registry row decides first: an
    agent whose `migrated_at` is set reads and writes all its records and its
    settings in the store (`AgentRecords`), whatever `project.json` says, and
    only on a node it has registered, named by AGENT_NODE_NAME. Every other
    project is read as above. Asking cannot fail a project whose records are
    files: a store that does not answer within `_PROBE_TIMEOUT` leaves it on
    its files. A project in store mode whose store does not answer keeps the
    refusal for its settings, rather than reading files that may be stale.
    `actor` names a writer that is not the person running the command:
    `migrate` or `<capability>-service`.

    A `project.json` that declares a `store` other than `files` is refused
    rather than read as files, since the records it points at are not where it
    says."""
    envelope, global_dir = Path(envelope), Path(global_dir)
    identity_file = envelope / "project.json"
    identity = _identity_record(envelope)
    declared = identity.get("store", "files")
    if declared != "files":
        raise StoreError("bad_store_mode",
                         f"{identity_file} declares store {declared!r}; records are "
                         "kept only in files",
                         'remove "store" from project.json, or set it to "files"')
    root = Path(project_root) if project_root else None
    in_store = connections_mode(envelope) == "store"
    agent_id = project_id or identity.get("id")
    if in_store:
        held = (RECORDS_MODE_KEY if records_mode(envelope) == "store"
                else CONNECTIONS_MODE_KEY)
        legacy: Records = StoreRecords(
            envelope, global_dir, agent_id, identity.get("slug"),
            include_global=not project_only, project_root=root,
            application_name=application_name, held=held)
    else:
        legacy = FileRecords(envelope, global_dir, identity.get("id"), identity.get("slug"),
                             include_global=not project_only)
    legacy._guard_agent = agent_id if isinstance(agent_id, str) and agent_id else None
    legacy._guard_root = root
    legacy._guard_application = application_name
    legacy.actor = actor
    if legacy._guard_agent is None:
        return legacy
    try:
        conn, schema = _store_connect(root or envelope.parent, application_name,
                                      None if in_store else _PROBE_TIMEOUT)
    except StoreError as exc:
        if in_store and exc.slug != "store_not_configured":
            legacy._settings_blocked = exc
        return legacy
    import psycopg
    try:
        state = _agent_state(conn, agent_id)
        conn.rollback()
    except psycopg.Error as exc:
        try:
            conn.close()
        except Exception:
            pass
        if in_store:
            legacy._settings_blocked = StoreError(
                "store_unreachable", f"cannot read the agent registry: {exc}",
                "check the database setting with `capabilities store doctor`")
        return legacy
    conn.close()
    if not state or not state.get("migrated_at"):
        if in_store:
            legacy._schema = schema
        return legacy
    node = os.environ.get(NODE_NAME_ENV, "").strip()
    if not node or node not in state["nodes"]:
        if not node:
            raise StoreError(
                "node_unnamed",
                f"agent {agent_id} keeps its records in the store, read per node, and "
                f"this process names no node: {NODE_NAME_ENV} is not set",
                _master_hint(state))
        raise StoreError(
            "node_unregistered",
            f"agent {agent_id} keeps its records in the store, and node {node} is "
            "not one of its nodes", _master_hint(state))
    adapter = AgentRecords(envelope, global_dir, agent_id, identity.get("slug"),
                           agent=state, node=node, include_global=not project_only,
                           project_root=root, application_name=application_name,
                           actor=actor)
    adapter._guard_root = root
    adapter._guard_application = application_name
    adapter._schema = schema
    return adapter

# <<< contract: store <<<

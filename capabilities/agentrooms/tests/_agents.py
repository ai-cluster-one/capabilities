"""The agents a case writes for, registered in the store before it writes.

Every row the capability keeps names its agent, and the store holds that name to
the core's `agents`: a row for an agent the store has never registered is
refused. On a real machine the manager registers the agent before any service of
it runs; a throwaway store has nobody to do that, so a case that writes for an
agent registers it here first, once the store's tables exist.
"""

from __future__ import annotations


def register_agents(db, *agent_ids: str) -> None:
    """Register each of `agent_ids` in the `agents` of the store `db` - an open
    `Store` or a connection bound to its schema - leaving one already
    registered as it is."""
    for agent_id in agent_ids:
        db.execute("INSERT INTO agents (id, kind) VALUES (%s, 'agent') "
                   "ON CONFLICT (id) DO NOTHING", (agent_id,))


def register_nodes(db, *node_ids: str) -> None:
    """Register each of `node_ids` in the store's `nodes`, as the manager
    registers the machine a service runs on, leaving one already registered as
    it is."""
    for node_id in node_ids:
        db.execute("INSERT INTO nodes (id, label, kind) VALUES (%s, %s, 'mac') "
                   "ON CONFLICT (id) DO NOTHING", (node_id, node_id))

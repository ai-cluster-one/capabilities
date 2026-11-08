#!/usr/bin/env python3
"""The agents a case writes for, registered in the store before it writes.

Every row the capability keeps names its agent, and the store holds that name to
the core's `agents`: a row for an agent the store has never registered is
refused. On a real machine the manager registers the agent before any service of
it runs; a throwaway store has nobody to do that, so a case that writes for an
agent registers it here first, once the store's tables exist.
"""

from __future__ import annotations


def register_agents(conn, *agent_ids: str) -> None:
    """Register each of `agent_ids` in the `agents` of the store `conn` is bound
    to, leaving one already registered as it is."""
    with conn.transaction():
        for agent_id in agent_ids:
            conn.execute("INSERT INTO agents (id, kind) VALUES (%s, 'agent') "
                         "ON CONFLICT (id) DO NOTHING", (agent_id,))


def register_project(daemon) -> None:
    """Register the agent a daemon imported against a store serves, in the
    schema it is bound to, as the manager has before the daemon runs. The
    product's own open migrates the store first, so the agent has a table to be
    registered in."""
    store, register = daemon.jobs.open_register(
        daemon.PROJECT_CAPABILITIES_DIR, setting=daemon.STORE_SETTING,
        project_id=daemon.jobs.bound_project_id(daemon.PROJECT_ROOT) or None)
    try:
        register_agents(store.conn, register.project_id)
    finally:
        store.close()

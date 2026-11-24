"""A shared account entered once and granted to agents, through the platform.

Every vault entry an agent did not write itself is written by the platform, the
keypair the manager makes on first need in a file only its user reads, and every
agent opens only what it sent itself and what that key sent. These tests drive the
real manager and coolify, in two agents' folders on one machine, against the
throwaway PostgreSQL and the HTTP instance of test_connections_store: a shared
connection granted to both, changed, revoked, written into one vault by the other,
recovered after a lost key, dumped, migrated from what an older release wrote, and
an agent's own account refused to another agent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_connections_store import (  # noqa: F401
    COOLIFY,
    MANAGER,
    PROJECT_ID,
    REPO,
    _coolify,
    _error,
    _global_rows_in_force,
    _here,
    _import,
    _manager,
    _ok,
    cluster,
    instance,
    lab,
)

from capabilities_contract import vault

PEER_ID = "prj_" + "0f0e0d0c0b0a"
KEY_ENV = vault.KEY_ENV


def _install(lab, name: str) -> None:
    """The capability in the lab's registry, so the manager counts it installed."""
    home = Path(lab["env"]["CAPABILITIES_HOME"]) / name
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "capabilities" / name / "bin" / name, home / name)


def _peer(lab) -> dict:
    """A second agent's folder on this machine, in store mode on the same store."""
    project = lab["project"].parent / "peer"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    envelope.mkdir()
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"coolify": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": PEER_ID, "slug": "peer"}))
    store = [line for line in (lab["project"] / ".env").read_text().splitlines()
             if line.startswith("AGENTKIT_DB_")]
    (project / ".env").write_text("\n".join(store) + "\n")
    peer = {**lab, "project": project, "envelope": envelope,
            "env": {**lab["env"], "CLAUDE_PROJECT_DIR": str(project)}}
    _ok(_manager(peer, "init"))
    _ok(_manager(peer, "connections", "import"))
    return peer


def _feed(lab, stdin: str, exe: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], cwd=lab["project"], env=lab["env"], text=True,
                          input=stdin, capture_output=True, timeout=240)


def _show(lab, name: str) -> str | None:
    shown = _manager(lab, "secrets", "show", name)
    return shown.stdout[:-1] if shown.returncode == 0 else None


def _key_text(lab) -> str:
    return next(line.split("=", 1)[1] for line in
                (lab["project"] / ".env.local").read_text().splitlines()
                if line.startswith(KEY_ENV + "="))


def _sql(lab, query: str, params: tuple = ()) -> list:
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        found = conn.execute(query, params)
        return found.fetchall() if found.description else []


def _shared_account(lab) -> tuple[dict, str]:
    """Two agents, each with its key, and a coolify account entered once on the
    machine: (the peer, the connection's id)."""
    _import(lab)
    _global_rows_in_force(lab)
    _ok(_manager(lab, "key", "generate"))
    peer = _peer(lab)
    _ok(_manager(peer, "key", "generate"))
    _install(lab, "coolify")
    _ok(_feed(lab, json.dumps({"secret_env": "shared-token-0001"}), COOLIFY, "connection",
              "add", "personal", "--global", "--field", f"base_url={lab['url']}", "--stdin"))
    [(cid,)] = _sql(lab, "SELECT id FROM connections WHERE label = 'personal'")
    return peer, cid


NAME = "COOLIFY_PERSONAL_TOKEN"


def test_a_shared_account_entered_once_reaches_both_grantees_its_change_too_and_a_revoke_one(
        lab):
    peer, cid = _shared_account(lab)
    granted = _ok(_manager(lab, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    assert granted["vault"]["sealed"] == [NAME] and granted["vault"]["pending"] == []
    granted = _ok(_manager(peer, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    assert granted["vault"]["sealed"] == [NAME]
    assert _show(lab, NAME) == _show(peer, NAME) == "shared-token-0001"
    for who in (lab, peer):
        key = next(k for k in _ok(_coolify(who, "connections"))["connections"][cid]["keys"]
                   if k["key"] == NAME)
        assert (key["tier"], key["value"]) == ("vault", "…0001")
    # The platform's file is its user's alone, and only its public key is in the store.
    held = Path(lab["config"]) / "capabilities" / "platform" / "key"
    assert held.stat().st_mode & 0o077 == 0
    assert _sql(lab, "SELECT platform_public_key FROM store_state") == [
        (vault.public_text(vault.parse_key(held.read_text().strip())),)]

    changed = _ok(_feed(lab, json.dumps({"secret_env": "shared-token-0002"}), COOLIFY,
                        "connection", "add", "personal", "--global", "--replace", "--stdin"))
    assert changed["secrets_in"] == "platform"
    assert sorted(changed["shared"]["sealed_for"]) == sorted([PROJECT_ID, PEER_ID])
    assert _show(lab, NAME) == _show(peer, NAME) == "shared-token-0002"

    revoked = _ok(_manager(peer, "set", "coolify", "grant", "personal", '{"enabled": false}'))
    assert revoked["vault"] == {"removed": [NAME]}
    assert _show(peer, NAME) is None and _show(lab, NAME) == "shared-token-0002"
    _ok(_coolify(lab, "--connection", "personal", "resources"))

    rebuilt = _ok(_manager(lab, "connections", "rebuild"))
    assert rebuilt["changed"] == 0 and rebuilt["refused"] == []
    assert rebuilt["connections"][0]["sealed"] == {PROJECT_ID: {NAME: "unchanged"}}


def test_without_the_platform_key_a_shared_value_is_refused_with_where_to_write_it(lab):
    peer, cid = _shared_account(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    (Path(lab["config"]) / "capabilities" / "platform" / "key").unlink()
    refused = _feed(lab, json.dumps({"secret_env": "shared-token-0009"}), COOLIFY,
                    "connection", "add", "personal", "--global", "--replace", "--stdin")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "platform_key_missing" and "platform key" in error["hint"]
    assert _show(lab, NAME) == "shared-token-0001"


def test_an_entry_another_agent_writes_into_a_vault_is_refused(lab):
    peer, _cid = _shared_account(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    mine = vault.parse_key(_key_text(lab))
    theirs = vault.parse_key(_key_text(peer))
    public, fp = vault.public_key(mine), vault.fingerprint(mine)
    forged = vault.seal_box(vault._public_secret(theirs), public, PROJECT_ID, NAME, "evil-0001")
    anonymous = vault.seal_for(public, PROJECT_ID, "PLANTED_TOKEN", "evil-0002")
    # The peer holds the store's login, as every agent does, and writes the rows.
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        vault.put_row(conn, PROJECT_ID, NAME, forged, fp, "peer")
        vault.put_row(conn, PROJECT_ID, "PLANTED_TOKEN", anonymous, fp, "peer")
    assert _show(lab, NAME) is None and _show(lab, "PLANTED_TOKEN") is None
    key = next(k for k in _ok(_coolify(lab, "connections"))["connections"][_cid]["keys"]
               if k["key"] == NAME)
    assert key["tier"] != "vault" or key["value"] != "…0001"
    # The platform delivers it again, and that opens.
    _ok(_manager(lab, "connections", "rebuild"))
    assert _show(lab, NAME) == "shared-token-0001"


def test_a_lost_key_is_recovered_and_every_entry_redelivered(lab):
    peer, _cid = _shared_account(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    _ok(_feed(lab, "own-secret-0001", MANAGER, "secrets", "set", "OWN_TOKEN"))
    before = _key_text(lab)
    env_local = lab["project"] / ".env.local"
    env_local.write_text("".join(line + "\n" for line in env_local.read_text().splitlines()
                                 if not line.startswith(KEY_ENV + "=")))
    assert _manager(lab, "secrets", "show", "OWN_TOKEN").returncode == 6
    recovered = _ok(_manager(lab, "key", "recover"))
    assert _key_text(lab) != before
    assert recovered["delivered"] == {"OWN_TOKEN": "created"} or \
        recovered["delivered"].get("OWN_TOKEN") in ("created", "replaced")
    assert list(recovered["connections"]) == [_cid]
    assert "unreadable" not in recovered
    assert _show(lab, "OWN_TOKEN") == "own-secret-0001"
    assert _show(lab, NAME) == "shared-token-0001"
    # The peer is not touched, and a grant to the recovered agent still works.
    assert _ok(_manager(lab, "connections", "rebuild"))["refused"] == []


def test_a_dump_of_the_database_alone_holds_no_key_and_no_value(lab):
    peer, _cid = _shared_account(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    _ok(_manager(peer, "set", "coolify", "grant", "personal", '{"enabled": true}'))
    _ok(_feed(lab, "own-secret-0001", MANAGER, "secrets", "set", "OWN_TOKEN"))
    pg_dump = shutil.which("pg_dump")
    assert pg_dump, "pg_dump is not on PATH"
    dump = subprocess.run([pg_dump, "--no-owner", "--data-only", "-h", "127.0.0.1",
                           "-p", str(lab["cluster"].port), "-U", "pgadmin", lab["database"]],
                          capture_output=True, timeout=120, check=True).stdout
    assert b"platform_secrets" in dump and b"secrets" in dump
    platform_text = (Path(lab["config"]) / "capabilities" / "platform" / "key").read_text()
    for text in (_key_text(lab), _key_text(peer), platform_text.strip()):
        assert text.encode() not in dump
    for value in (b"shared-token-0001", b"own-secret-0001"):
        assert value not in dump


def test_migration_reseals_old_vaults_loses_nothing_and_stops_where_copies_disagree(lab):
    _import(lab)
    _global_rows_in_force(lab)
    _ok(_manager(lab, "key", "generate"))
    peer = _peer(lab)
    _ok(_manager(peer, "key", "generate"))
    _install(lab, "coolify")
    _ok(_feed(lab, json.dumps({"secret_env": "seed-0000"}), COOLIFY, "connection", "add",
              "personal", "--global", "--field", f"base_url={lab['url']}", "--stdin"))
    [(cid,)] = _sql(lab, "SELECT id FROM connections WHERE label = 'personal'")
    for agent in (PROJECT_ID, PEER_ID):
        _sql(lab, "INSERT INTO grants (agent_id, capability, connection_id, enabled) "
                  "VALUES (%s, 'coolify', %s, true)", (agent, cid))
    here = _here(lab)
    _sql(lab, "INSERT INTO nodes (id, label, kind) VALUES (%s, 'lab-node', 'mac') "
              "ON CONFLICT (id) DO NOTHING", (here,))
    for agent, folder in ((PROJECT_ID, lab["project"]), (PEER_ID, peer["project"])):
        _sql(lab, "INSERT INTO agent_nodes (agent_id, node, folder) VALUES (%s, %s, %s) "
                  "ON CONFLICT DO NOTHING", (agent, here, str(folder)))
    mine, theirs = vault.parse_key(_key_text(lab)), vault.parse_key(_key_text(peer))
    # What an older release wrote: each agent's own under its key (format 1), and the
    # shared account for each grantee's public key (format 2) - here the two copies
    # disagree, as copies written by hand do.
    for agent, key, own, shared in ((PROJECT_ID, mine, "own-a-0001", "old-shared-value-aaaa"),
                                    (PEER_ID, theirs, "own-b-0001", "new-shared-value-bbbb")):
        fp = vault.fingerprint(key)
        with lab["cluster"].admin(lab["database"]) as conn:
            conn.execute("SET search_path TO lab")
            vault.put_row(conn, agent, "OWN_TOKEN",
                          vault.seal_symmetric(key, agent, "OWN_TOKEN", own), fp)
            vault.put_row(conn, agent, NAME,
                          vault.seal_for(vault.public_key(key), agent, NAME, shared), fp)
    found = _ok(_manager(lab, "secrets", "migrate"))
    assert sorted(a["agent"] for a in found["agents"]) == sorted([PROJECT_ID, PEER_ID])
    assert all(sorted(a["resealed"]) == [NAME, "OWN_TOKEN"] for a in found["agents"])
    assert found["platform_copies"] == []
    [stop] = found["disagreeing"]
    assert (stop["connection"], stop["name"]) == (cid, NAME)
    assert sorted(c["ends"] for c in stop["copies"]) == ["…aaaa", "…bbbb"]
    assert "old-shared" not in json.dumps(found) and "new-shared" not in json.dumps(found)
    assert found["hint"] and "--pick" in found["hint"]
    # Nothing is chosen: each vault keeps its own copy, sealed the new way.
    assert _show(lab, NAME) == "old-shared-value-aaaa" and _show(peer, NAME) == "new-shared-value-bbbb"
    assert _show(lab, "OWN_TOKEN") == "own-a-0001" and _show(peer, "OWN_TOKEN") == "own-b-0001"
    assert {bytes(c)[0] for (c,) in _sql(lab, "SELECT ciphertext FROM secrets")} == {
        vault.SEALED_FORMAT_BOX}
    picked = _ok(_manager(lab, "secrets", "migrate", "--pick", f"{cid}:{NAME}={PEER_ID}"))
    assert [(c["connection"], c["name"]) for c in picked["platform_copies"]] == [(cid, NAME)]
    assert picked["disagreeing"] == [] and "picks_unused" not in picked
    assert _show(lab, NAME) == _show(peer, NAME) == "new-shared-value-bbbb"
    assert _show(lab, "OWN_TOKEN") == "own-a-0001" and _show(peer, "OWN_TOKEN") == "own-b-0001"
    again = _ok(_manager(lab, "secrets", "migrate"))
    assert again["disagreeing"] == [] and again["platform_copies"] == []
    assert all(a["resealed"] == [] for a in again["agents"])


def test_where_the_platform_key_is_elsewhere_an_agent_migrates_its_own_part(lab):
    """A node: the agent's key lives in its environment, and the platform key the
    store publishes is held on another machine."""
    _import(lab)
    _ok(_manager(lab, "key", "generate"))
    text = _key_text(lab)
    env_local = lab["project"] / ".env.local"
    env_local.write_text("".join(line + "\n" for line in env_local.read_text().splitlines()
                                 if not line.startswith(KEY_ENV + "=")))
    node = {**lab, "env": {**lab["env"], KEY_ENV: text}}
    key = vault.parse_key(text)
    elsewhere = vault.parse_key(vault.generate_key())
    _sql(lab, "UPDATE store_state SET platform_public_key = %s", (vault.public_text(elsewhere),))
    fp = vault.fingerprint(key)
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        vault.put_row(conn, PROJECT_ID, "OWN_TOKEN",
                      vault.seal_symmetric(key, PROJECT_ID, "OWN_TOKEN", "own-node-0001"), fp)
        vault.put_row(conn, PROJECT_ID, "GRANTED_TOKEN", vault.seal_for(
            vault.public_key(key), PROJECT_ID, "GRANTED_TOKEN", "granted-node-0001"), fp)
    found = _ok(_manager(node, "secrets", "migrate"))
    assert found["platform"] == "elsewhere" and found["not_here"] == []
    [agent] = found["agents"]
    assert sorted(agent["resealed"]) == ["GRANTED_TOKEN", "OWN_TOKEN"]
    assert _show(node, "OWN_TOKEN") == "own-node-0001"
    assert _show(node, "GRANTED_TOKEN") == "granted-node-0001"
    # The platform, wherever it is, opens the copies the agent sent it.
    copies = dict(_sql(lab, "SELECT name, ciphertext FROM platform_secrets "
                            "WHERE agent_id = %s", (PROJECT_ID,)))
    scope = vault.copy_scope(agent_id=PROJECT_ID)
    assert {name: vault.open_box(vault._public_secret(elsewhere), scope, name, bytes(blob),
                                 [vault.public_key(key)])
            for name, blob in copies.items()} == {"OWN_TOKEN": "own-node-0001",
                                                  "GRANTED_TOKEN": "granted-node-0001"}
    assert not (Path(lab["config"]) / "capabilities" / "platform" / "key").exists()


def test_an_agents_own_account_is_never_granted_to_another_agent(lab):
    _import(lab)
    _install(lab, "telegrambot")
    for cid, owner in (("a1a1a1a1a1a1", None), ("b2b2b2b2b2b2", PROJECT_ID)):
        _sql(lab, "INSERT INTO connections (id, capability, label, spec, owner_agent_id) "
                  "VALUES (%s, 'telegrambot', %s, '{}', %s)", (cid, "bot-" + cid[:2], owner))
    refused = _manager(lab, "set", "telegrambot", "grant", "a1a1a1a1a1a1", '{"enabled": true}')
    assert refused.returncode == 6 and _error(refused)["code"] == "connection_own_kind"
    assert _sql(lab, "SELECT count(*) FROM grants WHERE connection_id = 'a1a1a1a1a1a1'") == [
        (0,)]
    _ok(_manager(lab, "set", "telegrambot", "grant", "b2b2b2b2b2b2", '{"enabled": true}'))

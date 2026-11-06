# Upgrading a store to a new schema major

Use this guide to move a store to a new schema major: what decides that one is due, the order of steps across machines and nodes, the dump the upgrade takes and where it is kept, how an interrupted upgrade goes on, how each kind of node is brought up afterwards, the release lines that keep every machine working until then, and what a machine that has not caught up sees.

`capabilities help` (STORE VERBS, `store upgrade`) is the contract for the verb and its answers. This guide is about going through it.

## What a schema major is

Each owner of tables in a store - the store's core, the manager's own tables, each capability that keeps tables - records its schema version as a major and a minor in the store's `schema_version` table. The store's major is the major of its `core` owner, or 1 for a store whose owners were migrated before the core was.

A minor change adds and breaks nothing, so it applies by itself: the next `update`, service start or first call of a capability migrates it, with no one asked.

A major change can rename or drop what older code reads, so it never applies by itself. Every migration outside the upgrade refuses it with `store_upgrade_needed`, naming `capabilities store upgrade`, and a release that reads a newer major than the store's is not taken by `self-update` or `update`. Only `capabilities store upgrade`, started by a person, crosses it.

## The release lines

`main` of the canonical source always carries the newest major. When a newer major lands there, the last commit of the older one is kept as the branch `store-major-<n>`, which takes only fixes and what that major needs to reach the upgrade.

`self-update` and `update` take the release that reads the store's major: from `main` while the store is on the newest major, else from the store's major's line. Their answers carry `store_major`, and say `upgrade_needed` with the command `capabilities store upgrade` while a newer major exists. A store that holds no schema yet takes the newest release. A source other than the canonical one has no lines: a release of another major from it is not taken, and what is installed is kept.

So publishing a new major moves no store and breaks no machine: every machine and node keeps running releases of the major its store is on until someone upgrades that store.

## The order of steps

1. Check, on the machine that will run the upgrade: `capabilities store upgrade --check`. It changes nothing. It answers the store's major and each owner's against what the installed releases read, whether the upgrade is needed and whether it can run, each thing that stops it with what to do, and every node the store holds with what it runs.
2. Clear each blocker the check names, and check again until `can_run` is true. A blocker is an owner whose tables are in the store with no release here to migrate them, `pg_dump` or `pg_restore` missing or older than the server, too little room for the dump, the read-only switch, or a store that does not answer. The check looks for `pg_dump` and `pg_restore` of the server's major or newer on PATH and where Homebrew and Postgres.app install them, so a newer client installed beside an older one on PATH is found; its `tools` names the ones the upgrade uses, and a blocker about them names what to install.
3. Upgrade, on that one machine only: `capabilities store upgrade`, or `capabilities store upgrade --events` for a line per phase. It takes the newest line's manager and hands over to it, updates every installed capability from the newest line, takes the dump and proves it, then migrates every owner. Its answer lists the nodes left to bring up.
4. Bring up every other machine that uses the store: `capabilities self-update`, then `capabilities update`. They now find the store on the new major and take the newest releases.
5. Bring up every node under `nodes_to_bring_up`, each with the command beside it, as described below. `capabilities node state` reads a node that cannot read the store yet as unreachable, with `cannot_read_store` saying why and what to run.

One upgrade is enough for everything that shares the store: whoever runs it moves the store for every machine, folder and node that uses it. A folder whose own `.env.local` names another store has its own store and its own upgrade.

## The dump

Before anything is migrated the upgrade takes a dump of the store's schema with `pg_dump`, in its custom format, to `$XDG_DATA_HOME/capabilities/backups/<store key>/<UTC time>.dump` (`~/.local/share/capabilities/backups` when `XDG_DATA_HOME` is unset). The store key names the store's database and schema with a digest of its address, so every store keeps its dumps apart. The check's `backup` says where it goes and how much room it needs against what is free.

The dump is proven before the upgrade goes on: `pg_restore --list` reads the archive whole, and its table of contents holds the data of every table the schema held when it was taken. The answer's `backup.proven` says what was proven. A dump that does not prove stops the upgrade before anything moves.

To return a store to the moment before the upgrade, stop everything that writes to it, rename the moved schema aside, restore the dump with `pg_restore --dbname=<database> <file>`, which makes the schema again as it was, and run the releases of the older major against it. The dump stays where it is until someone removes it.

## When it is interrupted

The upgrade keeps its progress per store in `$XDG_STATE_HOME/capabilities/store-upgrade/<store key>.json`. Run the same command again and it goes on: each phase and each owner already done is skipped, a proven dump is kept and reused rather than taken again, and the answer says `resumed`. A failed phase ends with its error and the progress as the error's detail; fix what it names and run the command again. Only one upgrade of a store runs at a time.

## The nodes

A node runs the releases it last took, so after the upgrade it keeps the older major until it takes the new one. Where the store's core moved, its manager cannot read the store at all: it says so in its log, writes no state, and a request asked of it through the store is never taken up, so `node update --node` refuses it rather than leaving a request no one reads. The check's `nodes` and the upgrade's `nodes_to_bring_up` name each node with its command.

- A node on the standard image is brought up by a restart: its boot self-updates the manager and updates every capability. `coolify restart <resource> --type service` restarts it; no redeploy is needed.
- A node on an image whose entrypoint predates the boot self-update (images built before 2026-10-25) does not take a new manager at boot. Move it to the current tag with `capabilities node update <node> --version <tag>`; `capabilities node image` names the tag.
- A custom image built `FROM` the standard image behaves as the standard image while it keeps the standard entrypoint, so a restart is enough. One that pins `CAPABILITIES_REF` installs the capabilities of that ref and stays on the older major until it is rebuilt against a ref of the new major. One with an entrypoint of its own takes the new major only if that entrypoint runs `capabilities self-update` and `capabilities update` at boot; otherwise run them in the node, with `capabilities node exec <node> -- capabilities self-update` and then `update`, and restart it.
- A node built on its server from a repository is rebuilt: `coolify deploy <resource>`.
- A machine that is not a Coolify resource - another Mac, a server run by hand - runs `capabilities self-update` and `capabilities update` itself, and restarts its node manager (`capabilities node run`).

A node that is off, or superseded, can be disabled with `capabilities node disable <node>`; a disabled node is not listed.

## What a machine that has not caught up sees

A machine or node still on the older major's releases reads its line and runs as before until its store moves. Once the store is on the newer major, its migrations refuse with `schema_too_new`, whose hint names `capabilities self-update`, then `capabilities update`, and the manager keeps that hint wherever it reports the refusal. A table the new major renamed or dropped may be left as a stub under its old name that holds no data and whose every read fails with a message saying what to run, so older code fails with that message rather than a missing table. Its `self-update` takes the newest release, since the store is on the newest major.

`capabilities store upgrade --check` on such a machine answers `state: behind`, and `store upgrade` changes nothing there: it answers the same, with `next` naming `capabilities self-update` and `capabilities update`.

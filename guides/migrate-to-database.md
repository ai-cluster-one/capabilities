# Moving agents from files to the database

Use this guide to walk a person through moving their agents' configuration from files into their own PostgreSQL database, on their own machines, after the manager has been updated. It is written for an agent carrying the move out: every step names the command to run and what its answer means, and every choice that belongs to the person is marked **Ask the person**.

`capabilities help` is the contract for every verb named here; read the `migrate`, `store` and `node` sections there whenever an answer below does not match what you see.

## What the person decides, and what you do

You run the commands, read their JSON and fix what is mechanical. The person decides, and you ask them, about:

- credentials and access: which database, its password, who may reach it;
- which machine is each agent's **master** - the machine that migrates the agent and writes its shared configuration, as does any node later let write with `capabilities node set <node> --may-write`; the first `migrate --apply` for an agent makes the machine it runs on the master;
- the name of each machine (its node name);
- every dry-run question whose answer changes what the agent does or where it runs;
- when each agent moves - the move is per agent and can be spread over days.

An agent that has not moved keeps working from its files exactly as before. Nothing moves on its own: updating installs code and migrates nothing.

## 1. Prerequisites

### Updated tools

On every machine that runs agents, update the manager and the capabilities, and the desktop app that launches agent sessions where one is installed:

```sh
capabilities self-update
capabilities update
capabilities self-update --check   # update_available false
capabilities update --check        # update_available false: every payload and contract library current
```

A migration refuses with `payload_outdated` while any installed capability is older than the manager's contract; `capabilities update` is the fix.

### Quarantined capabilities

A capability new to a machine - one an update brought in, say - arrives quarantined, and a quarantined capability is disabled in every project whatever its gate says. `capabilities list` shows each one's machine state. Lifting a quarantine is the person's decision: **ask the person** about each quarantined capability the agents use, and run `capabilities allow <name>` only for those they allow.

### A database

```sh
capabilities store show     # which level is in force: project, environment or machine
capabilities store doctor   # exit 0 when the database works for this project
```

`store doctor` exits 3 `no_store_setting` when nothing is configured. Then the person needs a PostgreSQL database. **Ask the person** whether they have one or want to create one with a hosted PostgreSQL service; for a hosted one, walk them through creating a project there and finding, on its connection page, the host, port, database name, user and password.

Choose the address:

- A direct PostgreSQL server needs only its one address.
- A hosted service that offers a session-mode pooler and a transaction-mode pooler: use the **session pooler** address as the main address. A known transaction pooler as the main address is refused (exit 6 `transaction_pooler`).

Write the machine's setting. The password never goes on the command line:

```sh
capabilities store set --host <host> --port <port> --database <database> --user <user> --sslmode require --password-stdin
capabilities store doctor
```

`--sslmode` defaults to `require` (`verify-full` with `--sslrootcert`). A local server without TLS - on `localhost`, a loopback address or a Unix socket - needs `--sslmode disable`; `disable` is refused for any other host.

`--password-file PATH` and `--password-env KEY` are the other ways in. If the person pasted a password into the conversation, set it through `--password-stdin`, then ask them to rotate it at the database provider and set the new one with the same command.

Secrets stay where they are: each capability's `credentials.env` in the config home and the agents' own environment files hold the keys connections name, and the migration neither moves them into the database nor removes them; `migrate --report` does not count them among `config_home.files`.

Behind a session pooler, the store can also be given its transaction address, so that short commands do not hold session-pooler connections: `capabilities store set ... --transaction-host <host> --transaction-port <port>` on a machine, or `AGENTKIT_DB_TRANSACTION_HOST` and `AGENTKIT_DB_TRANSACTION_PORT` beside the other `AGENTKIT_DB_*` keys in a server's environment. Set it only once every tool on that machine is at a manager and contract release that reads these fields: `capabilities changelog --manager --all` carries the entry "A store may name a transaction address", and `capabilities update --check` shows nothing behind. `store doctor` then proves it under `checks.transaction`.

A store reached only through a transaction pooler other than Supabase's - a self-hosted connection pooler in transaction mode, or another provider's transaction-mode pool - is declared one, so that nothing opens a session through it: `capabilities store set ... --transaction-pooler` on a machine, with the pooler as `--host` and `--port`, or `AGENTKIT_DB_TRANSACTION_POOLER=true` beside the other `AGENTKIT_DB_*` keys in a server's environment. Set it only once every tool on that machine is at a manager and contract release that reads it: `capabilities changelog --manager --all` carries the entry "A store setting may declare its one address a transaction pooler", and `capabilities update --check` shows nothing behind. Until it is updated, a tool still on an earlier release refuses a machine's setting that carries it, and ignores the key in a server's environment. A process keeps the release it started with, so a node manager, supervisor or service started before the update reads the setting as the earlier release does until it is restarted; `store set --transaction-pooler` refuses as `store_readers_behind`, naming each such process, until none is left. `store doctor` then shows `setting.addresses.session` null and says under `checks.session` that the store has no session address.

### A backup, before any `--apply`

Back up the database before the first `--apply` of anything. Use a `pg_dump` whose major version is at least the server's: `store doctor` reports the server's version under `checks.tls.server_version`, and `pg_dump --version` reports the tool's. Connect `pg_dump` to the direct or session address, not to a transaction pooler, and keep the dump where the person keeps backups. **Ask the person** where that is.

### Each machine's node id

Every machine resolves its own node id, twelve lowercase hex characters, with nothing set: a Mac derives it from its hardware, and any other machine keeps the one minted for it in `~/.config/capabilities/node-id` (a server node's is written there at enrolment). `capabilities node id` prints it, with where it came from. A moved agent's records are read per node by that id, so nothing has to reach a shell, a login session or a service manager for it, and a process started without the shell profile resolves the same node. `AGENT_NODE_ID` overrides it where a container is handed its id or a test runs as a given node; a box `deployment sync` compiles for a target is handed its target's `node` there, which for a node registered before ids is still its name. `AGENT_NODE_NAME`, where set, is only the label a machine gives itself when it is first registered; a node's label is its row's (`capabilities node set <id> --label <label>`).

A machine registered before node ids is known by its name. Move it to its id once, on that machine, with its agents' services stopped: `capabilities node rekey <name>` (`--dry-run` first lists every row it moves).

## 2. The order

1. The global layer, once per database.
2. Each live server's deployment target gets its node.
3. Each agent, on its master: dry run, questions, apply, verify, commit.
4. Restart what predates the move.
5. Other machines: register them and discard their leftover files.
6. Check that nothing remains.

### Step 1 - the global layer

The machine's own configuration (its gate, identifiers, connections, grants and global settings) becomes the global rows every agent reading that database inherits. Run it on the machine that will be the master:

```sh
capabilities migrate --global            # dry run: plan, gate_flips, questions
capabilities migrate --global --apply    # sets global_migrated_at
```

Read `gate_flips` with the person: each entry is an agent whose effective gate would change. Once `global_migrated_at` is set, running it again changes nothing.

`migrate --global` works against the database in force where it runs. An agent that keeps its own database in its project (`store show` answers `in_force: project` in that agent's root) needs its own global layer: run `migrate --global` from that agent's root, so it writes to that database. Every agent's migration refuses with `global_not_migrated` until its database has the global layer.

### Step 2 - servers: give each live target its node

Do this before the agents' dry runs; otherwise the dry run asks no node question for that server and the server is left out.

The dry run reads targets only where the agent has a `deployment/runtime.json`. Every target that declares a `node` gives a node question (`nodes.<node>`) until that node is registered. A target with no `node` gives a question (`deployment.targets.<target>`) only when the runtime's profile runs in a container; under the `host-agents` profile the services are placed on the master and no target is asked about, whatever its provider. An agent whose gate is already in the store gets no target question either.

For each agent that deploys to a server, and for each of its deployment targets **that has a live deployment**:

1. Add `"node": "<server-node>"` to `deployment/targets/<target>/target.json` (or `deployment/targets/<target>.json` where the declaration has not moved yet). **Ask the person** for the name, or propose one and confirm it. It is the server's label; the migration registers the server under it, and the server moves to its own id with `capabilities node rekey <server-node>` run there.
2. Remove any `AGENT_NODE_NAME` from `services.<name>.environment_defaults` in `deployment/runtime.json`; the target is its one home, and `deployment sync` refuses the duplicate with `node_declared_twice`.
3. Run `deployment sync` and commit the result.
4. Redeploy the server (`deployment deploy --target <target>` where that is how it deploys) so it runs the updated code before its agent moves.

A target with no live deployment gets no node: **ask the person** whether it is dead, and remove it rather than registering a node for it. `deployment node status --target <target>` reports `declared_node_name` beside the `node_name` the server gives itself, with `node_name_mismatch` while they differ. `deployment guide targets` ("The node a target's server runs as") is the reference.

### Step 3 - each agent

In the agent's root, on its master, one agent at a time:

```sh
capabilities migrate                                  # dry run, writes nothing
capabilities migrate --answers answers.json           # dry run with answers
capabilities migrate --apply --answers answers.json   # the move
```

Read the dry run's `blockers` (each with `code`, `message`, `hint`), `questions` and `plan`. Fix every blocker first. `--apply` writes nothing while a blocker or an open question remains.

The answers file is JSON. Each question carries an `answer` field: a dotted path, and the answer goes at that path. A question with `"answer": "nodes.<server-node>"` is answered by:

```json
{"nodes": {"<server-node>": {"register": true, "address": "<host or IP>"}}}
```

Answers are inputs the run records; keep the file beside the person's other notes for the agent, not in the agent's repository.

A successful apply answers `state: "migrated"`, `applied: true`, `migrated_at`, `verify.differences` (empty), `removed` (the files it deleted) and `archive` (where it kept a copy of every file git does not hold). Running `--apply` again changes nothing.

Then commit **only the files the migration removed** (the `removed` list) and, where the project still has one, the `capabilities/project.json` change that records `"records": "store"`. Other uncommitted changes in the repository are not the migration's; leave them for the person.

### Step 4 - restart what predates the move

A process started before the move still reads the agent's records as it found them. After the agents on a machine have moved, restart every long-running process that predates it: machine services, the desktop app that launches agent sessions, open terminals and agent harness sessions.

On a cold start with many agents, a machine service may need one restart cycle before every agent it serves answers; check each with `<cap> service doctor --machine` for the capabilities that run a machine service.

### Step 5 - other machines

A second machine that runs the same agent is a node of it: read its id there with `capabilities node id`, then on the master `capabilities node add <id> --label <label> --kind mac` (or `--kind server --address <host> --target <target>`); `capabilities agent materialize` run on that machine registers it the same way. `capabilities node list` shows the agent's nodes and its master.

On a machine that is no agent's master, once every agent there has moved, delete the machine's own leftover record and settings files:

```sh
capabilities migrate --discard
```

It refuses on the master node of any agent (`master_node`), before the global layer exists (`global_not_migrated`) and while any agent root it finds has not moved (`agents_unmigrated`, each root named).

On the global master, which keeps these files, and on any other machine that would rather keep a copy, retire them once the store holds what they say:

```sh
capabilities migrate --retire-connections   # the connections files
capabilities migrate --retire-records       # the gate, identifiers and global settings files
```

Each compares every value of its files with the store's global rows and, only when every one is there with the same value, moves the files to one dated backup under `$XDG_CONFIG_HOME/capabilities/retired/`. Otherwise it moves nothing and names each value that is missing or differs (`connections_not_in_store`, `records_not_in_store`); write it at global scope with its own verb, or remove it from its file, and run it again. `--retire-records` also refuses while any agent root it finds has not moved (`agents_unmigrated`), as `--discard` does. Credentials, env and session files stay where they are.

### Step 6 - done

```sh
capabilities migrate --report
capabilities migrate --report --root <path>   # for agent roots it does not find by itself
```

The report lists every agent root the machine finds - from the machine services' join lists, the desktop app's agent list, the project you run it in, the roots you give and their git worktrees - with its `state`, the record and settings `files` left, an open migration's `journal`, `consumers` (lines of the agent's own text that read those files directly) and a `reason`, and the machine's `config_home` files. On the master, roots that none of those sources know about need `--root`.

The move is done on a machine when every agent in its report is `migrated` with no `files`, no `consumers` and no `reason`, and `config_home.files` is empty - on the global master once step 5's retire runs have moved them, elsewhere once `--discard` or those runs have. It is done for the person when that holds on every machine.

Fix `consumers` with the person: each names a script or document of theirs that still reads the old files.

## 3. The dry run's questions

Each question names what code cannot decide. Read it to the person in plain words; the answer path is in its `answer` field.

- **A node to register** (`nodes.<node>`, choices `{"register": true, "address": ...}` or `{"register": "later"}`): a deployment target runs as this node. Register it now, or later with `capabilities node add` - the migration does not finish (`target_node_unregistered`) until every target's node is registered.
- **A target that names no node** (`deployment.targets.<target>`): answer a node name, or `null` when nothing placed there needs a row. Prefer giving the target its `node` (step 2) and running the dry run again.
- **A node for a legacy automation**: which node it runs on.
- **A connection whose stored spec differs** from the agent's files: `"store"` (the agent adopts the stored spec), `"files"` (the stored spec is replaced; a connection row is shared by every agent granted it) or `{"rename": "<name>"}` (the agent's own is written under that name, and its grants, default and settings follow it). **Ask the person**: this is an access choice.
- **An id that the project's files (`.env.local`/`.env`, else an older `project.json`) and the ContextKit binding disagree on**: which id the agent keeps. It is written to `.env.local`, which is not committed.
- **Records said to be in a store that holds none of them** (`records`, choices `"files"` or `"store"`): `capabilities/project.json` says `"records": "store"`, but the database in force holds none of the agent's grants, identifiers or gate, while its files hold some - typically left by an earlier import into another database. `"files"`: the files are the truth and are written, as for an agent in files mode. `"store"`: the store is kept as it is and the files are removed. **Ask the person**; whichever side is dropped, git history or the run's `archive` keeps a copy of the files.
- **A checkout** (`checkout`): the database already holds this agent and no checkout of it was found; answer with this root's path to move the agent from here.
- **Allow-list entries or modes that two settings files disagree on**: keep or drop each; **ask the person**, as these decide who the agent answers.

## 4. When something goes wrong

- **Interrupted** (a crash, a closed terminal, `store_unreachable` mid-apply): run the same `--apply` again on the same machine. A journal in the machine's state home resumes the run where it stopped.
- **Frozen**: from the start of an apply until it finishes, only that migration writes the agent's configuration, on any node; the agent's launchd agents on the master are stopped and its services that pause are paused until it finishes. `capabilities agent show` reports `migrating_since` while this lasts. Finish it by running `--apply` again on the machine that started it; another machine is refused (`node_mismatch`).
- **Verify failed** (exit 7 `verify_failed`): the agent stays frozen. Read the differences, settle them (often a connection question answered `"store"`), and run `--apply` again.
- **Other refusals** carry a `code` and a `hint`: `node_unidentified` (this machine resolves no node id: see `capabilities node id`), `not_master` (run on the master), `question_open` (answer the named questions), `second_checkout` (this root is a second checkout of an agent; move it from the main checkout the refusal names), `payload_outdated` (run `capabilities update`).
- **A moved agent refuses with `node_unregistered`** naming its master: this machine's node id is not among the agent's nodes; register it there with `capabilities node add <id>`, or, for a machine registered before node ids under its name, move it to its id with `capabilities node rekey <name>` here.

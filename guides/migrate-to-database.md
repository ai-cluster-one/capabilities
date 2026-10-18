# Moving agents from files to the database

Use this guide to walk a person through moving their agents' configuration from files into their own PostgreSQL database, on their own machines, after the manager has been updated. It is written for an agent carrying the move out: every step names the command to run and what its answer means, and every choice that belongs to the person is marked **Ask the person**.

`capabilities help` is the contract for every verb named here; read the `migrate`, `store` and `node` sections there whenever an answer below does not match what you see.

## What the person decides, and what you do

You run the commands, read their JSON and fix what is mechanical. The person decides, and you ask them, about:

- credentials and access: which database, its password, who may reach it;
- which machine is each agent's **master** - the one machine that writes the agent's shared configuration; the first `migrate --apply` for an agent makes the machine it runs on the master;
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
capabilities update --check        # update_available false
```

A migration refuses with `payload_outdated` while any installed capability is older than the manager's contract; `capabilities update` is the fix.

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
capabilities store set --host <host> --port <port> --database <database> --user <user> --password-stdin
capabilities store doctor
```

`--password-file PATH` and `--password-env KEY` are the other ways in. If the person pasted a password into the conversation, set it through `--password-stdin`, then ask them to rotate it at the database provider and set the new one with the same command.

Behind a session pooler, the store can also be given its transaction address, so that short commands do not hold session-pooler connections: `capabilities store set ... --transaction-host <host> --transaction-port <port>` on a machine, or `AGENTKIT_DB_TRANSACTION_HOST` and `AGENTKIT_DB_TRANSACTION_PORT` beside the other `AGENTKIT_DB_*` keys in a server's environment. Set it only once every tool on that machine is at a manager and contract release that reads these fields: `capabilities changelog --manager --all` carries the entry "A store may name a transaction address", and `capabilities update --check` shows nothing behind. `store doctor` then proves it under `checks.transaction`.

### A backup, before any `--apply`

Back up the database before the first `--apply` of anything. Use a `pg_dump` whose major version is at least the server's: `store doctor` reports the server's version under `checks.tls.server_version`, and `pg_dump --version` reports the tool's. Connect `pg_dump` to the direct or session address, not to a transaction pooler, and keep the dump where the person keeps backups. **Ask the person** where that is.

### A name for each machine

Every machine names itself with `AGENT_NODE_NAME`. A moved agent refuses to work in a process that has no node name (`node_unnamed`), so the name must reach every process that runs agents or their tools - interactive shells **and** the machine's service manager, so that services and apps it starts get it too:

- shells: export it in the shell profile;
- macOS: `launchctl setenv AGENT_NODE_NAME <node>` for the running login session, and the same command at every login (for example from a login item) so it survives a restart; check with `launchctl getenv AGENT_NODE_NAME`;
- Linux with systemd: an `Environment=AGENT_NODE_NAME=<node>` line in the units that run agents, or the user manager's environment; check with `systemctl show-environment` or the unit's own environment.

**Ask the person** for the name; it takes letters, digits, `.`, `_` and `-`. Set it before migrating. A new terminal should print it with `echo $AGENT_NODE_NAME`.

Servers get their name from their deployment target, below; nothing is set by hand on a server.

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

For each agent that deploys to a server, and for each of its deployment targets **that has a live deployment**:

1. Add `"node": "<server-node>"` to `deployment/targets/<target>/target.json` (or `deployment/targets/<target>.json` where the declaration has not moved yet). **Ask the person** for the name, or propose one and confirm it.
2. Remove any `AGENT_NODE_NAME` from `services.<name>.environment_defaults` in `deployment/runtime.json`; the target is its one home, and `deployment sync` refuses the duplicate with `node_declared_twice`.
3. Run `deployment sync` and commit the result.
4. Redeploy the server (`deployment deploy --target <target>` where that is how it deploys) so it runs the updated code before its agent moves.

A target with no live deployment gets no node: **ask the person** whether it is dead, and remove it rather than registering a node for it. `deployment node status --target <target>` reports `declared_node_name` beside the `node_name` the server answers to, with `node_name_mismatch` while they differ. `deployment guide targets` ("The node a target's server runs as") is the reference.

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

Then commit **only the files the migration removed** (the `removed` list) and the `capabilities/project.json` change that records `"records": "store"`. Other uncommitted changes in the repository are not the migration's; leave them for the person.

### Step 4 - restart what predates the move

A process started before `AGENT_NODE_NAME` was set does not have it, and a moved agent refuses to work there with `node_unnamed`. After the agents on a machine have moved, restart every long-running process that predates the variable: machine services, the desktop app that launches agent sessions, open terminals and agent harness sessions.

On a cold start with many agents, a machine service may need one restart cycle before every agent it serves answers; check each with `<cap> service doctor --machine` for the capabilities that run a machine service.

### Step 5 - other machines

A second machine that runs the same agent is a node of it: on the master, `capabilities node add <node> --kind mac` (or `--kind server --address <host> --target <target>`), then set `AGENT_NODE_NAME` to that name on the machine. `capabilities node list` shows the agent's nodes and its master.

On a machine that is no agent's master, once every agent there has moved, delete the machine's own leftover record and settings files:

```sh
capabilities migrate --discard
```

It refuses on the master node of any agent (`master_node`), before the global layer exists (`global_not_migrated`) and while any agent root it finds has not moved (`agents_unmigrated`, each root named).

### Step 6 - done

```sh
capabilities migrate --report
capabilities migrate --report --root <path>   # for agent roots it does not find by itself
```

The report lists every agent root the machine finds - from the machine services' join lists, the desktop app's agent list, the project you run it in, the roots you give and their git worktrees - with its `state`, the record and settings `files` left, an open migration's `journal`, `consumers` (lines of the agent's own text that read those files directly) and a `reason`, and the machine's `config_home` files. On the master, roots that none of those sources know about need `--root`.

The move is done on a machine when every agent in its report is `migrated` with no `files`, no `consumers` and no `reason`, and `config_home.files` is empty. It is done for the person when that holds on every machine.

Fix `consumers` with the person: each names a script or document of theirs that still reads the old files.

## 3. The dry run's questions

Each question names what code cannot decide. Read it to the person in plain words; the answer path is in its `answer` field.

- **A node to register** (`nodes.<node>`, choices `{"register": true, "address": ...}` or `{"register": "later"}`): a deployment target runs as this node. Register it now, or later with `capabilities node add` - the migration does not finish (`target_node_unregistered`) until every target's node is registered.
- **A target that names no node** (`deployment.targets.<target>`): answer a node name, or `null` when nothing placed there needs a row. Prefer giving the target its `node` (step 2) and running the dry run again.
- **A node for a legacy automation**: which node it runs on.
- **A connection whose stored spec differs** from the agent's files: `"store"` (the agent adopts the stored spec), `"files"` (the stored spec is replaced; a connection row is shared by every agent granted it) or `{"rename": "<name>"}` (the agent's own is written under that name, and its grants, default and settings follow it). **Ask the person**: this is an access choice.
- **An id that project.json and the ContextKit binding disagree on**: which id the agent keeps.
- **A checkout** (`checkout`): the database already holds this agent and no checkout of it was found; answer with this root's path to move the agent from here.
- **Allow-list entries or modes that two settings files disagree on**: keep or drop each; **ask the person**, as these decide who the agent answers.

## 4. When something goes wrong

- **Interrupted** (a crash, a closed terminal, `store_unreachable` mid-apply): run the same `--apply` again on the same machine. A journal in the machine's state home resumes the run where it stopped.
- **Frozen**: from the start of an apply until it finishes, only that migration writes the agent's configuration, on any node; the agent's launchd agents on the master are stopped and its services that pause are paused until it finishes. `capabilities agent show` reports `migrating_since` while this lasts. Finish it by running `--apply` again on the machine that started it; another machine is refused (`node_mismatch`).
- **Verify failed** (exit 7 `verify_failed`): the agent stays frozen. Read the differences, settle them (often a connection question answered `"store"`), and run `--apply` again.
- **Other refusals** carry a `code` and a `hint`: `node_unnamed` (set `AGENT_NODE_NAME`), `not_master` (run on the master), `question_open` (answer the named questions), `second_checkout` (this root is a second checkout of an agent; move it from the main checkout the refusal names), `payload_outdated` (run `capabilities update`).
- **A moved agent refuses with `node_unnamed`** in a terminal, service or app: that process predates `AGENT_NODE_NAME`; restart it (step 4). Refused naming its master: this machine is not registered among the agent's nodes; register it there with `capabilities node add`.

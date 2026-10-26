# Agent Nodes From The Standard Image

Use this guide to run an agent that lives in the store on a server of its own: create a node from the public standard image, enrol it, log in on it, move services onto it, watch it, update it and remove it. It is the whole procedure, in order; every command's exact options are `deployment help`'s, `capabilities help`'s and `coolify help`'s, and are not repeated here.

## What a node is

`agent-node` is one agent in one container. The container runs the public standard image `ghcr.io/agenium-dev/agent-node:<version>`, which carries the tools and the boot path - Python and uv, Claude Code and Codex, the capabilities manager with the whole catalogue, the supervisor - and no agent and no secret. Nothing is built per agent and no repository is cloned: the container is told only which agent it runs (`CAPABILITIES_PROJECT_ID`) and the folder it raises it in (`CAPABILITIES_PROJECT_ID_ROOT`, `/app`). Its identity is not in its environment: its label is its row's in the store, and it waits idle until enrolment puts its node id on its config volume and hands it a store to reach. It then raises the agent from the store and runs the node's service manager (`capabilities node run`), which runs a service only where the agent's gate places it on this node.

A node raised from the store holds no checkout of the agent's repository at `/app`, so nothing a service runs may name a file of that repository. A script a service runs is kept in the store instead: a tasks worker's hooks as stored hooks (`tasks guide workers`, Hooks), an automation as a stored body (`automations guide authoring`).

A node is also a row among the agent's nodes (`capabilities node list`): its id, the key every store row naming the node holds, its label, and for a node made here the coolify connection, the Coolify server and the Coolify resource it was created through, and the public half of the key pair it made for itself.

`agent-node-multi` - several agents of one owner in one container, as on a Mac, with a folder per agent and machine services serving all of them - is planned and not shipped. Everything below is `agent-node`.

## Before you start

Work from the folder of the agent on the machine you deploy from: a folder bound to the agent's id (`capabilities init --id <id>`, or `capabilities agent materialize`), whose records are in the store. A project whose gate is in its files gets `not_store_mode`.

You need:

- a coolify connection granted to this agent that allows writes (`coolify help` for connections and their write permission); with more than one granted, name it with `--connection`;
- in Coolify, an existing project and environment and the server the node runs on: their uuids and the environment's name. `deployment` creates no project and no environment;
- SSH as root from this machine to that server's address as Coolify reports it, with the key the connection names or the one your SSH configuration offers for that host. Enrolment, and everything you do inside the node, runs over it with `docker exec`;
- a store the server can reach. The node is given the store setting this folder resolves, so a setting that points at `localhost`, or names a root certificate file (`store_setting_not_portable`), does not travel.

## Create

`deployment node create <node>`, with the Coolify project, server and environment, the image tag (`--version`) and optionally a memory limit, makes one Coolify service of one container from the standard image. It has a volume of its own for each state path of the image - `~/.config` (store setting, secrets, node key), `~/.local/state` (capability state), `~/.claude` and `~/.codex` (the logins) - and one for `~/work`, the agent's work, and only the two keys above in its environment. Then it mints the node's id, twelve lowercase hex characters, and registers the node row under it with `<node>` as its label; the answer's `node.node` is the id, and every later verb takes the id or the label. The container comes up healthy and its log says it is waiting for a store setting; it runs nothing yet.

## Work

The agent keeps what it develops in - the checkouts of the repositories it works on and the git worktrees it opens beside them - where the manager says this machine keeps worktrees: `worktree_root` in `capabilities path --json`, which on a node answers its work volume. It is a volume of the node's own, so a redeploy or a new image tag keeps every checkout and every worktree, and git's records of the worktrees keep pointing at directories that are still there. Anything outside the volumes - the rest of the home, `/tmp` - is the image's, and goes with the container. The image makes the work folder and gives it to `agent` from agent-node 0.0.9; on an older tag the volume comes up owned by root, so give it to the node's user once with `docker exec -u root <container> chown agent:agent /home/agent/work`.

## Create from a repository

A node that needs more than the standard image carries - other tools, the host's Docker, the host's directories - gets an image of its own, built `FROM` the standard one. `deployment node create <node>` with `--repository <url> --branch <branch> --dockerfile <path>` in place of `--version` has Coolify clone that branch on the node's server and build the Dockerfile there, natively for the server's architecture, with no CI and no registry. `--context <dir>` names the build context when it is not the repository root; the Dockerfile lies inside it. A private repository is cloned through a Coolify deploy key (`--deploy-key <uuid>`) or GitHub App (`--github-app <uuid>`); `coolify sources` lists both.

The node is then a Coolify application rather than a service. It is told the same two keys, keeps the same volumes and memory limit, and gets no domain and no automatic deploy on a push, so a commit to the branch never restarts it. Fixed, non-secret settings its tools need go in the Dockerfile as `ENV`; `deployment` hands it nothing more.

Only such a node takes host paths, each bound at the same path inside the container, so a container it starts on the host sees the same paths it does:

- `--docker-socket` binds the host's `/var/run/docker.sock`. The node runs as user `agent`, which reaches the socket only through its group. From agent-node 0.0.4 the image puts `agent` in a group with the socket's group id at boot, read from the socket itself, so the Dockerfile names no id. On an older image the Dockerfile creates that group with the id the socket has on that server (`stat -c %g /var/run/docker.sock` there) and adds `agent` to it, then returns to `USER agent`. The socket is root on that server, so give it only to a node on a server that serves nothing else.
- `--host-dir <path>`, repeatable, binds a directory of the server, which must already exist and belong to the node's user (`agent`, uid 1000 in the standard image) for the node to write in it. A secret file the work needs there is placed once by a person; `deployment` puts nothing in it.

Coolify never creates, writes or deletes a host path, and `node remove` deletes the application and its volumes but never the socket, a host directory or what is in it.

The answer names the application, the build's deployment and the `coolify wait` command that waits for the build; enrol once it is done. To rebuild - a change to the Dockerfile, or a new standard-image tag in its `FROM` - run `coolify deploy <uuid>` for the application; the state volumes stay. Its container is the one labelled `com.docker.compose.project=<resource>` alone, without the `agent` service label, and `coolify applications <resource>` shows what Coolify reports for it.

## Enrol

`deployment node enrol <node> --services <name,...>` reaches the container over SSH and `docker exec` and, in this order:

- writes the node's id onto its config volume with the node's own `capabilities node id --set <id>`, so the node runs as the node its row is, across restarts and new image tags;
- makes the node's X25519 key pair in the container, keeps the private key on its config volume and records the public key on the node row;
- writes the agent's encryption key, `AGENT_ENCRYPTION_KEY` as it resolves here, into `~/.config/capabilities/shared.env` on the node's config volume, so the node opens the agent's vault in the store (`capabilities secrets`) and reads every secret kept there itself. A key that is not the one the vault is kept under is refused before anything is written to the node;
- writes the agent's other secrets - every secret key of a connection each capability the agent can use resolves here, enabled for the project or globally, apart from a name the vault holds - into each capability's credentials file on the node. The token of the coolify connection the node was created through is not among them: it stays on the deploying machine, and the node does not hold it. The answer's `secrets_in_vault` names each secret key left to the vault, `secrets_skipped` names each secret key that did not go, with the reason, and `environment_only` names each secret-looking key a capability reads from the process environment alone - one a compiled target kept in its container environment, say - which no node receives: keep it in a connection of that capability and enrol again;
- places each named service on the node (`capabilities place <name> --node <node>`), which takes an exclusive service off the machine it ran on before and leaves the capability usable everywhere it was; the answer's `unplaced` names those machines. A fresh node runs no service until one is placed there, and you can place or unplace services later without enrolling again;
- writes the store setting, last, with the node's own manager. That is what the waiting node raises the agent on, and enrol waits until the node reports `running agent ... as node <node>` (`--wait`, else `node_not_raised`).

The key, the secrets and the store password travel on standard input and are never printed or put on a command line. Keeping a secret in the agent's vault rather than in a file here means a node needs nothing but the key to read it, and a secret changed in the vault reaches every node without enrolling again.

## Log in on the node

No login is handed to a node: a person makes each one on it, once, and it stays on the node's volumes. No `deployment` verb does it for a node made by `node create` yet (`deployment node login` serves a `deployment/targets/` application); open a terminal into the container yourself:

```sh
ssh -t root@<server-address> docker exec -it <container> claude auth login
ssh -t root@<server-address> docker exec -it <container> codex login --device-auth
```

The container is the one Docker labels `com.docker.compose.project=<resource>` and `com.docker.compose.service=agent`, where `<resource>` is the node row's `coolify_resource`: `docker ps --filter label=com.docker.compose.project=<resource> --filter label=com.docker.compose.service=agent` on the server names it.

`gh auth login` works the same way: the image carries the GitHub CLI from `agent-node` 0.0.2, and 0.0.1 does not, so on an older node check with `gh --version` in the container. Its login is kept under `~/.config`, on the config volume.

A login that speaks JSON events instead of a terminal runs from this machine with `deployment node exec <node> -- <command>`, which reaches the same container over SSH and `docker exec -i` with no terminal: `deployment node exec box1 -- telegram login --events` or `deployment node exec box1 -- whatsapp pair --events` streams each event line back unchanged, forwards what you type on standard input, and exits with the command's own code.

## Move a service onto the node

Where a service runs and where its capability is usable are two separate answers of the agent's gate: a service runs on a node only where it is placed there, and placing or unplacing it never changes where the capability's CLI works (`deployment guide profiles`, **Where a service runs**). Each service's descriptor says how many nodes run it at once, and `<name> manifest` shows it as `service.placement`: `exclusive`, for a service that holds one account or one connection, runs on one node at a time and its lease enforces that; `shared` runs on every node it is placed on.

To move an exclusive service from the machine it runs on now to the node, place it on the node:

```sh
capabilities place <name> --node <node>
```

That one write places it on the node and takes it off every other node; the answer names them under `unplaced_from`. The capability stays usable on the machine it left - reading its messages there on demand keeps working - and only its service stops there. At its next reconcile the machine's runner stops the service and releases the lease, and the node's runner takes it over at its own next reconcile, by default within a minute.

A shared service is placed on the node beside the machines it already runs on; to take it off one of them, `capabilities unplace <name> --node <machine>`.

Do not move a service with `capabilities disable <name> --project --node <machine>`: that turns the capability off on that machine, CLI and all. `capabilities inherit <name> --project --node <machine>` removes the machine's row, and with it the service, and leaves the CLI to whatever the project's row for every node says. Both warn when they take a service off a node.

No recompile and no redeploy is needed on either side. `deployment services` on each side shows what the gate answers there, whether the runner runs the service and who holds the lease.

## Status and logs

`deployment node status` and `deployment logs` read a `deployment/targets/` application and do not reach a node made by `node create` yet. What there is today:

- `capabilities node list` here: the node's row, its resource and its public key;
- `coolify services <resource>`: what Coolify reports for the node's service;
- in the container, `docker exec <container> deployment services`: each service's gate answer, runner state, refusal and lease; `docker exec <container> cat /tmp/agent-node.state`: what the node is doing (`idle: ...`, `running agent ...`);
- on the server, `docker logs <container>`: the boot log, which says what an idle node is waiting for and what the agent's raise did.

## Update to a new image version

`deployment node update <node> --version <new>` moves a node of the standard image to `ghcr.io/agenium-dev/agent-node:<new>`. The tools are the image's, and everything the node learned is on its volumes, so an update is the same resource running a newer tag: it writes the node's Coolify service the whole Compose document `node create` writes for it, with the new tag, through the coolify connection its row names (`coolify service set-compose`, so the token needs no read:sensitive), redeploys it with its volumes kept, and waits for its container to run the new tag, report healthy and read its agent running (`--wait`, 600 seconds by default; `node_not_raised` otherwise). The store setting, the node's id, the secrets, the node key and the logins stay on the volumes, and each boot brings the capabilities to the catalogue the new image names. The document's parts come from where `node create` got them: the Compose service name `agent` and the volume names are node create's own; the environment is this agent's id and `CAPABILITIES_PROJECT_ID_ROOT`; the memory limit is the one the node's container runs with. Before it writes anything it reads the node's agent container over SSH with `docker inspect` and refuses, changing nothing, unless every volume the document mounts is the one the container mounts at that path, named `<resource>_<name>`, and the container mounts nothing else but the image's own anonymous volumes (`node_volumes_unconfirmed`) - a renamed volume would start the node empty - and unless it has a memory limit and sets exactly those two keys besides the image's environment and Coolify's own keys (`node_document_unconfirmed`). An `AGENT_NODE_NAME` other than the node's id, or an `AGENT_NODE_ID` equal to it, from a node created before its identity left the environment, is dropped and named in `env_dropped`; a node still keyed by its name keeps needing it and is refused. A path the container mounts nothing at, as `work` on a node created before node create declared it, gains the document's volume, new and empty, and is named in `volumes_added`; whatever the old container kept there outside a volume - checkouts in `work` on such a node - is gone with it, so push or copy it off first. A node with no agent container at all is refused (`node_document_unconfirmed`): bring it back on its current document with `coolify deploy <uuid>` and run the update again. A node already running the tag with no volume to gain is not written, and is redeployed only when its container is not running, so running the update again finishes one that left the node down. A node built from a repository runs no tag and is refused (`node_built_from_repository`): rebuild it as **Create from a repository** says. Do not update with `node remove` and `node create`: remove deletes the volumes, and the new node has to be enrolled and logged in again. A node created before node ids is registered under its name: the first boot of an image that carries node ids finds the store still holding a node whose id is the `AGENT_NODE_NAME` it was created with and moves it to its own id with `capabilities node rekey`, before it raises the agent.

## Remove

`deployment node remove <node> --yes` deletes the node's Coolify resource with its volumes and configuration, then its node row. It first checks that the resource is this node's own - its agent container runs the standard image for this agent - and deletes nothing when it is not (`node_resource_not_own`); a resource already gone leaves only the row to remove.

The policy rows that placed services on the node are not removed with it; they name its id, which no other node takes. Take them off first, one per service: `capabilities unplace <name> --node <node>`, or `capabilities inherit <name> --project --node <node>` to remove the row altogether.

## Architectures

`agent-node` is published for `linux/amd64` and `linux/arm64` from 0.0.3, so an amd64 or an arm64 server pulls the image for its own architecture under the same tag. 0.0.1 and 0.0.2 are published for `linux/amd64` only, and an arm64 server cannot run them.

## Another provider

The image is plain Docker; Coolify is only how `deployment` creates and removes it. Any host that runs a container can run a node: start `ghcr.io/agenium-dev/agent-node:<version>` with `CAPABILITIES_PROJECT_ID`, `CAPABILITIES_PROJECT_ID_ROOT=/app`, a named volume on each of the four state paths and on `/home/agent/work`, a memory limit and a restart policy. Mint it an id - twelve lowercase hex characters, such as `openssl rand -hex 6` prints - and register its row from the agent's folder with `capabilities node add <id> --label <label> --kind server`; the node raises no agent without it. `deployment node enrol` and `remove` reach only a node whose row names a Coolify resource, so on another provider you do by hand what **Enrol** lists - the id onto its volume with `capabilities node id --set <id>` in the container, the secrets into each capability's credentials file in the container, `capabilities place <name> --node <node>` from the agent's folder, and the store setting last with the node's own `capabilities store set` - and removing it is deleting the container and its volumes, then `capabilities node remove <node>`.

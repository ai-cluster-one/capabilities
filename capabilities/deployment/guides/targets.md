# Deployment Targets

A target lives at `deployment/targets/<name>/target.json` and uses schema
`capabilities.deployment.target.v1`. Its folder also holds the bundle `deployment
sync` compiles for it on a container profile. A declaration written before
bundles, `deployment/targets/<name>.json`, is read where it is until it moves.

Required fields:

- `name` - target name, usually `production` or `staging`.
- `provider` - `coolify`, `dockerhost`, or `manual`.
- `connection` - provider connection id, or `null` for `manual`.
- `resource` - provider-facing handle data.

Coolify targets should store a resource identifier label, not a raw UUID:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "coolify",
  "connection": "default",
  "environment": "production",
  "resource": {
    "type": "application",
    "identifier_label": "production_resource_uuid"
  }
}
```

The raw Coolify UUID belongs in the Coolify identifiers envelope:

```sh
coolify ids set production_resource_uuid <uuid> --note "production application"
```

Raw-server targets are reserved for a Docker-host adapter:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "dockerhost",
  "connection": "production",
  "environment": "production",
  "resource": {
    "remote_path": "/opt/marvin",
    "compose_project": "marvin"
  }
}
```

`deployment plan` reports whether the provider capability is installed/enabled
locally, but it does not perform remote operations.

## Agent nodes through Coolify

A checkout node's target selects `provider: coolify`, an explicitly granted `connection`, `server` (a label in `coolify ids` holding the server UUID), `project` (a project UUID label, created on first deploy), `environment` (the Coolify environment name, normally `production`), `deploy_key` (the instance's deploy-key UUID label), and `resource.identifier_label` (the application UUID label written on first deploy). The connection's `ssh.key_path` is the Mac's root SSH identity; when omitted, the first existing identity from OpenSSH's resolved configuration is used. A local Coolify server reported as `host.docker.internal` uses the instance hostname's resolved address for SSH, with normal host-key verification. Prepare the instance's deploy key through Coolify's bootstrap/provider surface; deployment creates a missing project and places the application in the project's existing `environment`, `production` when the field is absent, which Coolify creates with every project. It creates no environment: a named one the project lacks stops the deploy before anything is created. Every deploy sets the application's base directory to the target's bundle folder (`/deployment/targets/<name>`) and its Compose location to `/docker-compose.yaml`, because Coolify runs Compose from the base directory and the bundle names its build context relative to its own folder; an application created before the project moved onto bundles follows on its next deploy. The application is named `<project>-<target>`, where the project is the name ContextKit's config gives it, else the slug the capabilities manager recorded for it, and never the checkout folder, so a worktree deploys the same application under the same name; every deploy sets the name again. Without a GitHub body source the instance's deploy key must already be authorized for the server's `git` user. A sole key from `coolify sources` can be selected without a `deploy_key` label.

Run `deployment setup --profile agent-box-checkout --target <name> --provider coolify`, fill the target labels and connection, run `deployment sync`, review and commit the declarations and compiler inputs, then run `deployment deploy --target <name>`. A clean body and `deployment sync --check` are required. Redeploying an existing application needs Coolify's rollback interface before deployment starts; an initial deployment has no previous release and can use the deploy and wait surface alone; the waiter uses `coolify wait` when available and otherwise polls deployment and application status behind one adapter.

The Mac pushes the committed branch through `node-<name>` to `git@<server>:/srv/git/<project-slug>.git`. Deployment prepares the bare repository, allows rollback fetches, and generates a separate SSH identity for this node in the checkout's private Git directory. The private half reaches the node only through `coolify env bulk` on stdin. The node uses the Docker host gateway with the server host key obtained over the Mac's trusted root SSH connection. No private key belongs in the body. Coolify builds the repository only when deployment asks, with automatic deploys disabled. The checkout, Claude state and Codex state remain named volumes across redeploys.

### A body taken straight from GitHub

A checkout target can instead take its body from a GitHub repository, so neither the server's bare repository nor this machine carries it:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "coolify",
  "connection": "default",
  "server": "node_server_uuid",
  "project": "node_project_uuid",
  "environment": "production",
  "deploy_key": "node_deploy_key_uuid",
  "body_source": {"kind": "github", "repository": "example-org/example-repo"},
  "resource": {"type": "application", "identifier_label": "node_app_uuid"}
}
```

`body_source.repository` is `<owner>/<repository>` without `.git`. `deploy_key` is required with it, and is the Coolify identifier label of the key Coolify clones the repository with; its public half must be added to the GitHub repository as a read-only deploy key. `deployment doctor` and `deployment plan` refuse a malformed declaration with `body_source_invalid`, and every node verb refuses it the same way.

`deployment deploy` then prepares no server repository, pushes through no `node-<name>` remote and opens no SSH connection for the body. It fetches the branch through this checkout's own remote for that repository, in any form git writes a GitHub URL, and refuses with `github_remote_missing` when there is none and with `commit_not_on_remote` when the commit being deployed is not on the GitHub branch: push first. A new application is created from `git@github.com:<owner>/<repository>.git` with the declared key; an existing one is pointed at that repository and the current branch on every deploy and keeps the key it already has. The build is pinned to the deployed commit through Coolify's rollback interface, so a branch the node has moved on since is not what gets built. `AGENT_REPO_URL` is `https://github.com/<owner>/<repository>.git`, and the node's body sync pulls and pushes it with the node's own git credentials for GitHub, which the project provides in the node's environment; no key is shipped, and the deploy empties `GIT_DEPLOY_KEY_B64` and `AGENT_GIT_KNOWN_HOSTS_B64` on the application.

A node takes its database only from its server's environment. Set `AGENTKIT_DB_URL`, or the separate `AGENTKIT_DB_*` fields, on the Coolify application once, with `AGENTKIT_DB_TRANSACTION_PORT` (and `AGENTKIT_DB_TRANSACTION_HOST` when it differs) where the store also has a transaction address; the Compose file passes each through to the containers, and every capability there resolves it through the same cascade as anywhere else (the store setting in the executable standard names the keys and their order). A deploy sends no database value from the Mac and writes no setting file in the container, and its bulk environment update leaves the server's own values standing.

`deployment node login claude --target <name> --print-command` and `deployment node login codex --target <name> --print-command` prepare the terminal commands; the person runs them and finishes subscription login in their browser. Without `--print-command`, login requires a terminal. `deployment logs --target <name> [--build] [--follow]` reads provider logs. `deployment rollback --target <name> [--to <commit>]` waits for the rollback; a failed deploy requests the previously recorded release and reports whether that request succeeded.

`deployment node sync --target <name>` fetches, rebases the current clean branch on the node branch, and pushes it. For a target with a GitHub body source it answers `"state": "nothing_to_carry"` with exit 0 and touches no remote, and `host-agents` compiles no interval job for it. A conflict aborts the rebase, preserves the branch and reports `diverged` with paths. `mirror` optionally names a second Git remote to push afterwards. ContextKit resolves its memory root for the compiler's union rule in `.gitattributes`; commit that rule before deploying. The node snapshots its own changes before trying the network, so an offline interval loses nothing. Each successful sync rebuilds the generated context with `capabilities build`; generated output remains covered by the body's existing ignore rules and is never a deployment source. `host-agents` compiles a launchd interval job for each Coolify target (`sync_interval`, default 60 seconds); hand it to launchd through the existing `deployment next` procedure.

### The node a target's server runs as

`node` names the node the target's server runs as in the gate, the name a store-mode policy row and a service lease name it by:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "coolify",
  "node": "example-node",
  "resource": {"type": "application", "identifier_label": "node_app_uuid"}
}
```

It takes letters, digits, `.`, `_` and `-`, starting with a letter or digit. `deployment sync` writes it into the target's bundle as `AGENT_NODE_NAME` in the Compose file's shared environment, as a literal every container receives whatever the server's environment holds, and `deployment deploy` sends the same value to the application's environment. The target is its one home: a `services.<name>.environment_defaults` that also sets `AGENT_NODE_NAME` stops `sync` and `doctor` with `node_declared_twice`, and a malformed value with `node_invalid`. A target without `node` compiles as before, and its containers take `AGENT_NODE_NAME` from the server's environment, the host name - for a container, its id - where that holds none. A runtime that names its own artifact paths compiles no bundle, so `node` there is reported as `node_not_compiled`. `deployment node status` reports the declared name as `declared_node_name` beside the `node_name` the node answers to, with `node_name_mismatch` while they differ.

`service_policy.placement` maps capability service names to `node`, `local` or `both`. Custom services use `services.<name>.placement`. An absent placement preserves the profile's current behavior. The container compiler selects node/both; host-agents selects local/both. Placement is read in files mode; in store mode it is retired, and which node runs a service is the gate row naming that node, with a lease keeping it on one machine (`deployment guide profiles`). `deployment node status --target <name> --json` reports provider and SSH observations, the recorded release, the fetched Git sync state, login state, service placement (files mode) with each service's state on the node and here and, from `deployment services` run in the node's container, what the gate answers for it there, whether it runs, any refusal it is held idle for, and who holds its lease, and the node's database in force - resolved in its container through the shared library, with the level and sources that answered, the setting with its secrets redacted, whether it is reachable and whether the server refuses it without TLS; unknown observations and errors remain explicit. Git sync state is read against the node remote, or for a GitHub body source against this checkout's remote for that repository, `unknown` when it has none. The status read uses the last fetched Git refs and does not itself fetch, rebuild or write.

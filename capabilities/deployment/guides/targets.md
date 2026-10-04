# Deployment Targets

A target lives at `deployment/targets/<name>.json` and uses schema
`capabilities.deployment.target.v1`.

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

A checkout node's target selects `provider: coolify`, an explicitly granted `connection`, `server` (a label in `coolify ids` holding the server UUID), `project` (a project UUID label, created on first deploy), `environment` (the Coolify environment name, normally `production`), `deploy_key` (the instance's deploy-key UUID label), and `resource.identifier_label` (the application UUID label written on first deploy). The connection's `ssh.key_path` is the Mac's root SSH identity. Prepare the instance's deploy key through Coolify's bootstrap/provider surface; deployment creates a missing project and environment. The current CLI has no environment-create verb, so that one request uses the granted connection through a narrow adapter to [Coolify's environment endpoint](https://coolify.io/docs/api/endpoints/projects/create-environment). The instance's deploy key must already be authorized for the server's `git` user. A sole key from `coolify sources` can be selected without a `deploy_key` label.

Run `deployment setup --profile agent-box-checkout --target <name> --provider coolify`, fill the target labels and connection, run `deployment sync`, review and commit the declarations and compiler inputs, then run `deployment deploy --target <name>`. A clean body and `deployment sync --check` are required. The target needs Coolify's rollback interface before deployment starts; the waiter uses `coolify wait` when available and otherwise polls deployment and application status behind one adapter.

The Mac pushes the committed branch through `node-<name>` to `git@<server>:/srv/git/<project-slug>.git`. Deployment prepares the bare repository, allows rollback fetches, and generates a separate SSH identity for this node in the checkout's private Git directory. The private half reaches the node only through `coolify env bulk` on stdin. The node uses the Docker host gateway with the server host key obtained over the Mac's trusted root SSH connection. No private key belongs in the body. Coolify builds the repository only when deployment asks, with automatic deploys disabled. The checkout, Claude state and Codex state remain named volumes across redeploys.

`deployment node login claude --target <name> --print-command` and `deployment node login codex --target <name> --print-command` prepare the terminal commands; the person runs them and finishes subscription login in their browser. Without `--print-command`, login requires a terminal. `deployment logs --target <name> [--build] [--follow]` reads provider logs. `deployment rollback --target <name> [--to <commit>]` waits for the rollback; a failed deploy requests the previously recorded release and reports whether that request succeeded.

`deployment node sync --target <name>` fetches, rebases the current clean branch on the node branch, and pushes it. A conflict aborts the rebase, preserves the branch and reports `diverged` with paths. `mirror` optionally names a second Git remote to push afterwards. ContextKit resolves its memory root for the compiler's union rule in `.gitattributes`; commit that rule before deploying. The node snapshots its own changes before trying the network, so an offline interval loses nothing. Each successful sync rebuilds ContextKit output; generated output remains covered by the body's existing ignore rules and is never a deployment source. `host-agents` compiles a launchd interval job for each Coolify target (`sync_interval`, default 60 seconds); hand it to launchd through the existing `deployment next` procedure.

`service_policy.placement` maps capability service names to `node`, `local` or `both`. Custom services use `services.<name>.placement`. An absent placement preserves the profile's current behavior. The container compiler selects node/both; host-agents selects local/both. Placement does not implement exclusivity or a lease. `deployment node status --target <name> --json` reports provider and SSH observations, the recorded release, the fetched Git sync state, login state, service placement, and the node's store probes; unknown observations and errors remain explicit. The status read uses the last fetched Git refs and does not itself fetch, rebuild or write.

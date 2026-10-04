# deployment — change log

## 2026-10-04 — Agent nodes deploy and sync through Coolify

`deploy`, `rollback`, `logs`, and `node login|status|sync` operate a checkout node through a granted Coolify connection and the server's SSH Git hub. Environment and node keys travel on stdin, subscription logins remain interactive, failed deployments request the previous release, and body conflicts abort without discarding either writer. Service placement selects node/local/both per substrate; ContextKit memory uses Git union merges, and host-agents compiles node sync intervals. No existing declaration needs a placement field.

## 2026-10-03 — Machine services are supervised by a machine scope

`deployment machine sync` compiles a launchd agent `capabilities.machine.<name>` for every installed capability that declares `service.machine` and is allowed on this machine, running its machine command with `AbandonProcessGroup`, plus a `capabilities.machine.watchdog` agent whose pass (`deployment machine watchdog`) runs each one's machine doctor and kickstarts it after repeated failures. Settings live in `$XDG_CONFIG_HOME/deployment/machine.json` (watchdog settings and a per-service `disabled`) and the agents in `$XDG_STATE_HOME/deployment/machine/launchd/`, whatever directory it runs from. `deployment machine next` prints the `launchctl` hand-over and `deployment machine status` reports each agent; installing one stays a person's act. Nothing the project scope does has changed.

## 2026-10-03 — Container images install their capabilities allowed

The generated Dockerfile installs each capability in `deployment/capabilities.lock` with `capabilities install <name> --allow`, because a capability new to a machine otherwise arrives quarantined and an image is one agent's whole environment. Rebuild an image with `deployment sync` to pick it up.

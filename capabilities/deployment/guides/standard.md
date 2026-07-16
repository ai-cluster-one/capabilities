# Deployment Standard

`deployment` owns the local, provider-neutral deployment description for a
project. Provider capabilities own remote systems.

The split is deliberate:

- `deployment` declares what the project runs: runtime profile, compose file,
  services, required environment variables, state volumes, and deployment
  targets.
- `coolify`, `dockerhost`, `railway`, or another adapter executes one target
  against its own external substrate.
- Context files may point to deployment references, but they do not need to
  duplicate the runtime schema.

The standard files are:

- `deployment/runtime.json` - one runtime declaration for the project.
- `deployment/targets/*.json` - one target declaration per deploy destination.
- `deployment/capabilities.lock` - lightweight install list for the agent image:
  one capability name per line.
- `capabilities/deployment/reference/*.md` - lightweight context pointers.

Use `deployment setup` for a full bootstrap, including Dockerfile,
docker-compose, env example, entrypoint, deployment declarations, and human next
steps. Use `deployment init` only when you want the declaration files without
Docker scaffolding. Use `deployment freeze` after the capability gate changes,
`deployment doctor` before relying on the declaration, and `deployment plan`
when deciding which provider adapter would execute a target.

Docker builds should bootstrap the capabilities manager from the selected
`CAPABILITIES_REF`, then run `capabilities install <name>` for each non-comment
line in `deployment/capabilities.lock`.

For ContextKit projects, Docker builds install ContextKit via the public
installer (https://raw.githubusercontent.com/ai-cluster-one/context-kit/${CONTEXTKIT_REF}/install.sh),
defaulting `CONTEXTKIT_REF` to `main`. After copying the project body and
installing capabilities, the build runs `contextkit install-hooks` for all
configured targets, `contextkit build --target all` to generate host bindings
and compile context, and `contextkit doctor` to verify. Generated host bindings
(`.codex/generated/`, `.claude/rules/CONTEXT.md`) and the ContextKit manager
binary (`.contextkit/manager/`) are excluded from the build context via
`.dockerignore`. These are target-local build artifacts, not deployment inputs
shipped from the repo.

`deployment` does not require `coolify` to be enabled. If a target chooses
`coolify`, that only means the target is executable by the `coolify` capability
when the project later enables and configures it.

# Deployment Standard

`deployment` owns the local, provider-neutral deployment description for a
project. Provider capabilities own remote systems.

The split is deliberate:

- `deployment` declares what the project runs: runtime profile, compose file,
  services, required environment variables, state volumes, and deployment
  targets.
- `coolify`, `dockerhost`, `railway`, or another adapter executes one target
  against its own external substrate.
- Project-specific reference files are optional; when absent, agent context
  reads the canonical runtime and target JSON directly.

The standard files are:

- `deployment/runtime.json` - one runtime declaration for the project.
- `deployment/targets/*.json` - one target declaration per deploy destination.
- `deployment/capabilities.lock` - lightweight install list for the agent image:
  one capability name per line.
- `capabilities/deployment/reference/*.md` - optional project-specific operational
  models. Reference files are neither created nor required by deployment init or
  setup. When present, they hold genuine project-specific deployment context
  rather than pointers duplicating runtime.json or target JSON schemas.

Use `deployment setup` for a full bootstrap, including Dockerfile,
docker-compose, env example, entrypoint, deployment declarations, and human next
steps. Use `deployment init` only when you want the declaration files without
Docker scaffolding. Use `deployment freeze` after the capability gate changes,
`deployment doctor` before relying on the declaration, and `deployment plan`
when deciding which provider adapter would execute a target.

Docker builds should bootstrap the capabilities manager from the selected
`CAPABILITIES_REF`, then run `capabilities install <name>` for each non-comment
line in `deployment/capabilities.lock`, initialize project contexts with
`capabilities init`, and verify the capability set with `capabilities doctor` to
ensure the lock is complete before proceeding.

For ContextKit projects, Docker builds install ContextKit via the public
installer (https://raw.githubusercontent.com/ai-cluster-one/context-kit/${CONTEXTKIT_REF}/install.sh),
defaulting `CONTEXTKIT_REF` to `main`. After copying the project body, installing
capabilities, initializing project contexts, and verifying the capability set, the
build runs `contextkit init` to create target-local technical bindings,
`contextkit install-hooks` for all configured targets, then the canonical validation
sequence: `contextkit doctor` to verify configuration, `contextkit build --target all`
to generate host bindings and compile context, and `contextkit audit` to validate the
built context. Generated host bindings (`.codex/generated/`, `.claude/rules/CONTEXT.md`),
the ContextKit manager binary (`.contextkit/manager/`), and machine-local bindings
(`.env.local`) are excluded from the build context via `.dockerignore`. These are
target-local build artifacts, not deployment inputs shipped from the repo.

`deployment` does not require `coolify` to be enabled. If a target chooses
`coolify`, that only means the target is executable by the `coolify` capability
when the project later enables and configures it.

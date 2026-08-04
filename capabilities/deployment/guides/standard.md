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

Use `deployment setup` for a full bootstrap, including Dockerfile, Compose, env
example, entrypoint, deployment declarations, and human next steps. Use
`deployment init` only when you want declarations. `deployment sync` is the sole
compiler for capability services: it discovers installed
`manifest.service.deploy` descriptors, filters them through explicit project
enablement and `runtime.json` service policy, and reconciles the runtime graph,
lock, Compose, env example, Dockerfile, dockerignore, and entrypoint.

`service_policy.auto_include` controls descriptor-default auto inclusion.
`service_policy.capabilities` accepts `enabled`, `disabled`, or `auto` per
capability. An override cannot bypass the explicit project gate. Run
`deployment sync --check` in CI; it performs no writes and reports structured
missing/content/ownership drift with exit 7.

Generated root artifacts carry an ownership marker. Sync updates marked files
and creates missing files, but refuses an unmarked file at an owned path. Local
Compose overlays, additional env files, and unrelated project files are left
untouched. For a pre-standard project, review the finding and either move local
customization into an overlay or deliberately adopt the generated boundary with
`deployment setup --force`. A legacy runtime without `service_policy` migrates
listed capability services to explicit enabled overrides on first sync. Legacy
Telegram/automations setup flags remain policy shorthands.

This stage compiles generated container artifacts for the `agent-box` profile.
The `generic` profile remains a declaration-only runtime and doctor reports an
actionable finding if sync is requested for it.

Use `deployment freeze` after a capability gate change when only the lock is
needed, `deployment doctor` to validate settings → lock → runtime services →
compiled artifacts, and `deployment plan` to select the provider adapter.

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

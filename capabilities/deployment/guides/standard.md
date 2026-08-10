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
  one capability name per line. It is derived from the project gate, minus the
  host-only names declared by `runtime.json` under `capabilities.exclude`.
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

`service_policy.auto_include` controls descriptor-default auto inclusion, and
`service_policy.default_mode` selects `embedded` or `enabled` for those auto
services. New `agent-box` setup defaults to `embedded`; existing runtimes that
do not declare the field retain the previous `enabled` behavior.
`service_policy.capabilities` accepts `enabled`, `embedded`, `disabled`, or
`auto` per capability. `enabled` renders a separate Compose service;
`embedded` merges the descriptor's environment and mounts into the agent
service and, for managed agent-box artifacts, renders its command into a
generated Supervisor configuration; `disabled`
omits the service runtime while leaving the CLI available. An override cannot
bypass the explicit project gate. Run
`deployment sync --check` in CI; it performs no writes and reports structured
missing/content/ownership drift with exit 7.

The project gate answers which capabilities are usable while working in the
repository. The image lock answers which of those capabilities ship inside the
deployed body. Keep operator-only adapters such as a deployment provider on the
host with an explicit runtime exclusion:

```json
{
  "capabilities": {
    "lockfile": "deployment/capabilities.lock",
    "exclude": ["deployment", "coolify"]
  }
}
```

An enabled or embedded service cannot be excluded because its runtime command
would be absent from the image.

New setup uses root artifact paths, but runtime v1 can explicitly declare a
custom compiler layout. `compose_file` selects the generated base Compose file;
`compiler.artifacts` selects the Dockerfile, entrypoint, env example,
dockerignore, and optional supervisor paths; `compiler.compose_overlays` lists project-owned overlays;
and `compiler.container` resolves portable service mount targets:

```json
{
  "compose_file": "deployment/compose.yaml",
  "compiler": {
    "artifacts": {
      "dockerfile": {"path": "deployment/docker/Dockerfile", "ownership": "external"},
      "entrypoint": {"path": "deployment/docker/entrypoint.sh", "ownership": "external"},
      "supervisor": {"path": "deployment/supervisor/supervisord.conf", "ownership": "external"},
      "env_example": {"path": ".env.example", "ownership": "external"},
      "dockerignore": {"path": ".dockerignore", "ownership": "external"}
    },
    "compose_overlays": ["deployment/compose.local.yaml"],
    "container": {"agent_home": "/home/jess", "project_root": "/app"}
  }
}
```

Artifact strings remain managed for compatibility. The object form makes
ownership explicit: `managed` artifacts participate in generation, drift, and
`--adopt`; `external` artifacts must exist but are never generated, compared,
or overwritten. Compose remains managed through `compose_file`, while declared
overlays remain external.

Compose build arguments are emitted only from `compiler.build_args`. When that
mapping is absent or empty, generated Compose has no `build.args`, so an
external Dockerfile's own `ARG` defaults remain authoritative.

Capability descriptors use `{agent_home}` and `{project_root}` mount targets.
Compilation resolves those tokens through `compiler.container`; an existing
runtime volume mount is an explicit project override and is never relocated.
Descriptor-owned command, doctor, required environment, and state requirements
are reconciled into each capability service. Project-owned role, description,
restart, additional required/optional environment, additional state, and
extension fields are preserved. Descriptor requirements are added as a union,
not used as a replacement.

For an embedded descriptor, the same required/optional environment, defaults,
and state mounts are reconciled into `services.agent`; its name is recorded in
`services.agent.embedded_services`. Managed agent-box artifacts install
Supervisor, render each descriptor command as one program, and start it from the
entrypoint. If the Dockerfile, entrypoint, or supervisor artifact is declared
`external`, its process-management implementation remains project-owned.

Each runtime service may declare `environment_defaults`, a mapping from env key
to string fallback. These project defaults take precedence over descriptor
optional defaults and may introduce additional project env keys. Required env
uses non-failing `${KEY:-}` interpolation in Compose; readiness belongs to
doctor/preflight rather than Compose parsing.

Managed generated artifacts carry an ownership marker. Sync updates marked files and
creates missing files, but refuses an unmarked file at any declared artifact
path. External artifacts are validated for existence and otherwise ignored.
Declared Compose overlays, additional env files, and unrelated project files
are left untouched. For a pre-standard project, first declare the exact
layout and container paths in `runtime.json`, run `deployment sync --check`,
review its ownership drift, then run `deployment sync --adopt` to transfer only
declared managed artifact paths to the compiler. For a `manual` target, also set
`resource.compose_file` to the same value as runtime `compose_file`; doctor and
plan reject a mismatch. A legacy runtime without
`service_policy` or `compiler` records root-layout defaults on first sync;
legacy Telegram/automations setup flags remain policy shorthands.

This stage compiles generated container artifacts for the `agent-box` profile.
The `generic` profile remains a declaration-only runtime and doctor reports an
actionable finding if sync is requested for it.

Use `deployment freeze` after a capability gate change when only the lock is
needed. `deployment doctor` validates settings → lock → runtime services →
declared generated artifacts, then runs Compose semantic validation with the
declared base and overlays. `deployment next` likewise emits layout-aware local
commands. Use `deployment plan` to select the provider adapter.

Docker builds should bootstrap the capabilities manager from the selected
`CAPABILITIES_REF`, then run `capabilities install <name>` for each non-comment
line in `deployment/capabilities.lock`, initialize project contexts with
`capabilities init`, and verify the capability set with `capabilities doctor` to
ensure the lock is complete before proceeding.

Generated agent-box images install Codex from the complete pinned release
package rather than the legacy single-binary archive, so the CLI,
`codex-code-mode-host`, and its runtime resources stay version-aligned.

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

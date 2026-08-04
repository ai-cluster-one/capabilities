# Deployment Bootstrap

Use `deployment setup` when a project should become deployable without asking a
human to make Docker architecture decisions.

Default flow:

```sh
deployment setup
deployment doctor
deployment next
```

The default profile is `agent-box`: a long-running container image with Codex,
Claude, ContextKit when the project uses it, and capability CLIs installed by
the capabilities manager. Setup compiles deploy descriptors for capabilities
explicitly enabled in project settings. Global inheritance, installed payloads,
and existing service directories do not authorize runtime services.

`deployment setup --dry-run` shows which files would be written before changing
the project.

The human-facing questions are intentionally simple:

- What kind of thing is being deployed? Default: `agent-box`.
- Where should it run? Default: `coolify`.
- Which explicitly project-enabled capability services should run? Default:
  descriptor policy plus `service_policy.auto_include`.

The Telegram and automations setup flags remain compatibility shorthands. They
write `enabled` or `disabled` overrides into `runtime.json`; `auto` leaves the
generic descriptor policy in control.

After setup, `deployment next` prints the checklist for the chosen target:
which files were created, which secrets must be entered, local validation
commands, provider steps, and first-login commands for Codex, Claude, and
Telegram when present.

## ContextKit Projects

For projects with `.contextkit/config.toml`, the generated Dockerfile:

1. Installs ContextKit globally via the public installer (pinned to `CONTEXTKIT_REF`).
2. Copies the project body.
3. Installs the capabilities manager and all locked capability payloads.
4. Runs `contextkit init` to create target-local technical bindings (`.env.local`, guards).
5. Runs `capabilities init --codex --claude` to initialize project contexts.
6. Runs `contextkit install-hooks --target codex --target claude` to install hooks.
7. Runs `contextkit doctor` to verify the project is correctly configured.
8. Runs `contextkit build --target all` to generate host bindings and compile context.
9. Runs `contextkit audit` to validate the built context.

Generated host bindings (`.codex/generated/`, `.claude/rules/CONTEXT.md`),
the ContextKit manager binary (`.contextkit/manager/`), and machine-local
bindings (`.env.local`) are excluded from the Docker build context via
`.dockerignore`. These are target-local build artifacts, not deployment inputs
shipped from the repo.

The build fails if any step from contextkit init through contextkit audit fails.

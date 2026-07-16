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
the capabilities manager. If Telegram is enabled or already configured,
`setup` also creates a Telegram service in `docker-compose.yaml`.

`deployment setup --dry-run` shows which files would be written before changing
the project.

The human-facing questions are intentionally simple:

- What kind of thing is being deployed? Default: `agent-box`.
- Where should it run? Default: `coolify`.
- Should it run a Telegram assistant service? Default: `auto`.

After setup, `deployment next` prints the checklist for the chosen target:
which files were created, which secrets must be entered, local validation
commands, provider steps, and first-login commands for Codex, Claude, and
Telegram when present.

## ContextKit Projects

For projects with `.contextkit/config.toml`, the generated Dockerfile:

1. Installs ContextKit globally via the public installer (pinned to `CONTEXTKIT_REF`).
2. Copies the project body.
3. Installs the capabilities manager and all locked capability payloads.
4. Runs `contextkit install-hooks --target codex --target claude`.
5. Runs `contextkit build --target all` to generate host bindings and context.
6. Runs `contextkit doctor` to verify the installation.

Generated host bindings (`.codex/generated/`, `.claude/rules/CONTEXT.md`) and
the ContextKit manager binary (`.contextkit/manager/`) are excluded from the
Docker build context via `.dockerignore`. These are target-local build artifacts,
not deployment inputs shipped from the repo.

The build fails on missing installer, hook, doctor, or context-build steps.

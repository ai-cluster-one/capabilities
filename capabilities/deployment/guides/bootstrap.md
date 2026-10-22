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
Claude, and capability CLIs installed by the capabilities manager. Setup compiles deploy descriptors for capabilities
explicitly enabled in project settings. Descriptor-default services are embedded
in the agent container and supervised there; an explicit `enabled` override keeps
a capability as a separate Compose service. Global inheritance, installed
payloads, and existing service directories do not authorize runtime services.
Each embedded service runs under its runner.

That is files mode. In a project whose records are in the store (`"records": "store"` in `project.json`), setup compiles every installed capability service, and whether one runs on a node is the gate's answer there: `capabilities enable <name> --project --node <node>` turns it on without a redeploy (`deployment guide profiles`). There the Telegram and automations flags below exit 6 with `retired_option` for any value but `auto`.

`deployment setup --dry-run` shows which files would be written before changing
the project.

The human-facing questions are intentionally simple:

- What kind of thing is being deployed? Default: `agent-box`.
- Where should it run? Default: `coolify`.
- Which explicitly project-enabled capability services should run? Default:
  descriptor policy plus `service_policy.auto_include`, embedded for agent-box.

The Telegram and automations setup flags remain compatibility shorthands. They
write `enabled` or `disabled` overrides into `runtime.json`; `auto` leaves the
generic descriptor policy in control.

When services are embedded, setup generates `supervisord.conf`, installs
Supervisor in the image, and makes the entrypoint run it as PID 1. The generated
Codex installation uses the complete release package, including
`codex-code-mode-host` and runtime resources.

After setup, `deployment next` prints the checklist for the chosen target:
which files were created, which secrets must be entered, local validation
commands, provider steps, and first-login commands for Codex, Claude, and
Telegram when present.

## ContextKit Projects

For projects with `.contextkit/config.toml`, the generated Dockerfile neither
installs nor calls ContextKit; the capabilities manager does the whole setup:

1. Copies the project body.
2. Installs the capabilities manager and every capability in the catalogue.
3. Runs `capabilities init --codex --claude` to create target-local bindings
   (`.env.local`) and initialize project contexts.
4. Runs `capabilities doctor` to verify the manager and the capability set.
5. Runs `capabilities hooks install --claude --codex` to point both hosts'
   session hooks at `capabilities session-start`.
6. Runs `capabilities build` to generate host bindings and compile context.

Generated host bindings (`.codex/generated/`, `.claude/rules/CONTEXT.md`),
the ContextKit manager binary (`.contextkit/manager/`), and machine-local
bindings (`.env.local`) are excluded from the Docker build context via the
target's `Dockerfile.dockerignore`. These are target-local build artifacts, not
deployment inputs shipped from the repo.

The build fails if any step from capabilities init through capabilities build fails.

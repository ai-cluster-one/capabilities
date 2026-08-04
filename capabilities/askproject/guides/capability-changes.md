# Fixing a capability from another project

Use a local `capabilities dev` session for the iterative work. Use
`askproject` once the prepared capability commit already works in the consuming
project and needs final ownership review, integration, and publication.

This keeps the feedback loop local without blurring ownership: the consuming
project proves the behavior it needs; the capabilities repo remains the owner
of the published source and its final audit.

## Source, dev installation, and canonical installation

- **Source repo** — the Git checkout that owns capability source. Never edit the
  manager's installed registry.
- **Dev session** — a manager-created source worktree plus isolated HOME,
  registry, PATH bin, config, state, data, cache, and logs. An optional consumer
  worktree provides a second checkout for integration tests. This is process and
  state isolation, not a security sandbox.
- **Canonical installation** — the published version installed by the normal
  manager registry and linked onto the normal PATH. It changes only after the
  source owner publishes and the consumer runs `capabilities update <name>`.

The exact command contract lives in `capabilities help`; the operational model
and safety rules live in `capabilities guide dev`.

## Local development loop

From the consuming project, create a named session:

```sh
capabilities dev start <name> \
  --source <capabilities-repo> \
  --consumer <consumer-repo> \
  --session <session-id>
```

Use `--consumer` for normal development. It creates a consumer worktree and
therefore does not copy untracked files such as `.env`. Use `--consumer-live`
only when a deliberate final smoke test must see the live checkout.

Edit only the returned source worktree. Refresh the isolated installation after
source changes, then run focused checks inside the session:

```sh
capabilities dev install <session-id> <name>
capabilities dev exec <session-id> -- <name> doctor
capabilities dev exec <session-id> -- <original failing command>
capabilities dev doctor <session-id>
```

The child process receives an isolated environment. A credential or other
parent variable is available only when explicitly named:

```sh
capabilities dev exec <session-id> \
  --inherit-env REQUIRED_KEY -- <command>
```

The value stays out of argv. Prefer test credentials and session-specific ports
or resource names when the capability talks to a live service. `dev exec` runs
foreground processes; their lifetime is the lifetime of that command.

Run the capability's focused tests and commit the prepared source change on the
session's `dev/<session-id>` branch. Do not merge it into `main` from the
consumer project.

## Final owner handoff

Once the local behavior is proven, make one action request to the capabilities
repo owner. Include the session branch or prepared commit, the original failure,
the local test evidence, and the expected consumer smoke test:

```sh
askproject <capabilities-repo> --act \
  "Review and integrate prepared commit <sha> from dev/<session-id>. Run the repo-owned audit and tests, publish it to main if clean, and report the published commit. Evidence: <tests and smoke result>."
```

The owning agent inspects the prepared diff, applies doctrine, runs the complete
source index/check and relevant tests, integrates it, and publishes it. It is
not asked to rediscover and reimplement the change through a long iterative
conversation. If the owner finds a substantive defect, keep the session,
correct the prepared branch locally, and hand it back only after the focused
checks pass again.

## Canonical update and cleanup

After publication, update the normal installation and repeat the original
consumer smoke test outside the dev session:

```sh
capabilities update <name>
<original failing command>
capabilities dev stop <session-id>
```

`dev stop` removes only clean worktrees whose commits are absent, already merged,
or patch-equivalent to commits on the recorded base ref. Dirty or unpublished
work is retained. Use `capabilities dev list`, `dev doctor`, and `dev gc` to find
old sessions; garbage collection applies the same preservation rule.

# Fixing a capability from another project

You are working inside **another project** and have hit a deficiency in a
capability you have installed — a CLI bug, a missing verb, behaviour that is
wrong for the job. This guide is the round trip for getting that fixed and back
onto the machine, using `askproject` as the bridge into the capabilities repo.
Work done *inside* the capabilities repo follows its doctrine directly and has
no need of this.

## Source vs deployed copy

- **Source** — the development workspace where every capability is authored
  and changed. It lives in a checkout of the `capabilities` repo on your
  machine; this guide refers to that checkout's path as `<capabilities-repo>`.
  Every change to a capability is made there, against its contract and
  doctrine, and travels through normal git review.
- **Deployed copy** — the manager's installed registry, re-fetched from
  GitHub `main` on every `capabilities update` and symlinked onto `PATH`.
  Never edit in place: any edit there is overwritten on the next update. The
  fix goes in the source.

The deployed copy is the one you are running when you typed the capability's
name on your terminal. The source is the one you fix.

## The cycle

The full chain is implement → validate → push to `main` → reinstall → re-test;
the peer's `done` is the *start* of this chain, never the end. Until
`capabilities update` has put the new version on `PATH`, the change has not
arrived. Five beats:

### a. Implement in source via `askproject`

You do not have the capabilities repo open; you have your own project open.
Hand the edit to a peer Claude Code agent that owns the capabilities repo, by
calling `askproject` from your project with `--act`:

```sh
askproject <capabilities-repo> --act "<one-paragraph task description>"
```

The peer loads that repo's `CLAUDE.md`, doctrine, and rules, edits within its
contract, fenced to that directory, and returns a JSON envelope naming the
files it changed (`git.files_changed`) — read it back; refine with `-c` if a
second pass is needed. The peer's report means *edited in source* — not
shipped, not installed, not ready.

### b. Validate and sign off

Its own beat, never skipped. The change is validated before it lands, and
this can be delegated to a sub-agent that reviews it and returns a verdict —
a read-mode `askproject <capabilities-repo> "review the change…"`, a
`--schema` structured pass/fail, or a reviewer sub-agent of your own.
Optionally smoke-test the in-progress source on `PATH` first with
`capabilities install <name> --from <capabilities-repo>` and exercise the
case that failed. Validation produces the sign-off that the change is correct
and ready to ship.

### c. Land it in the repo

Once signed off, the change is committed and pushed to GitHub `main`; the
deployed copy only ever comes from `main`. The push is the user's gate: the
outside agent does not push — it hands the validated change to the user (what
changed, where, what validation showed) and stops.

### d. Reinstall via the capabilities manager

```sh
capabilities update <name>
```

This pulls the published version into the manager's canonical location and
relinks `PATH` if the link is not already current (if no relink is needed,
the manager simply does nothing extra). This is the *only* step that makes
the new version the one the machine actually runs.

### e. Consume and re-test

Then, and only then, the calling project can claim it is ready to use the
updated capability; re-test against the original failure. A failure here is a
regression to report back, never a reason to edit the deployed copy. Until
`capabilities update` has completed, the change has not arrived: the peer's
`done`, a green local smoke test, even a merged PR are not the same as the
manager having reinstalled it onto `PATH`.

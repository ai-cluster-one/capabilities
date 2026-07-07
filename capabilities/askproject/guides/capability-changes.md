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
arrived. Three beats:

### a. Delegate the edit to the capabilities repo agent

**From your outside project**, delegate the implementation to a peer Claude
Code agent that owns the capabilities repo, via `askproject` with `--act`:

```sh
askproject <capabilities-repo> --act "<one-paragraph task description>"
```

The peer loads that repo's `CLAUDE.md`, doctrine, and rules, implements the
change in the **source**, validates it per the repo's audit/test requirements,
commits it, and pushes to `main`. The peer's JSON envelope reports what
changed (`git.files_changed`) and the validation result. The peer **must not
stop** after merely editing source — it owns the full cycle through commit and
push to `main`. The outside agent never edits capability source directly; that
is the capabilities repo agent's domain.

### b. Reinstall via the capabilities manager

Once the peer has pushed to `main`, **back in your calling project**, pull
the published version and relink `PATH`:

```sh
capabilities update <name>
```

This fetches from GitHub `main` into the manager's canonical registry and
relinks the symlink on `PATH`. This is the *only* step that makes the new
version the one the machine actually runs.

### c. Re-test on PATH

Then, and only then, the calling project can claim it is ready to use the
updated capability; re-test against the original failure. A failure here is a
regression to report back to the capabilities repo agent, never a reason to
edit the deployed copy. Until `capabilities update` has completed, the change
has not arrived: the peer's `done`, even a merged PR, are not the same as the
manager having reinstalled it onto `PATH`.

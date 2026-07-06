# Routine — harness surfacing

The routine index every session sees is generated, never hand-edited. Each harness compiles it at session start; routine bodies stay on demand.

## Claude Code

A consuming project can wire a `SessionStart` hook that finds every `.routines/**/*.md`, reads `name` and `description`, and writes an always-on routine index. The index is deterministic and should only change when a routine is added, removed, renamed, or redescribed.

## Codex

A Codex project can compile its project context from the same generated routine index. The Codex context compiler may regenerate the routine index first, then include it in the project document. Generated context files are host materialization, not hand-authored doctrine.

## Regeneration Caveat

Session-start regeneration may affect the next agent session rather than the session that triggered it. When validating a change inside the same run, call the compiler or `routine index` directly instead of assuming the live model context has already changed.

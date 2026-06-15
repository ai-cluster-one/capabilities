# Implementation handoff — `capabilities sync-contract` (vendor the shared contract preamble)

**Branch:** `main` (protocol v2 is now the primary line).
**Status:** mechanism built + all 10 connection-bearing caps migrated and pushed. A fresh session continues from "Remaining" below.

## Progress (current)

- **Built** (commit `c2f102a`): `contract/preamble.py` (two fenced tiers — `capability core` for all caps, `connections` opt-in); `capabilities sync-contract [--check]` stamps the fenced interiors; `capabilities audit` byte-checks each fence vs canonical and fails on drift (exit 7). Markers: `# >>> contract: <tier> (generated …) >>>` / `# <<< contract: <tier> <<<`. Manager excluded — recorded in `.claude/rules/manager.md`.
- **Decisions locked**: drop mailbox/telegram custom write-gate wording (canonical message); drop mailbox `mailboxes` alias; `_emit` = empty-string-guarded variant; `_select_connection` folds mailbox's `address`-match (no-op without an `address` field); drift = hard audit failure; manager never participates.
- **Proven** (commit `ea11bd3`): **mailbox** + **fathom** migrated (vendored helpers excised, per-cap connection helpers preserved, drift-free, `audit` green). mailbox reinstalled; deployed PATH copy verified green against the simplbooks consumer (connections + address-select + doctor).
- **Migrated** (commit `4d1be41`, pushed): the remaining 8 — **asana, callva, directo, notion, railwayc, stripe, telegram, windmill** — via 8 parallel sub-agents (each touched only its own file, left empty fences; central `sync-contract` stamped, race-free). Verified: `sync-contract --check` drift-free + idempotent; `audit` green for all 8 (contract surface run end-to-end via uv); function-name inventory identical HEAD→now per cap (no domain code lost). NOT yet installed in any consumer — the simplbooks install/migration is a separate handoff.
- **Verification loop**: migrate → `sync-contract` → `audit <cap> --from .` → commit → reinstall (`install <cap> --from capabilities/<cap>/bin/<cap>`) → verify in a real consumer **as a sub-agent** (never pollute main context).

## Remaining

- All 10 connection-bearing caps are migrated (✓ mailbox, fathom, asana, callva, directo, notion, railwayc, stripe, telegram, windmill) and the `PROTOCOL = 2` banner fixes are done.
- **Install + migrate in the simplbooks consumer** — separate handoff: for each connection-bearing cap the consumer uses, reinstall from this repo's protocol-2 source and migrate its envelope. (User runs this in the simplbooks project from a handoff prompt.)
- Core-only caps (`capability core` fence alone, no connections fence): mail; simplbooks + whatsapp join after Tasks S/W per `capability-contract-standard.md`. These stay protocol 1 until their own passes bump them.

---

(original brief follows)

## Why

Every capability CLI (`capabilities/*/bin/*`) hand-copies ~150 lines of identical plumbing — the env-file parser, project-root walk, credential cascade, generic connection-envelope handling, the gate, `_emit`/`_die`, the contract-verb dispatch. They are **copied, not shared**, so a fix or improvement to that plumbing means editing ~13 files by hand, and nothing guarantees the copies stay identical (drift is silent).

The goal: **one home for the shared plumbing, with zero runtime coupling.** Copy-at-build, not import-at-run.

## Invariants this MUST preserve (do not regress these)

These are the properties the no-import rule buys (see `SHEBANG.md` "One file, uv run" + `DOCTRINE.md` rule 15, and SHEBANG's "spec, never a shared runtime library"):

1. **Single self-contained file.** Each `bin/<name>` stays one file runnable by `uv run` with no sibling imports. You can `scp` it to a bare box and it runs.
2. **No import from the manager or any shared lib.** A manager update must never be able to break a deployed capability. The mechanism is codegen (text stamping), NOT a Python import.
3. **Frozen per install.** The installed registry copy keeps whatever preamble it was stamped with at install time.
4. **Deviation stays legal.** A capability may legitimately differ in a helper (e.g. mailbox's `$MAILBOX_CONFIG` registry override; windmill's settings-tier in `_resolve_key_default`). The mechanism must let a capability opt a function out, recorded in its `deviations.md`.

If any step would break 1–4, stop and reconsider.

## The mechanism

1. **Canonical source** — `contract/preamble.py` (new, repo root): the one true copy of the shared helpers.
2. **Fence markers** in each `bin/<name>`:
   ```python
   # >>> contract preamble (generated — edit contract/preamble.py, run `capabilities sync-contract`) >>>
   ... vendored helpers ...
   # <<< contract preamble <<<
   ```
   Everything outside the fence is the capability's own code, never touched by codegen.
3. **`capabilities sync-contract`** — reads `contract/preamble.py`, replaces the fenced span in every `capabilities/*/bin/*` (and the manager's own copy if it uses the same helpers). Idempotent.
4. **Drift-check in `capabilities audit`** — each script's fenced block must byte-match the canonical; report drift as a failure (exit 7, like the other audit/doctor reconciles). This is what makes "copied" into "provably identical" and enforces that someone ran sync-contract.
5. **Override convention** — a capability that must diverge marks a function `# contract: override <fn>` just inside the fence; sync-contract preserves that function's body, the drift-check exempts it, and the divergence is recorded in that capability's `deviations.md`.

## THE CRUX — determine the exact common set first

The helpers are **not** all byte-identical today. Step zero is empirical:

1. Extract each candidate helper from all `capabilities/*/bin/*` and diff them across capabilities. Likely **fully common** (byte-identical given module-level constants): `_parse_env_file`, `_project_root`, `_project_env`, `_env_dir`, `_state_dir`, `_gate`, `_mask`, `_emit`, `_die`, `_resolve_env_key`, `_connections_registry`, `_select_connection`, `_key_report`, `_docs_base`, `_cmd_guide`, `_cmd_refs`, `_cmd_ids`. Probably also `_contract` and `_write_gate`.
2. **Per-capability, stays OUTSIDE the fence** (shape varies — do NOT vendor): `_build_conn`, `_resolve_conn`, `_cmd_connections`, `doctor`/`_check_connection`, the declaration constants (`NAME`, `SCOPE`, `CRED_KEYS`, `WRITE_VERBS`, `WRITE_DEFAULT`, `DOCS_BASE`, `TOPICS`, `STATE`, `POST_INSTALL`, and any capability-specific like fathom's `DB_FIELDS`), the domain commands, argparse/`main`.
3. Known partial-deviators — **all three pre-cleaned away** (see `connections-standard-handoff.md` §0), so re-confirm the common set is uniform before assuming the override convention is needed:
   - **mailbox** — `$MAILBOX_CONFIG` override + legacy `settings.json`-profiles detection **removed**; `_registry_envelope` now the standard envelope check.
   - **windmill** — settings-envelope tier **removed** from the credential cascade; `_resolve_env_key` standard; `deviations.md` deleted.
   - **railwayc** — 2-tier cascade **normalized** to the standard tiers; `_resolve_env_key` byte-identical to stripe's.
   - **callva** — settings-tier **removed** (same construct as windmill, narrower); `_resolve_env_key` standard; `deviations.md` deleted.
   The override-convention (`# contract: override <fn>`) therefore has **no known consumer**. Step-zero should confirm a uniform common set, then drop the mechanism (simpler canonical loader + stricter drift-check) rather than build it speculatively. The settings-tier cruft class is fully eliminated — no further pre-clean suspects.

The vendored block depends only on module-level constants (`NAME`, `SCOPE`, `CREDENTIALS_ENV`, `_CONFIG_HOME`, `_STATE_HOME`, `CRED_KEYS`, `WRITE_VERBS`, `WRITE_DEFAULT`, `DOCS_BASE`, `TOPICS`, `STATE`, `POST_INSTALL`) being defined **above** the fence. Document that contract at the top of `contract/preamble.py`.

## Steps

1. **Diff-and-extract** (the crux above). Produce the exact common-helper list and note every deviation.
2. **Write `contract/preamble.py`** — the common helpers verbatim, with a header docstring listing the module-level constants each capability must define above the fence.
3. **Pick fence markers** (above) and add a tiny parser: locate `# >>> contract preamble … >>>` … `# <<< contract preamble <<<`, replace the interior. Respect `# contract: override <fn>` regions.
4. **Add `capabilities sync-contract`** to `bin/capabilities` — iterate `capabilities/*/bin/*`, stamp each fenced region. Print which files changed. Idempotent.
5. **Add the drift-check to `capabilities audit`** — compare each script's fenced block (minus overrides) to canonical; failure on mismatch.
6. **Migrate the capabilities** — for each: insert fences around its shared region, delete the now-duplicated bodies, run `capabilities sync-contract`. Start with **two** (fathom + mailbox — mailbox exercises the address-match + envelope normalization hardest) to prove it, then the rest. While in each file, **correct the stale `(protocol 1)` banners**: the `# --- Capability contract (protocol 1) — shared verbatim ---` caption is replaced by the canonical `(protocol 2)` block on stamp; the `# --- Capability declaration (protocol 1) ---` caption above the per-capability constants is a one-line fix to `(protocol 2)` for every `PROTOCOL = 2` cap (asana, callva, mailbox, notion, railwayc, stripe, windmill; directo + telegram already correct). mail/simplbooks/whatsapp stay `(protocol 1)` until their migrations (S/W) bump them.
7. **Update `SHEBANG.md`** — it currently teaches these helpers as prose/code. Keep the rationale; change the code blocks to point at `contract/preamble.py` as the authority so the code stops living in two places (DOCTRINE rule 1). Add a short "contract preamble" subsection describing the fence + sync-contract + drift-check.
8. **Add a deviations note** anywhere a capability uses `# contract: override`.

## Verification

- `capabilities audit <name>` green (incl. zero drift) for every capability.
- `python3 -c "import ast; ast.parse(open(f).read())"` for each stamped script.
- `fathom doctor` + one other capability's `doctor` green (behaviour unchanged).
- `git diff` shows ONLY fenced regions changed in the migrated scripts — no domain code touched.
- Re-running `capabilities sync-contract` is a no-op (idempotent).

## Decisions to surface to the user before finalizing

- Marker syntax (the `# >>>` form above is a proposal).
- Whether the manager's own `bin/capabilities` participates (it shares some helpers) or stays hand-maintained.
- Whether the drift-check is a hard `audit` failure (recommended) or a warning.

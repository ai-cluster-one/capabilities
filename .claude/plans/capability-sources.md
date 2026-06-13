# Plan (preliminary): additional capability sources

**Status:** preliminary — captured for a dedicated session, not started.
**One-liner:** let the `capabilities` manager resolve installs/updates from more than one conformant source, with this repo as the default ("alma mater") and user-registered sources beside it.

## The idea

Today the manager has one implicit upstream: this repo. The goal is to let a
person who has the CLI installed **register additional conformant sources** on
their machine — their own capabilities repo, a team's, a third party's. After
that, `install` / `update` / `groom` work exactly as they do now; the only
difference is that capabilities start arriving from two or more places. This
repo + the manager stay the reference source and the convention's home; other
sources plug in beside it. No central marketplace — a local, user-owned set of
sources, the way a package manager registers extra registries.

## What already exists (the foundation is most of the way there)

The per-capability provenance plumbing is **already built** — this is a small
extension, not a rebuild:

- `SOURCE` (bin/capabilities:103) — the single default upstream, already
  overridable by env: `CAPABILITIES_SOURCE`.
- `_resolve_source(name, from_ref)` (bin/capabilities:320-343) — already
  resolves a local path, a local dir, **or an explicit `http(s)://` URL**, and
  falls back to `{SOURCE}/capabilities/{name}/bin/{name}`. It returns
  `(bytes, recorded-source-string)`.
- `meta.json` per installed capability (bin/capabilities:401-404) — already
  records the resolved `source` and `sha256` at install time. **Provenance is
  already tracked.**
- `cmd_update` (bin/capabilities:459-461) — already re-fetches from the
  recorded `meta["source"]`, so an installed capability already updates from
  wherever it came from. **Source-aware update already works.**

So a capability installed today with `--from https://other.example/...` already
re-updates from that URL. What's missing is only the *named registry* and the
*resolution-across-sources* step for bare `install <name>`.

## What's missing (the actual work)

1. **A named-source registry.** A small file (e.g. `~/.capabilities/sources.json`):
   each entry is `name → base URL` plus an ordered search list and a default
   pointer. Mirror the **connections** shape (DOCTRINE rule 18): deterministic
   selection — explicit flag, else default, else sole entry, else refuse as
   ambiguous.
2. **A `source` verb group:** `source add <name> <url>`, `source rm <name>`,
   `source list`, `source default <name>`. Reports without touching the network.
3. **Multi-source resolution for bare `install <name>`** (no `--from`): walk the
   registered sources in order until one serves the named capability; record the
   winning source into `meta.json` (already recorded — no change needed there).
   Add an explicit `--source <name>` selector for disambiguation.
4. **Collision handling** when a name exists in more than one source — explicit
   `--source`, deterministic order, refuse-as-ambiguous. Decide whether names
   namespace (`source/name`) or stay flat.
5. **Trust / pinning model.** Arbitrary-URL fetch-and-execute is the real risk
   surface. The bootstrap already has a `SHA256_PIN` concept (install.sh); extend
   per source — checksum/signature, or trust-on-first-use, plus how a user vets a
   third-party source before first install.
6. **Surface provenance** in `list` and `doctor` (which source each installed
   capability came from).
7. **Doc updates:** README reframed so this repo reads as *one* source (the
   default), not *the* source; a DOCTRINE rule for source resolution mirroring
   rule 18's selection discipline; SHEBANG only if the contract changes (it
   likely doesn't — sources are a manager concern, not a capability concern).

## Open design questions

- **What is a "source"?** A raw base URL (install-by-known-name only, like
  today), a git repo, or a source that publishes a **catalogue index** so the
  manager can *enumerate* what it offers (`list --source X`, discovery)?
  Enumeration needs an index endpoint; today install is by-known-name only.
- **Versioning/pinning** per source (tags? shas? like the bootstrap's TAG_PIN).
- **Trust bootstrapping** for a third-party source — TOFU, pinned checksum,
  signature?
- `groom` / `audit` operate on the installed local copy, so they probably stay
  source-agnostic — confirm.

## Suggested phasing

1. **Registry + `source` verbs + multi-source resolution for `install`**
   (by-known-name, no enumeration). Reuse the connections selection discipline.
2. **Catalogue index** per source → enumeration and discovery (`list --source`).
3. **Trust / pinning** model.

Implement in its own session; this is a v-next, not part of the current pass.

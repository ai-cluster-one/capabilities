# Handoff: the v2 connection standard + the migrations it implies

Self-contained brief for a fresh agent (or this session after compaction). It states the
**consolidated connection standard going forward**, then the concrete work that brings the
laggards into line. Repo: `/Users/kz/dev/capabilities`, branch **`main`** (protocol v2 is the
primary line now).

## 0. Current state / what's already done

- `fathom` capability shipped and installed; `doctor` green. README catalogued.
- Manager scaffold bug fixed: credential notes now write on their own `#` line, never inline after `=` (the cascade parser can't strip inline comments because `#` can be a secret char).
- `fathom/deviations.md` **deleted** — it recorded no real rule-departure (multi-secret connection, non-HTTP backend, remote store are all standard).
- **Deviation pre-clean done** (ahead of Task C): three perceived deviations were investigated and found to be cruft / over-recording, not rule-departures —
  - **windmill** — settings-tier removed from the credential cascade (`_resolve_key_default` collapsed into the standard `_resolve_env_key`); `deviations.md` deleted. `folder`/`operator` stay in `_config_value` (legitimate non-secret config). `doctor` green live.
  - **railwayc** — cascade normalized (user `credentials.env` tier restored; `_resolve_env_key` byte-identical to stripe's); only the "2-tier" section dropped from `deviations.md` — its genuine entries (subprocess transport, transparent forwarding, write-verb classification, refuse-ambient-auth, doctor-via-status/exit-7) remain. `SCOPE=project` kept for scaffolding; `expect_project` guards mis-scope.
  - **mailbox** — `$MAILBOX_CONFIG` override + legacy `settings.json`-profiles detection removed; `_registry_envelope` simplified to the standard envelope check; dead `_project_settings` removed. Registry resolution is now the standard two-home walk. The two live consumer configs migrated to `connections.json` (agentic-dev: 1 conn, default `jess`; simplbooks: 5 conns, **no default** — explicit `--connection` required), legacy `settings.json`/`mailbox.json` deleted.
  - **Implication for Task C:** the override-convention (`# contract: override`) now has **no known consumer** — confirm at step-zero diff and prefer dropping the mechanism (cleaner canonical loader, stricter drift-check) over building it speculatively.
  - **callva** — the same settings-tier cruft as windmill, narrower (only `api_url`, the API base endpoint; the secret stayed clean) and dead on disk; now removed: `_resolve_key_default` folded into the standard `_resolve_env_key`, dead `_project_settings` removed, docstring renumbered to 4 tiers, `deviations.md` deleted. audit green. **callva held the last settings-tier; that cruft class is fully eliminated.**
- Sibling handoffs in `.claude/plans/` (untracked working notes):
  - `sync-contract-implementation.md` — the enforcement mechanism (Task C below); read it for full detail.
  - `simplbooks-migration-handoff.md` — a **different** migration (the simplbooks *project's* `.capabilities/` envelope from old-markdown → v2). **Do not conflate** with Task S here, which refactors the simplbooks *capability's code*.
- Authoritative shapes to read first: `SHEBANG.md` (sections "Connections", "The credential cascade", "State"), `DOCTRINE.md` (rule 18, rule 4, the "Deviations" section), `TEMPLATE.md` ("Settings vs identifiers vs reference"). The reference implementation for login-based connections is **`capabilities/directo/bin/directo`** — read it before Task S.

## 1. The base going forward — the consolidated connection standard

State these affirmatively; they refine (not replace) the existing doctrine.

1. **`connections.json` is the universal front door for any credentialed capability** — the same `{"default": "<id>", "connections": {"<id>": {…}}}` envelope, the same two-home resolution (project `.capabilities/<name>/connections.json` first, else user `$XDG_CONFIG_HOME/<name>/connections.json`, first-found-wins), regardless of *how* auth works: bearer key, API token, username/password→session, browser-scrape login, MTProto — all the same registry.
2. **The connection entry holds the inputs.** Non-secret wiring sits literally in the entry; every secret is named by env-key indirection (`secret_env`, or several `*_env` fields — multiple secrets per connection is standard). No secret is ever inline.
3. **What auth mints is per-connection STATE.** A session cookie, an exchanged token — keyed `<state-dir>/<connection-id>/…` (SHEBANG "State": *"keys its state per connection"*). The request layer resolves: for connection X, is there a stored token? → use it; else mint it from X's credentials, store it, use it. Switching connections switches which stored token is used.
4. **`settings.json` is non-secret behavioral config only** (tuning, default folders, `messages_dir`) — never endpoints or identities. Those are connections.
5. **Deviations are rare and narrow.** A `deviations.md` records a departure from a SHEBANG/DOCTRINE *rule*, and only that — never a connection's ordinary properties (multi-secret, an auth mechanism, a non-HTTP transport), never an archetype's standard traits (see 7), never "this capability is unusual." If the standard or an archetype can express it, it is not a deviation.
6. **Connection-less capabilities are first-class.** A capability with nothing to connect to yet is a thin contract-only citizen — `stub`/`manifest`/`ids`/`refs`/`doctor`, no domain verbs, no connections — a uniform v2 citizen that grows later. "No connections" is only a gap when the capability *needs* named multi-connection and lacks it. The declaration surface those verbs form is its own universal floor, standardized the same vendored way as connections — see `capability-contract-standard.md` (the **capability core** tier vs the **connections** tier).

7. **Connection archetypes are part of the contract, not deviations.** A credentialed capability's connection follows one of a small set of *archetypes*, each with standard, expected traits — so those traits are contract, never a `deviations.md` entry:
   - **API** (default) — a bearer token / API key; the secret *is* the credential, resolved by `secret_env`. Most capabilities (asana, notion, stripe, fathom, windmill).
   - **CLI-wrapper** — wraps another CLI and forwards commands to it; the secret is a token handed to the child process' env; stdio and exit codes pass through, so the JSON I/O contract and exit taxonomy apply only to the wrapper's own layer (auth / doctor / not-on-PATH). e.g. railwayc (wraps `railway`), askproject (wraps `claude`).
   - **Web-session** (browser emulator) — no public API; the secret is username+password (or a scraped session); auth *mints* a session cookie/token persisted as per-connection STATE, refreshed on expiry (resolve-or-mint-then-reuse, point 3). e.g. simplbooks, directo; telegram's MTProto session is the same shape.
   A capability **declares its archetype**; the archetype's traits are standard, not a departure. This reclassifies most current deviation files into archetype traits: railwayc's forwarding/transport/refuse-ambient-auth → **cli-wrapper**; directo/telegram/simplbooks' login ceremony → **web-session**. A `deviations.md` then records only a departure from *even the archetype's* norms. **This is the spine of Task D.**

The enforcement of "one loader inherited by all" is Task C (`sync-contract`): the connection-loading helpers become a single vendored canonical block, drift-checked, so divergence can't be introduced silently.

## 2. Conformance snapshot (where each capability stands today)

- **Fully conforms:** asana, mailbox, notion, stripe, **windmill** + **callva** (settings-tier removed from both), **railwayc** (cascade normalized; its remaining `deviations.md` entries are transport/forwarding/auth-model = cli-wrapper archetype traits).
- **Conforms, login-based (web-session archetype, the Task-S template):** directo (connections + `username_env`/`password_env` + minted session cookie in state home, per connection), telegram (MTProto session per connection).
- **No settings-tier suspects remain:** windmill, callva, and railwayc were the recorded settings/cascade deviators; all normalized. The `deviations.md` files that remain (directo/telegram/simplbooks login ceremony; railwayc's wrapper traits) become **archetype traits** under Task D (point 7), not deviations.
- **Connection-less by nature (fine):** mail (macOS grant, no resolvable credential).
- **Laggards to migrate:** **simplbooks** (Task S — has stateful session but no connections registry; single implicit identity), **whatsapp** (Task W — a parallel "profiles in settings.json" scheme that is connections in all but shape).
- **Out of scope:** askproject (meta-tool).

---

## Task S — simplbooks: adopt the connections registry + per-connection session

**Why:** simplbooks logs in with username+password and mints a session, but keys it per-email with no registry — so it can't hold multiple named identities (different SimpleBooks instances, different users/privileges). directo already does exactly the target pattern; copy it.

**Current state (read these in `capabilities/simplbooks/bin/simplbooks`):** `SCOPE = "project"`, `CRED_KEYS` (the email/password keys), `STATE = True`, `_session_file(email)` keyed per-email (`session.<tag>.env`), scrape caches (`CLIENTS_CACHE`, etc.), the `login`/PIN ceremony. No `_connections_registry`/`_select_connection`.

**Reference implementation:** `capabilities/directo/bin/directo` — its `_connections_registry`, `_select_connection`, `_resolve_env_key`, `username_env`/`password_env` resolution, login ceremony that persists the session into the per-connection state home, and `connections`/`doctor` verbs. Mirror its structure.

**Target:**
- Add the standard connections registry (both homes) + `_select_connection` + `--connection` flag on every domain verb + a `connections` report verb.
- Connection entry interior: non-secret identity inline (the login email, any account/company id), password by `secret_env` (e.g. `SIMPLBOOKS_<ID>_PASSWORD`). The implicit `default` resolves the existing `CRED_KEYS` from the cascade (unchanged for a single-account install).
- **Re-key session state per connection-id**: `_state_dir()/<connection-id>/session.env` (and the scrape caches likewise), replacing the per-email keying. Login mints into the selected connection's dir; a request for connection X uses X's stored session, or logs in from X's creds if absent/expired.
- `doctor` examines every connection (cheapest authed round-trip per id), re-authenticating an expired session before reporting.
- `connections.json` example to support in help:
  ```json
  {
    "default": "juko",
    "connections": {
      "juko":  { "login_email": "<email>", "secret_env": "SIMPLBOOKS_JUKO_PASSWORD", "allow_write": true },
      "other": { "login_email": "<email>", "secret_env": "SIMPLBOOKS_OTHER_PASSWORD", "allow_write": false }
    }
  }
  ```
- **The narrowed deviation** (this is the *only* legitimate one, same as directo's): auth is a scraped browser login — no API token — so the "secret" is username+password and the minted artifact is a session cookie persisted as per-connection state, and the exit-code/IO surface follows the scrape (document precisely what departs and why). Everything about *using connections* is standard and is NOT in the deviation file.

**Verify:** `capabilities audit simplbooks --from <path>`; `simplbooks connections` (resolution, secrets masked, both a single-account default and a 2-connection registry); `simplbooks doctor` per connection; log in under one connection, run a read under another, confirm sessions don't cross; existing single-account behavior unchanged when no registry is present.

---

## Task W — whatsapp: normalize to the standard connections.json

**Why:** whatsapp is connections in spirit (a `default` pointer + named entries with `base_url` + `secret_env`) but wears a parallel coat. Reference the real file `/Users/kz/dev/simplbooks/.capabilities/whatsapp/whatsapp.json`:
```json
{ "default": "kz-personal", "messages_dir": "messages",
  "kz-personal": { "base_url": "https://waha.callva.one", "session": "default",
                   "number": "...", "mode": "read", "secret_env": "WAHA_WHATSAPP_API_KEY" } }
```
**The five incidental deltas to remove** (current code in `capabilities/whatsapp/bin/whatsapp`: `CONFIG_REL = .capabilities/whatsapp/settings.json`, `LEGACY_CONFIG_REL = …/whatsapp.json`, `$WHATSAPP_CONFIG`, bespoke profile discovery, no registry helpers):
1. **Filename** `settings.json`/`whatsapp.json` → `connections.json`.
2. **Envelope** flat top-level profiles → nested under a `"connections"` key.
3. **`messages_dir`** (a setting) → move out to `.capabilities/whatsapp/settings.json`.
4. **Discovery** single-home + `$WHATSAPP_CONFIG` → standard two-home registry (project → user).
5. **Plumbing** → adopt the shared `_connections_registry`/`_select_connection`/`_resolve_env_key` + a `connections` verb; `--connection` flag (keep `--profile` as an alias if desired).

**Also:** map `mode: "read"|"send"` → the standard write gate (`allow_write`: a `send` connection is `allow_write: true`); the unimplemented `send` stays a gated reservation but is expressed as the write gate, not a bespoke mode.

**Target shape** (`.capabilities/whatsapp/connections.json`):
```json
{ "default": "kz-personal",
  "connections": { "kz-personal": { "base_url": "...", "session": "default",
                                     "number": "...", "secret_env": "WAHA_WHATSAPP_API_KEY",
                                     "allow_write": false } } }
```
with `messages_dir` (a relocatable bulk-store root) in `settings.json`.

**Delete `whatsapp/deviations.md`** once normalized — the multi-identity need it cites is exactly what the registry handles, so the deviation dissolves. (Keep a migration note only if reading the legacy `whatsapp.json`/`settings.json` filename for back-compat is retained — and prefer NOT to retain it; the project config gets rewritten to `connections.json`.)

**Verify:** `capabilities audit whatsapp --from <path>`; `whatsapp connections`; `whatsapp doctor`; a read against the migrated `connections.json`; confirm `messages_dir` still resolves from `settings.json`.

---

## Task D — doctrine patch (draft for review, do NOT auto-apply)

Capture points 1–7 in the doctrine surface. The **connection archetypes (point 7) are the spine** — they convert most existing `deviations.md` content into named, contract-level traits:
- **Connection archetypes** — enumerate **API** (token/key), **CLI-wrapper** (forwards to a child CLI; stdio/exit-codes pass through), and **web-session** (login+password → minted session persisted as per-connection STATE). A capability declares its archetype; the archetype's traits are contract. Reclassify the existing deviation files accordingly: railwayc's forwarding/transport/refuse-ambient-auth → cli-wrapper traits; directo/telegram/simplbooks' login ceremony → web-session traits. Consider a declared `ARCHETYPE` constant + contract-level support.
- **SHEBANG.md "State"** — make the *per-connection session-token* pattern explicit (the web-session archetype's mechanism): connection inputs live in `connections.json`; what login mints is state keyed by connection-id; the resolve-or-mint-then-reuse loop. (directo is the embodiment.)
- **DOCTRINE.md / SHEBANG.md** — add the **connection-less thin-citizen** shape as first-class: contract verbs only, no connections until needed; "no connections" is not non-conformance.
- **DOCTRINE.md "Deviations"** — sharpen: a deviation is a *rule-departure* only; connection properties and archetype traits are never deviations. (fathom's deleted file is the worked example of over-recording corrected.)
Present as a diff/proposal to the user; this is policy and wants review before landing on `main`.

## Task C — sync-contract (enforcement; full detail in `sync-contract-implementation.md`)

The connection loader (`_connections_registry`, `_select_connection`, `_resolve_env_key`, the cascade, `_emit`/`_die`, the gate, the `connections` report) is the canonical vendored block: one source `contract/preamble.py`, stamped into each `bin/<name>` between fence markers by a new `capabilities sync-contract` verb, with an `audit` drift-check. Copy-at-build, not import-at-run — preserves single-file portability, per-install freeze, and legal deviation. This is what makes "one loader inherited by all" structural rather than a hand-policed convention. **Sequence:** ideally land Tasks S and W *after* Task C exists (so they inherit the canonical loader), or do S/W first and re-vendor — either works; note the choice.

## Global done-criteria

- `capabilities audit <name>` green for simplbooks, whatsapp (incl. zero drift once Task C lands).
- Every credentialed capability resolves through the standard two-home `connections.json`; the only `deviations.md` files that remain document genuine rule-departures (directo's/telegram's/simplbooks' login-ceremony, callva's/windmill's settings tier, railwayc's 2-tier).
- `settings.json` holds no endpoints/identities anywhere.
- Connection-less capabilities (mail, future empties) are thin contract-only citizens, unflagged.

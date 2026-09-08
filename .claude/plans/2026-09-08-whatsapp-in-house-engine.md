# Plan — WhatsApp on an in-house engine

**Branch:** `main`. **Status:** researched and de-risked on a live account; not started.
**Supersedes:** the WAHA/GOWS operational model in `capabilities/whatsapp/guides/`.

## The premise

The `whatsapp` capability today consumes a self-hosted WAHA bridge: a separate
server, a Docker deployment, an API key, and tier limits on concurrent sessions.
Replace it with an engine the capability installs itself, so a consuming project
needs nothing but the CLI.

The engine is **whatsmeow**, reached from Python through `neonize`, which ships
it as a compiled shared library inside the wheel. This is not a downgrade from
WAHA: WAHA's own GOWS engine *is* whatsmeow, so the change removes the REST
wrapper and the server while keeping the engine already in use.

## What the spike established

Measured against a live linked account, not inferred.

- **Pairing works headless.** Phone-number pairing code preferred; QR available.
  Session persists, reconnects without re-pairing.
- **History arrives once, at pairing, and is shallow.** Five chunks in ~13s:
  400 chats, 1740 messages. 365 of those chats got 5 messages or fewer; exactly
  one got real depth. `endOfHistoryTransfer` was set nowhere.
- **Reconnect delivers no history at all.** Zero chunks, `OfflineSyncCompleted
  Count: 0`. Miss the burst and it is gone short of re-pairing.
- **whatsmeow stores no messages.** Its SQLite holds device, sessions, identity
  keys, contacts (1346), LID map, message secrets, chat settings — and no
  message table. The engine is transport; the store must be ours.
- **Downtime is covered by the protocol — measured, not assumed.** With no client
  connected, two messages were sent; on the next connect both arrived and
  `OfflineSyncCompleted` reported `count=3`. The server holds what a linked
  device missed and hands it over on reconnect. This is what makes a
  daemon-less design viable: `sync` is a real substitute for a listener, not a
  compromise.
- **Media has a retention window, and it is shorter than first measured.** A
  photo 0.4 minutes old downloaded and decrypted cleanly (299 KB JPEG). The
  first sweep found `403` on eight of eight items aged 65-69 days, but two
  later passes over the same account narrowed it considerably: **voice notes
  return `410 Gone` at 25 days**, and images fetch cleanly at 28 days while
  returning `403` from 41 days. The boundary therefore differs by media type
  and is closer to weeks than to months. Keys stay valid; the ciphertext leaves
  WhatsApp's CDN. Capture media close to arrival or record the loss.
- **`endOfHistoryTransfer` does not mean "this chat is complete".** The boolean
  says only that a *transfer* finished; whether more remain on the phone lives
  in the sibling `endOfHistoryTransferType`
  (`COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY = 0` /
  `COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY = 1`). Reading the boolean
  alone as completeness silently disables reach-back for every chat the pairing
  burst touched. The spike reported it as a "complete" column and was lucky that
  it was zero everywhere.
- **Backfill only walks backwards.** `BuildHistorySyncRequest` answers with
  messages *preceding* an anchor the caller already holds; there is no
  "fetch newer" counterpart. Forward coverage comes from the offline queue, not
  from paging.
- **On-demand backfill works and is deep.** Anchored paging returns 50 messages
  per request in ~4s. One group went 728 -> 3619 messages and from 3 months to a
  full year, in ~3.5 minutes of requests. The floor announced itself the way a
  real boundary does: a short chunk (42 of 50), then an immediate empty answer.
- **Media is recoverable.** `mediaKey`, `fileEncSHA256`, `fileSHA256` are present
  in the raw history chunks; voice notes carry the PTT flag and duration.
- **The send surface is complete.** Text, reply, edit, revoke, reactions, all
  media kinds, albums, polls, presence/typing, group management, newsletters.

## The dependency

Stock `neonize` binds neither `BuildHistorySyncRequest` nor `SendPeerMessage`, so
on-demand backfill is unreachable from Python. A pure-Python workaround does not
exist: the `Peer` flag routes through a different node structure that a plain
self-send does not reproduce.

- **Upstream PR:** krypton-byte/neonize#215 — adds `RequestHistorySync` (Go
  export, ctypes binding, `NewClient.request_history_sync`) and a generic
  `SendPeerMessage` export. The second one matters beyond history: peer messages
  carry the whole `PeerDataOperationRequest` family, and whatsmeow builds only
  one of them. With the generic export any of them can be constructed from
  Python — `FULL_HISTORY_SYNC_ON_DEMAND` (a time window rather than an anchor),
  `PLACEHOLDER_MESSAGE_RESEND` (the remedy for undecryptable messages),
  `HISTORY_SYNC_CHUNK_RETRY`.
- **Fork:** `ai-cluster-one/neonize`, branch `history-sync-on-demand`, one commit
  on top of upstream `master`.
- **If the PR merges,** drop the fork and depend on the release.
- **Until then,** the fork's CI (inherited `release.yml`: android/zig/linux/darwin)
  builds wheels published as GitHub Release assets; the capability's PEP 723
  header points at the wheel URL. No Go on the consumer side.
- **Gotcha to preserve:** `neonize` verifies `GetVersion()` against
  `__GONEONIZE_VERSION__` and silently downloads the official library over a
  mismatched one. A fork build must carry the matching version string.
- **Exit trigger:** if our patch set grows past three or four functions, or
  upstream stalls for months, replace neonize with our own sidecar over
  whatsmeow. Note the bus factor — neonize is effectively one maintainer.

## Scope of this iteration

**No daemon, no service, no long-running process.** The capability connects an
account, captures what pairing hands over, backfills further history on demand,
reads from its own store, and sends under the write gate. Every verb is a
short-lived invocation that connects, does its work, and exits.

A listening service that reacts to incoming messages — the Telegram-style
assistant — is a **separate later iteration**, and the design below is arranged
so it can be added without rework: it would take over the same store and the same
capture path, just holding the connection open instead of opening it per call.

What that costs, stated plainly: between invocations nothing is captured live.
The store advances only when a verb runs. `sync` is therefore the load-bearing
verb — it connects, drains whatever the server holds for the linked device,
writes it down, and disconnects. How often it runs is the consuming project's
business: by hand, from a routine, or on a schedule.

## Architecture

```
phone <--- backfill: anchored, 50/request, paged --------.
                                                          |
WhatsApp --- pairing burst + whatever the server holds ---+--> store --> verbs
                                                          |              |
                                                   raw chunks       export files
```

Three writes, one store:

- **Pairing capture.** The one-shot burst, written before it can be lost.
- **`sync`.** Connect, take what the server has for this device, write, exit.
- **`backfill`.** Anchored on the oldest message held for a chat, pages backwards.

And one read: a caller asks for the last N of a chat and gets it from the store
immediately — the adjustable context window, same shape as the Telegram
capability offers, without WhatsApp being in the request path.

Two properties the Telegram capability does not have, which must be modelled
explicitly rather than papered over:

- **The phone must be online** for backfill. Its absence is a distinct failure
  state and must surface as one, never as an empty result.
- **Backfill is asynchronous.** The request goes out, chunks arrive as events, so
  a verb that returns when the request was merely *sent* is lying to its caller.

## Surface principle

**No verbs invented for this engine.** The capability keeps the small standard
set it already has — `pair`, `chats`, `messages`, `contact`, `export`,
`transcribe`, `render`, `send`, plus the contract verbs — and everything new is
expressed as flags on those. A surface you combine is cleaner to read, cheaper
to maintain, and means the move off WAHA is invisible to a consumer.

Two things this rules out that were nearly built:

- **No `backfill` verb.** Depth is a parameter of reading:
  `messages <chat> --limit 500 --since 2026-01-01`. The CLI answers from the
  store and reaches for more when the store cannot cover the request. Splitting
  one action across two verb names buys nothing.
- **No `sync` verb.** The server's queue for an absent device is drained by the
  act of connecting, at no cost. So any verb that already goes to the network
  collects what accumulated. A "synchronise" command would name an
  implementation detail rather than a user's intent.

## Capture depth is a pairing-time choice, and it is separable from reach

Measured on two consecutive pairings of the same account:

| pairing config | initial burst | store |
|---|---|---|
| defaults | 1 740 messages / 400 chats | — |
| `requireFullSync`, 3650-day limit | **16 586** messages, back to 2016 | 6.1 MB + 8.9 MB raw |
| `requireFullSync`, 30-day limit | 1 785 messages | 624 KB |

The `FULL` chunks are what the day limit governs; at 30 days it arrives empty.

Crucially, **a shallow pairing does not cap later reach**: from the 30-day
pairing, anchored paging walked 15 rounds of 50 straight past the window, from
2026-06-10 back to 2026-02-24. Declaring the capability and taking the data are
therefore independent decisions.

So the default is **shallow at pairing, deep on request**. Copying a decade of
someone's messages to disk because a CLI was installed is disproportionate to
what was asked for, and it matches how the repo already treats history — a
project registers the conversations it cares about. Storage is not the argument
(20 MB for ten years); reversibility is: depth can always be added later, and an
archive already written cannot be unwritten. A connection may opt into a deep
initial sync where a consumer genuinely wants it.

The cost to state honestly: deepening runs about 50 messages per 4 seconds, so
warming a 4 000-message chat is roughly five minutes of requests. Reactive
depth is not instant.

## Device identity

The capability appears in the user's Linked Devices list, and that entry is the
only place a human sees what is attached to their account. Two `DeviceProps`
fields govern it: `os` is the displayed name, `platformType` picks the icon.
Leaving `platformType` unset lands on `UNKNOWN`, which WhatsApp renders as
"Other device" — the capability must set both, and name itself recognisably.

`DESKTOP` is the honest category: this runs on a machine, not in a browser and
not through Meta's Cloud API.

## The connection entry loses its secret

Worth stating because it changes the capability's credential story rather than
just its fields. A WAHA connection carries an instance URL, an `X-Api-Key` named
by `secret_env`, dashboard basic-auth, an engine and a tier. The in-house engine
has **none of them**: there is no server to address and no key to present. The
credential is the linked-device session, which is *minted state*, not configured
material — so it lands in the state home under rule 16 and never in a registry
or an env file.

What an entry holds instead:

- the **account binding** — the phone number, and the account JID once known, so
  a session that turns out to belong to a different account is refused rather
  than silently used. Telegram already does exactly this with
  `expected_account_id`, and the same reasoning applies: a personal messaging
  identity must be pinned deliberately.
- `messages_dir`, `allow_write` — unchanged from today.
- optional overrides: device name, initial sync depth.

The only secret the capability still declares is `DEEPGRAM_API_KEY`, and only
`transcribe` reads it. That is a marked simplification of the manifest and of
what a consuming project has to configure.

## Identities arrive in two forms and one of them is unreadable

Senders and chats come back as either a phone JID (`15550000001@s.whatsapp.net`)
or a LID (`100000000000001@lid`), and group participants in particular arrive as
LIDs. The engine store carried 471 LID mappings alongside 1 346 contacts, and
`GetLIDFromPN` / `GetPNFromLID` are bound.

Raw output is therefore not usable: the first browse of the capture listed real
conversations as `100000000000002@lid`, which tells a reader nothing. Resolving
LID to a contact — and to a display name — is a requirement of the output
contract, not a nicety, and the store must keep both forms so the mapping stays
reversible.

## Session lifecycle

Two failure modes that a daemon-less design must handle deliberately, because
nothing is watching between invocations.

**Remote unlink.** A person can remove the device from their phone at any time,
and several times during the spike we did exactly that. The next invocation must
report it as what it is — the session is gone, re-pair — with its own exit code,
never as an empty result or an obscure protocol error.

**Inactivity expiry.** WhatsApp drops linked devices that have not connected for
a while. A capability that only runs when a human asks may go weeks untouched,
and then the session is dead and re-pairing yields a fresh shallow burst — a gap
in the record that backfill can only partly repair. This is the strongest
remaining argument for *something* periodic, even without a listening service: a
routine that runs any verb occasionally keeps the session alive and drains the
queue. The exact horizon is commonly quoted as around two weeks and should be
confirmed rather than trusted.

## `doctor` and `health` need new subjects

`health` currently answers "is the WAHA instance ready" — with no instance, the
question dissolves and the verb must either be retired for this engine or
re-pointed at the phone's reachability, which is what backfill actually depends
on.

`doctor` keeps its role as the cheap "can I work now?" probe, but its chain
changes: the engine library loads, a session exists, it authorises, and the
account it authorises as matches the connection's binding. Nothing about a
server, a key, or a tier survives.

## Export stays a contract

`messages.json` plus `media/` is what consuming projects already read, and the
existing guides describe it. Rendering from the new store must produce the same
shape, so that the engine swap is invisible on that side too. Any change to the
export schema is a separate decision with its own migration, not a side effect
of changing how messages are fetched.

## Storage decision

### The engine swap changes the credential scope

`bin/whatsapp` currently declares `SCOPE = "project"`, and correctly so: a WAHA
instance URL plus API key belongs to a deployment, not to a person. The in-house
engine mints something else entirely — a linked-device session created by one
human scanning with one phone. That is the Telegram situation, so the declaration
becomes `SCOPE = "user"`.

This is not a preference. Rule 16 fixes the state home from the declared scope,
and `_state_dir()` already implements the resolution, so the location follows for
free once the declaration is corrected:

```
$XDG_STATE_HOME/whatsapp/<connection>/
├── session.db     # whatsmeow's own: device, keys, contacts, LID map
├── messages.db    # ours: chats, messages, enrichment
└── raw/           # history chunks as received, written before parsing
```

One store per connection, because a connection is one WhatsApp identity and two
accounts sharing a file muddies rule 15's single-writer question. The store is
user-scoped, so several projects consuming the same account share one capture
instead of each re-backfilling it.

The project side is unchanged: `capabilities/whatsapp/messages/<slug>/` stays the
**export**, project-scoped and git-ignored, holding the conversations a given
project registered.

### Specific in storage, converged at the contract

**Capability-local SQLite as the working store; files stay as the export surface.**

The current file model (`capabilities/whatsapp/messages/<slug>/messages.json` +
`media/`) is an *export* format — what a human or an agent reads. It is not what
a daemon writes into: the burst arrives as protobuf chunks spanning 400 chats at
once, backfill pages backwards and must dedupe by message id (an upsert), the
daemon writes continuously and concurrently, and enrichment needs a join key.

So:

- **Working store** — SQLite under the capability's state home, per connection,
  git-ignored. Written by the daemon and the backfill. Raw history chunks are
  written to disk *before* parsing, so a parser bug costs a re-ingest and never
  the data — this was proven necessary when the first capture's media keys were
  missing from the schema but recoverable from the raw chunks.
- **Enrichment in a sibling table**, keyed by message id: transcript, local media
  path, derived `effective_text`. Never mutate engine-written rows.
- **Media rows must carry their decryption material.** `download_media_with_path`
  fetches from `direct_path` plus `mediaKey`, `fileEncSHA256`, `fileSHA256` and
  the length, which is what lets media be fetched later from the store rather
  than only from a live message object. The first capture schema stored the path
  but not the keys and would have been unable to download anything; the raw
  chunks saved it. Store all of them.
- **Export unchanged.** `whatsapp export` renders a chat from the store into the
  existing `messages.json` + `media/` layout, so the consuming project's contract
  and the existing guides hold.

The schema mirrors WhatsApp honestly rather than aiming at a neutral shape,
because the protocol is not neutral: 107 message variants, LID identities
alongside phone-number JIDs (senders arrive as `100000000000002@lid`, and the
engine store carried 471 such mappings), a PTT flag separating voice notes from
audio files, per-chat `endOfHistoryTransfer`, sync-type provenance for every row,
and media keys. None of that has a Telegram analogue; a neutral schema would
either drop it or degrade into a JSON bag.

Convergence belongs one layer up, at the **output contract** the CLI already
publishes (`whatsapp contract`). A consumer building a context window should see
the same message envelope whether it came from Telegram or WhatsApp, and that is
a projection over storage — cheap to change, and reversible. Converging the
databases instead buys the same uniformity at the price of lock-in.

**Deliberately out of scope: a shared cross-capability message schema.** Nothing
in the repo stores messages in a database today; both `telegram` and `whatsapp`
export to files, and the shared SQLite (`store.py`) owns records — config, state,
connections, identifiers, context — not payloads. Designing a common message
model from one capability's sample is how a bad schema gets locked in, and rule 15
(one writer per collection) would have to be answered first. Keep the store an
implementation detail of this capability; if a shared model later earns its place,
migrating from a defined SQLite is easy, from scattered JSON it is not.

## Milestones

- **M0 — scope correction.** `SCOPE = "user"`, state home follows; verify the
  gitignore guard and `capabilities doctor` agree.
- **M1 — engine adapter.** `engine` on the connection selects `waha` (existing) or
  the in-house one. Both paths live simultaneously so the cutover is reversible.
- **M2 — pairing and session.** `pair` on the new engine (phone code, QR
  fallback), session in the state home per connection, `doctor`/`health`
  reporting the real chain. A lockfile makes concurrent invocations on one
  session impossible — two clients on one linked device cause desyncs and
  logouts.
- **M3 — store and capture.** Raw-chunk-first capture, SQLite schema, ingest,
  `reingest` from raw. Wire the pairing burst so it is never lost.
- **M4 — `sync`.** Connect, drain, write, exit. Reports what it took, so a caller
  can tell "nothing new" from "never ran".
- **M5 — `backfill`.** `whatsapp backfill <chat> [--count] [--rounds]`, anchored
  paging, waits for the answer rather than the send, phone-offline surfaced as
  its own state.
- **M6 — reads.** `chats`, `messages`, `search` served from the store, with the
  window a parameter. This is the surface a worker consumes.
- **M7 — media and transcription.** Bounded by the retention finding below:
  download what is still fetchable, record what is not, transcribe voice notes,
  populate the enrichment table and `effective_text`.
- **M8 — export and cutover.** Render from the store into the existing layout,
  migrate the connection registry, retire the WAHA guides.

Writes stay gated on `allow_write` throughout; the send surface is rich but a
WhatsApp send reaches a real person.

**Deferred to the next iteration:** the listening service — daemon lifecycle,
supervision, ownership across projects, live reaction to incoming messages.

## Open questions

Dropping the daemon settled several of these outright. What remains is recorded
with its evidence, including where the evidence is not yet clean.

### Settled by removing the daemon

- **Supervision.** No long-running process, so nothing to supervise, autostart or
  survive a reboot. Returns when the listening service does, and the answer then
  must be deliberate: the Telegram service is a bare `subprocess.Popen` with a
  pidfile and no launchd, which is tolerable only because Telegram's server holds
  the archive. WhatsApp's does not.
- **Ownership across projects.** No daemon to own. What remains is much smaller:
  a lockfile so two invocations never drive one session at once.
- **Backfill policy.** Not a lazy-versus-eager question any more. Backfill is an
  explicit verb with an explicit depth, invoked deliberately.

### Open, with evidence

**Settled: the offline queue works.** Two messages sent with nothing connected
both arrived on the next connect, `OfflineSyncCompleted count=3`. The
daemon-less design rests on solid ground: `sync` connects, drains what the
server held, writes, exits. The earlier `count=0` readings were an artefact of
two broken handlers of mine — millisecond timestamps parsed as seconds, then
`PushName` for `Pushname` — each of which swallowed the arriving message at the
ctypes callback boundary and looked exactly like "the server sent nothing".
Whatever the capability ships must treat a handler exception as a loud failure,
never as an absent event.

**Settled: media has a retention window.** Fresh media downloads and decrypts
(299 KB JPEG at 0.4 minutes old); media aged 65-69 days returns `403` on every
attempt. So M7 is bounded honestly: fetch media close to arrival, and for older
messages record the loss rather than pretend it is recoverable. The exact
boundary is unmeasured and not worth measuring precisely — the design
consequence is the same at 30 days as at 60.

**1. `FULL_HISTORY_SYNC_ON_DEMAND` did not answer.** The request was accepted and
nothing came back. Most likely the device never advertised `onDemandReady` /
`completeOnDemandReady`, which are announced in `DeviceProps` at pairing time.
Worth retrying with the flags set on the next pairing, because it is the one
mechanism that fetches by time window rather than by anchor — the natural repair
path if the offline queue ever proves to have a horizon. Not blocking: the queue
covers the forward direction today.

**3. Roughly 6.5% of captured messages were unreadable.** 99 `empty` plus 14
`placeholderMessage` out of 1740. `placeholderMessage` means the client could not
decrypt it; whatsmeow's remedy is `BuildUnavailableMessageRequest`, which — like
the history-sync pair — is not bound in neonize. Our patch does not cover it.
Decide whether a second export is worth the loss.

**Settled: distribution works, and CI is not a blocker.** The claim that the
fork's `release.yml` would try to publish `neonize` to PyPI was wrong — its
publish step is guarded by `if: env.UV_PUBLISH_TOKEN != ''`, so a fork with no
`PYPI_TOKEN` skips it. Only `secrets.PAT` is genuinely missing, and it is used
solely as a `repo-token` for the protoc installer action; `GITHUB_TOKEN` serves.

More usefully, the path was proven end to end without CI at all: `uv build`
produces a wheel carrying the patched shared library, and a PEP 723 script
declaring that wheel's URL installs and exposes every patched method. The
darwin-arm64 build is published as
`ai-cluster-one/neonize` release `v0.4.7-histsync.1`, so development can proceed
against a real URL today.

CI becomes worth wiring when a Linux deployment needs its own build — at that
point the fork's existing android/zig/linux/darwin jobs are reusable with the
PAT reference swapped. Pushing a workflow file needs the `workflow` token scope,
which the working `gh` token lacks.

**5. Rate limits at scale are unmeasured.** One chat on one account behaved well,
with a single transient stall. Backfilling hundreds of chats is a different
exercise and the failure mode is account-level rather than request-level. Less
acute without a daemon, since invocations are human-paced, but a `--rounds 200`
run is not.

**6. No test strategy.** The Telegram capability carries `tests/`. This plan says
nothing about what is tested, or how a protocol dependency gets covered without a
live account in the loop.

## Spike artifacts

The spike script (`wa-spike.py` — four engines, snapshot capture, browse,
anchored backfill) was lost with the scratchpad; its findings are recorded above
and the engine comparison does not need repeating. The linked session survives at
`~/.local/state/wa-spike/neonize/`. What is worth rebuilding, rebuild inside the
capability rather than as a spike.

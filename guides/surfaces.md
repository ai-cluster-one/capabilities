# Designing a capability's command surface

Use this guide when shaping the domain verbs, arguments, and help body of a new capability, or when reworking an existing one's surface.

It is a recommendation base, reasoned from what the catalogue already does and what it costs when it does otherwise. It is not law and it does not dictate. A capability that does not match it is not in violation and is not a bug: most of the catalogue was built before this was written, and a surface that works has earned its shape. One is adopted when a capability is next opened for a reason of its own.

Two registers run through it. **Settled** marks the few items where a departure actively misleads the caller — a wrong answer that arrives looking right. Those questions are closed; spend the design attention elsewhere. **Recommended** marks everything else: a position with evidence behind it, held unless the domain gives a reason not to. Both are advice, and nothing mechanical reads this file.

The contract itself is a different subject: the contract verbs, the I/O envelope, the exit codes, the write gate, `doctor`, and what `help` must carry belong to `SHEBANG.md`, which slot holds which knowledge to `TEMPLATE.md`, and whether a system deserves a capability at all to `capabilities guide authoring`.

## As complex as the task requires

A capability is as complex as its task requires and no more. Where a simpler option is stable and an agent will understand it, the simpler option wins — fewer verbs, fewer flags, fewer shapes to hold — even when the richer one is more capable in the abstract.

This governs most of what follows. A second way to name an identity, a verb parallel to a flag that already works, a composite that fixes one way of pairing two calls: each is capability bought with a permanent rise in what every caller must learn. Buy it because the task requires it, not to be thorough.

## The caller is an agent

Capabilities are driven by agents, essentially never by hand-written programs. That settles two things.

An agent loads the whole help body at once, keeps no memory between calls, sees no colour or terminal width, and pays a round trip for every exploratory invocation. So a value's accepted format is stated where the value is defined, the platform's own refusals are written down rather than discovered by retry, and an answer keeps its shape as the result grows. Short flags, prompts, colour, and column-fitted tables earn nothing from this caller.

A reshaped verb surface then costs an instruction reload on the next run and rewritten tests, not a consumer migration. Reshaping is cheap, so a surface is improved rather than accreted around; a break still travels through `capabilities guide dev` and `capabilities guide publishing`.

## The verb surface

**Recommended** — keep the entry menu at roughly twenty names, and when it passes that, open the noun clusters before adding another. The budget counts what the menu lists: a nested noun is one name however many operations hang from it, so opening a cluster reduces the number it measures. `notion` runs its whole surface on six names; `clickup`, `asana`, `callva`, and `windmill` each carry a flat list a caller has to search.

**Recommended** — a noun carrying two or more operations nests as `<noun> <verb>`, one level deep, noun first, with a hyphen inside a name belonging to the noun rather than to the nesting. `ids list|get|set|rm` is the contract's own precedent and `stripe invoices list|pdf` follows it; a second axis stays a flag, as in `signoz fields keys --signal logs|traces|metrics`. The alternative is `clickup`'s `field-get|field-set|field-clear` — a subcommand that was never opened — or `callva field-groups`, where `add-field` and `remove-field` scatter one object across the alphabet by its action.

**Recommended** — a flag changes how one operation runs; a verb is a different operation with a different answer. `whatsapp` states the test in its own help — *depth is a parameter of reading, not a separate verb* — so `messages <chat> --rounds N` reaches further back and no `deepen` verb exists. `journal search|scan|grep` are three verbs because each matches a different thing and returns a different shape.

**Recommended** — a convenience verb earns its place by carrying behaviour its flag form cannot. `clickup complete` is not `update --status`: it discovers the list's closed status, refuses on incomplete dependencies, cascades to a parent whose subtasks are now all done, and reports what it unblocked.

**Recommended** — where the system already ships an agent-drivable CLI, forward to it and own only identity, policy, and readiness; where named reads leave gaps, cover them with one escape hatch rather than a verb per endpoint. `calcomc`, `vapic`, and `rclonec` inject one connection's identity and exec the real tool, each saying in a `MODEL` section that it *never maps, renames, or enumerates* the child's commands; `signoz api` and `signoz query <file|->` carry what the named reads do not.

## Parameters

**Settled** — long flags only, one spelling per parameter, and no abbreviation matching. Argparse allows abbreviation by default, so `--conn` silently resolves today and a near-miss becomes a different flag tomorrow; the mechanism is `ArgumentParser(allow_abbrev=False)` on every parser and every subparser. The short flags the catalogue does carry already collide: `-f` is `--filter` in `callva` and `--format` in `whatsapp`, `-c` is `--continue` in `askproject` and `--connection` in `whatsapp`.

**Settled** — an argument the chosen verb, mode, or engine does not take is refused, never silently ignored: a filter that is accepted and dropped returns the wrong rows at exit 0. `signoz` refuses `--select` under `--signal logs`, where there is no traces half for it to reach; `whatsapp` states it as a rule of the surface — *a flag one engine does not take is refused rather than ignored*.

**Settled** — a name the catalogue already carries keeps the meaning it already has, or takes a different name. Spelling alone is not the reuse: `--page` is a 0-indexed page of 100 in `clickup` while `callva` calls it *Page number* and names no base, so an off-by-one returns the wrong rows and reports success. `--force` already means three things — override a refusal in `clickup complete`, redo completed work in `whatsapp transcribe`, rebuild without cache in `coolify deploy`.

**Recommended** — reach for the names the catalogue already carries: `--limit`, `--from` / `--to` for an absolute range, `--since` for a relative window, `--out` for a written file, `--json` for a structured variant, `--connection` for the identity, and `--dry-run` for resolving and printing an operation without performing it, as `deployment` and `simplbooks` both spell it.

**Recommended** — a verb acting on one identified object takes that object positionally, together with any further operand it acts *with*: `notion publish <page>`, `slack post <target> <text>`. A scan takes no positional and expresses its scope as filters; `clickup task <task>` beside `clickup tasks --list ID` is that distinction, not an inconsistency.

**Recommended** — a value whose format is not obvious carries its accepted form where the value is defined, by example. `clickup` splits `--due-on YYYY-MM-DD` from `--due-at ISO8601` rather than typing one flag two ways; `whatsapp` shows every accepted chat-id form and `slack` every target form.

**Recommended** — one way to select an identity, and let it be the connection. `asana`, `clickup`, `notion`, and `youtrack` each carry `--token-env <KEY>` beside `--connection`, and each then has to repeat that it never lifts the connection's write gate; a second selector must be reconciled against every gate it passes.

Secrets never reach argv: that rule and the cascade that replaces them belong to `DOCTRINE.md` and `SHEBANG.md`.

## Writes

**Settled** — omission never blanks. A field not passed is left alone and emptying is its own explicit act: `clickup update --add-assignee / --rem-assignee` is an add/remove set rather than a replace, and `clickup update` refuses with `no_changes` rather than reading an empty call as a blanking one.

**Settled** — a write verb states whether repeating it is safe. An agent retries on timeout, so a create that duplicates in silence produces two of something the caller believes it made once. `windmill folder-ensure` carries *idempotent* in the name and the help line, `notion upsert` finds-or-creates by exact title, `resend send --idempotency-key KEY` hands the dedup key to the platform — and where a repeat is genuinely unsafe, `clickup create` records *Not idempotent — ClickUp has no settable dedup key*.

**Settled** — where a write leaves the system and its outcome is unknown, the tool reports the ambiguity rather than retrying, because a retry can create a duplicate outside, where nothing can withdraw it. `instagram` fails `delivery_unknown` with *Delivery could not be confirmed; do not retry automatically*, and the recovery is a separate verb — `instagram messages reconcile <handle> --offline-id ID` confirms the send from thread history, carries `retry_safe: false`, and is not itself a write verb, so it *never authorizes retry*.

**Settled** — a verb acting over several items reports each item's outcome. `windmill provision` returns one entry per folder, user, variable, and schedule, each carrying what happened to it, `skipped` and its reason included. Exit 0 on a half-applied change tells the caller the whole change landed.

**Recommended** — an element-wise edit is the default and the shape of a partial change: one element at a time, by its own flag or verb, so a call meaning to change one thing can never drop the rest. `clickup update --add-assignee|--rem-assignee` and `asana follower-add|follower-remove` work this way.

**Recommended** — a replacing form is permitted where set reconciliation is the real job, and it names its own destructiveness at the point of use. *Tags should now be exactly X,Y,Z* is a legitimate request, and element-wise calls turn it into a read, a diff, N writes, and a race between them. `journal update --tags "a,b,c"` replaces the whole array and says so on the same line, with `--metadata JSON` merging beside it: the two behaviours are told apart before the call.

**Recommended** — an irreversible verb refuses by default and its refusal names what is lost, as `clickup task-delete` and `whatsapp pair --recreate` do. Read the token as a stop sign rather than a confirmation: the agent that decided to delete appends `--yes` in the same breath, so a second token from the same actor confirms nothing. What confirms is a token derived from the target — the id being destroyed, echoed back — which a mistargeted call cannot supply and a correct one supplies for free.

## Reads

**Settled** — an answer never leaves the caller unable to tell two different situations apart. Two states the caller would act on differently are two answers; one shape that covers both is a wrong answer arriving looking right, and it is believed rather than checked. What follows is that principle in the places it bites.

`whatsapp` refuses rather than return the empty list an unlinked connection would produce — *an empty answer from an unlinked connection is a lie by omission: there is nothing to read because no device is linked, and that is what to say* — and answers exit 7 `not_linked`, naming `whatsapp pair`. `coolify env list` marks a value the platform withheld as `value_hidden` with the reason beside it, *instead of presenting null as a value*: redaction *is not evidence that the variable is empty*, and a caller reading null as empty overwrites what it could not see.

**Settled** — an answer that can be truncated says so in its own envelope. `journal list` carries the matched and total counts so a capped page is visible as such, and `directo report` carries an explicit `truncated` flag. `clickup tasks` returns a bare array paginated to `--limit`, default 100, so exactly 100 rows is indistinguishable from all of them.

**Settled** — a lookup that matches nothing answers with an explicit absence at exit 0, because *nothing matched, so create it* and *you addressed it wrong* are different situations and the caller acts differently on each. `asana external-find` returns the task on a hit and *`null` + exit 0 on a miss (absence is not an error)*; `clickup find` answers *task JSON on a hit, null + exit 0 on a miss*.

**Recommended** — a scan and a read answer differently, the expensive fields stay out of the scan, and a scan widens by flag rather than by a parallel verb. `journal list` carries metadata with summaries cut to 200 characters while `journal read` carries the untruncated summary and the whole transcript; `clickup` ships both `subtasks <task>` and `task --with-subtasks`, two homes for one answer.

**Recommended** — a long or costly operation states its cost per unit and sends progress to stderr, where the I/O contract already puts everything that is not the answer. `whatsapp messages --rounds N` says each round asks the phone for 50 older messages and takes about four seconds, so a caller sizes the call before making it.

The shape of a structured payload is printed by the tool and built by the producer's own builders — `stripe contract`, `whatsapp contract` — never transcribed into prose. DOCTRINE rule 3 owns why.

## Refusals

A refusal is the recovery plan. The agent reading it has no session to reason from, so a message saying only that something is invalid buys a retry that fails the same way.

**Recommended** — a refusal names the offending input, the state that made it wrong, and the one command that resolves it. `clickup` refuses an unknown custom-field name with the name it was given, the list it searched, and `clickup fields --list <id>` as the way to see what exists; `signoz` refuses `--select` under `--signal logs` by explaining that `--select` reaches the traces half and `--signal logs` has none, then offering `--signal traces` or dropping the flag.

**Recommended** — carry a section for what the platform will not do. `notion` ends on "Quirks worth knowing": search is unavailable to PAT tokens, `upsert` matches direct children only. `clickup` says statuses and custom-field definitions are UI-only, so no management surface exists for them. This is knowledge an agent otherwise buys with a failed call.

## The help body

`SHEBANG.md` fixes what the body contains and that it is the single source of truth for the surface. What follows is how it is arranged.

**Recommended** — the order is: what the tool is and what it refuses to be, then the connection or startup facts, then the verbs, then the grammars shared across verbs, then the contract verbs, then the I/O contract and the exit codes. `resend`, `stripe`, `notion`, and `signoz` read in that order.

**Recommended** — verbs are grouped by the job a caller has, not alphabetically and not by HTTP method. `signoz` separates readiness, discovery, search, correlation, saved objects, and the escape hatch into blank-line-divided blocks without needing headers for it; `clickup` splits `Read:` from `Write:`, and that half works.

**Recommended** — a grammar shared by several verbs is stated once in its own section: addressing (`slack` TARGET, `whatsapp` "Chat and contact IDs"), filters, paging, input. Where the domain has a model the caller must hold, give that a short section too rather than spreading it through verb descriptions — `clickup` THE HIERARCHY, `calcomc` and `vapic` MODEL.

## Layers

The surface is reached in layers: the stub line, `help` (the entry menu — every verb with what it answers, plus the shared grammars), `help <verb>` (one verb's full argument contract), `guide <topic>`, `contract`, and `connections` and `doctor`.

**Recommended** — a fact has one authoritative home among those layers and the others point at it. A per-verb page names the addressing or filter grammar it depends on, because a layer reached on purpose has to be usable on its own; it does not restate it.

**Recommended** — every layer names the command that reaches the next. `clickup help [<command>]` is advertised in the body's first command line, so the third layer is discoverable rather than folklore.

**Recommended** — open `help <verb>` on verb count, not on body length: once the surface carries more than roughly a dozen callable forms, the per-verb flags move down a layer and the entry menu keeps one line per verb saying what it answers. `clickup` is the instructive case — it has the third layer and still folds every flag of every verb into the second, so the same facts are printed twice.

**Recommended** — `help` takes the whole verb path. Under a nested noun, `help cards create` prints that operation's arguments and `help cards` prints the noun's own verb list; a forwarding capability passes the rest through, as `calcomc help <cmd>` reaches the child's own help.

## When the capability is not a plain API wrapper

Skip this section when the capability is a CLI over an HTTP API and nothing else. These four shapes carry obligations the rest of the guide does not reach.

**A capability wrapping another CLI** — its envelope and exit codes cover its own layer only and it says so, pointing at the wrapped tool's help as the surface: `railwayc` heads the list *EXIT CODES (railwayc's own layer)* and records that *Forwarded commands return RAILWAY's own exit code, not these*, while `rclonec` sends the caller to `rclone help` because it *does NOT re-document or re-map it*.

It refuses rather than fall back to the wrapped tool's ambient login — `calcomc` *REQUIRES a resolved key and never falls back to this machine's stored `calcom login` session*, because a silent success under another account is the worst outcome available; an unrecognised forwarded command is classified as a write, as in `rclonec`'s *a verb in neither set is treated as a write* and `railwayc`'s *a subcommand this tool has not seen is refused rather than waved through*; and a fact that would drift is asked of the installed binary, `rclonec doctor` reporting *the path it resolved and the version it found*.

**A capability with a login ceremony** — not-ready is its own answer rather than a credential failure, and the refusal names the verb that fixes it: `whatsapp` exits 7 *unlinked* beside exit 2 *auth* and hints `whatsapp pair`, and `telegram` opens its help on an *Agent startup protocol (this is a STATEFUL CLI — it holds a login session)* that routes a missing session to `telegram login`.

**A capability reading a local store** — a verb answering without touching the network says so, so a cheap pre-check is discoverable: `whatsapp health` is *Local only; answers "is there anything to consume?" without touching the network*, and `journal connections` and `fathom connections` are *Local only*. And the answer says how fresh what it holds is — `whatsapp health` carries the store's oldest and newest message and its last capture, and `fathom` stamps `synced_at` on every row it stores.

**A capability shipping a service** — its health is not its liveness: `automations doctor` fails `environment_idle` because *a daemon that is alive and scheduling nothing stops reading as healthy*, and `config_stale` because a daemon that is alive is not thereby current. A control operation returns once the daemon confirms it; the reload contract itself belongs to `SHEBANG.md` and DOCTRINE rule 19, so read it there rather than re-derive it.

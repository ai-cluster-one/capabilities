# Design + plan — `youtrack` MCP parity and noun-verb CLI

**Status — 2026-07-28: ✅ M1, ✅ M2 and ✅ M3 are shipped and all merged to `upstream/main`. M4 outstanding.**

- ✅ **M1** — PR #14, catalog repair in #15.
- ✅ **M2 step 0** — write-direction probe closed, then extended the same day to the *create* path (see "Create path — measured"), which settled step 2's and step 5's central premises.
- ✅ **M2 step 1** (`issues update`) — PR #19.
- ✅ **M2 steps 2–5** (`issues create` with `--field`/`--fields`, type-aware marshalling, schema-backed error translation, `--draft`) — PR #20, whose commit declares "Completes M2". 42 new tests; 105 tests total in `capabilities/youtrack/tests/test_youtrack.py`.
- ✅ **Decision D1** — the misnamed `required` key is gone; `projects fields *` now emit `canBeEmpty` faithfully (breaking change, landed in PR #20).
- ⬜ **M2 "Done when" is code-complete but not live-demonstrated** — the one-call sprint-ready IONDEV create over `text` fields is still unproven against the live server; see the note under M2's "Done when".
- ✅ **M3** — PR #21, merged 2026-07-28: `users find`, `issues links add`/`remove`, links on `issues get`, `--offset` on every `--limit` verb, `--select` on `issues search`. 151 tests pass; verified live against ION. Surface is now **19 domain verbs**. ⬜ **M4a** (unblocked tail) and ⬜ **M4b** (tags and work items) — see "What remains" below. M4 was split into these two because Group C needs a probe and Groups A/B do not.
- **Standing constraint (owner, 2026-07-28): all experiments run against the ION project; IONDEV is not to be touched.** What ION cannot prove stays recorded as unproven — which permanently blocks M2's Group 1 gap, since ION carries no `text` field.

This doc is both the surface contract and the milestone plan; keep them together so they cannot drift.

**Base:** `upstream/main` (`ai-cluster-one/capabilities`), which carries the knowledge-base article verbs merged in PR #13. The `zjor/capabilities` fork's `main` runs behind upstream — branch from `upstream/main`, not `origin/main`.

**Live-verified against `ion.youtrack.cloud` — IONDEV (project `0-6`, 45 custom fields) for reads and update-path writes, ION (project `0-1`, 11 custom fields) for create-path writes.** The marshalling and output sections below are measured, not inferred. The *write* direction is measured too — for both verbs — but **not for every type on every verb**: the exact coverage, and the rows that remain inferred or unconfirmed, are recorded under "Write direction" and "Create path" below. Read those before treating any marshalling row as proven.

## Why

The capability was built issue-and-article shaped: read a task, comment, flip State, author articles. JetBrains' predefined MCP tool set is the surface an agent is now expected to have, and the gap is not evenly spread — it concentrates in **custom fields**, which is where consuming projects keep the material that makes an issue actionable.

The forcing example is the ionwater.io consumer. Its `docs/process/youtrack-field-guide.md` defines ~45 custom fields on the IONDEV project, ~18 required before an issue may enter a sprint (Type, Subsystem, Team, Assignee, Readiness, Priority, Effort Level, Points, Original Estimate, Acceptance Criteria, Definition of Done, …). The capability today **writes exactly one of them** (State) and **reads none** — `ISSUE_FIELDS` omits `customFields` entirely. An agent using this CLI cannot create a sprint-ready issue and cannot check whether an existing one is ready.

**That "~18 required" is a *consumer readiness rule*, not a schema constraint** — it comes from the field guide, and YouTrack knows nothing about it. In the schema sense IONDEV has exactly **4** fields with `canBeEmpty: false`: State, Type, Priority (all single-value) and **Work Category** (`enum[*]`, multi-value) — read live from IONDEV's schema on 2026-07-28. Conflating the consumer's 18 with the schema's 4 is precisely what makes a client-side required-field pre-flight look justified; it is not (see the `canBeEmpty` call-out under "Create path").

A second, smaller problem: the surface is a flat list of unique first-level verbs (`task`, `issues`, `article-create`, `article-comments`). That scales badly — full parity would mean ~30 top-level verbs with no grouping. This design converts it to noun-verb in the same pass.

## Baseline: measured coverage

Against the **23** predefined MCP tools listed on [Predefined MCP Tools](https://www.jetbrains.com/help/youtrack/devportal/predefined-ai-tools.html), enumerated in page order and counted directly: 6 at parity, 6 partial, 11 absent, plus 2 CLI-only verbs. (An earlier reading reported 24; that was a miscount. There is no 24th tool, and the set below is complete.)

| MCP tool | CLI verb today | status |
|---|---|---|
| `get_current_user` | `users me` | ✅ parity |
| `find_projects` | `projects find` | ✅ parity |
| `get_article` | `articles get` | ✅ parity |
| `create_article` | `articles create` (also `--parent`) | ✅ parity+ |
| `update_article` | `articles update` | ⬜ near — cannot re-parent (M4a) |
| `add_issue_comment` | `issues comments add` | ⬜ near — no `permittedUsers`/`permittedGroups` (M4a) |
| `get_issue_comments` | `issues comments list --limit --offset` | ✅ **parity** (M3) |
| `get_issue` | `issues get` | ✅ **parity** (M1 — custom fields now returned) |
| `search_issues` | `issues search QUERY --limit --offset --select` | ✅ **parity** (M3 — sort still absent, deliberately) |
| `create_issue` | `issues create --project/--summary/--description/--field/--fields` | ✅ **parity** (M2 step 2) |
| `update_issue` | `issues update --field/--fields/--summary/--description` | ✅ **parity** (M2 step 1) |
| `search_articles` | `articles list` | ⬜ partial — a listing, not a query (M4a) |
| `get_issue_fields_schema` | `projects fields list` · `projects fields get` | ✅ **parity** (M1) |
| `find_user` | `users find` | ✅ **parity** (M3) |
| `link_issues` | `issues links add` · `issues links remove` | ✅ **parity** (M3) |
| `change_issue_assignee` | `issues update --field Assignee=…` | ✅ covered — deliberate drop, no own verb |
| `manage_issue_tags` | — | ⬜ absent (M4b) |
| `log_work` | — | ⬜ absent (M4b) |
| `get_project` | — | ⬜ absent (M4a) |
| `get_saved_issue_searches` | — | ⬜ absent (M4a) |
| `find_user_groups` | — | ⬜ absent (M4a) |
| `get_user_group_members` | — | ⬜ absent (M4a) |
| `create_draft_issue` | `issues create --draft` | ✅ **parity** (M2 step 5) |
| — | `articles comments list`, `articles comments add` | CLI-only; MCP has no article-comment tools |

**The `status` column above is kept current; the 6/6/11 sentence is the frozen baseline at authoring time. As of 2026-07-28, with M1, M2 and M3 all merged: 13 at parity, 3 near/partial (`update_article`, `add_issue_comment`, `search_articles`), 6 absent, plus 1 (`change_issue_assignee`) deliberately covered by `issues update --field` instead of its own verb.** 13 + 3 + 6 + 1 = 23. (An earlier revision of this line said "1 near/partial, 8 absent" — a miscount that did not sum to 23; corrected against the table above.) Everything still open is M4's long tail. `search_issues` is counted at parity without sort support: sorting is expressible inside the YouTrack query itself (`sort by:`), so a dedicated flag would duplicate the query language.

Attachments are in **neither** surface — they belong to the 44-tool community server, not JetBrains'. Not a parity item.

Parity is the target for *coverage*, not *shape*. The CLI keeps what MCP lacks: named connections, the `allow_write` gate, `doctor`, `ids`, and the JSON-on-stdout / exit-code contract. Nothing here erodes those.

---

# The command surface

## Grammar rules

1. **Contract verbs stay flat.** `help`, `doctor`, `connections`, `stub`, `manifest`, `guide`, `refs`, `ids` — SHEBANG mandates them at level 1. No domain noun may take those names.
2. **`search` = YouTrack query language; `find` = substring match.** The verb tells the caller what to type without reading help.
3. **Nest a sub-resource only when it supports more than one operation.** Two or more ops → `issues comments list`. Exactly one → the sub-resource *is* the verb: `groups members ID`.

Six nouns: `issues`, `articles`, `projects`, `users`, `groups`, `searches`.

**The old flat verbs are removed outright — no aliases, no deprecation window.** Nothing live depends on them: the 31 `youtrack …` references across the ion workspace are all ContextKit-generated context (rebuilt by `contextkit build`) or historical plan assets, and the agent reads `youtrack help` at use time. The break lands in **M1**, while the surface is still six verbs; renaming after M2 would mean rewriting help and tests twice.

## issues

| Command | Milestone |
|---|---|
| `issues get ISSUE` | ✅ M1 |
| `issues search QUERY [--limit N] [--offset N] [--select F,F]` | ✅ M1 · ⬜ M3 (`--offset`, `--select`) |
| `issues create --project ID --summary S [--description TEXT\|-] [--field N=V]… [--fields JSON\|-] [--draft]` | ✅ M2 |
| `issues update ISSUE [--summary S] [--description TEXT\|-] [--field N=V]… [--fields JSON\|-]` | ✅ M2 |
| `issues comments list ISSUE [--limit N] [--offset N]` | ✅ M1 · ⬜ M3 (`--offset`) |
| `issues comments add ISSUE [--text TEXT\|-]` | ✅ M1 |
| `issues links add ISSUE --to ISSUE --type TYPE` | ⬜ M3 |
| `issues links remove ISSUE --to ISSUE --type TYPE` | ⬜ M3 |
| `issues tags add ISSUE TAG` · `issues tags remove ISSUE TAG` | ⬜ M4 |
| `issues work log ISSUE --duration D [--date D] [--type T] [--text TEXT\|-]` | ⬜ M4 |
| `issues work list ISSUE` | ⬜ M4 |

Links are returned by `issues get`; there is deliberately no `issues links list`.

## articles

| Command | Milestone |
|---|---|
| `articles get ID` | ✅ shipped pre-M1 |
| `articles list [--project ID] [--limit N] [--offset N]` | ✅ shipped · ⬜ `--offset` |
| `articles search QUERY [--limit N] [--offset N]` | ⬜ M4 |
| `articles create --project ID --summary S [--content TEXT\|-] [--parent ID]` | ✅ shipped |
| `articles update ID [--summary S] [--content TEXT\|-] [--parent ID]` | ✅ shipped · ⬜ M4 (re-parent) |
| `articles comments list ID [--limit N]` · `articles comments add ID [--text TEXT\|-]` | ✅ shipped |

`articles list` keeps `list` rather than `find` because it is a project-scoped listing, not a match.

`articles create --project` becomes **required**, tightening today's optional flag: the current code sends `{"project": {"id": …}}` unconditionally, so omitting it fails server-side regardless. Making it required moves that failure to argparse, as exit `6` with a usable message.

## projects · users · groups · searches

| Command | Milestone |
|---|---|
| `projects find [SUBSTRING]` | ✅ shipped |
| `projects get ID` | ⬜ M4 |
| `projects fields list ID` · `projects fields get ID NAME` | ✅ **M1** |
| `users me` · `users find SUBSTRING` | ✅ M1 (`me`) · ⬜ M3 (`find`) |
| `groups find [SUBSTRING]` · `groups members GROUPID` | ⬜ M4 |
| `searches list` | ⬜ M4 |

## Deliberate drops

- **`change_issue_assignee` — cut.** `issues update --field Assignee=s.royz` covers it. A dedicated verb for one field invites one per field.
- **`create_draft_issue` — a flag, not a verb.** `issues create --draft`; same payload, one boolean apart.
- **`whoami` → `users me`.** The one contract-adjacent verb that was really a domain read.

## Flag-name collisions resolved

`--fields` means *field values to write* (a JSON object in). Search projection is therefore **`--select`**, never `--fields`.

---

# Field semantics

**Two input mechanisms.** `--field NAME=VALUE`, repeatable, for one or two quick edits; `--fields` taking a JSON object as inline text, a file path, or `-` for stdin — the same three-way convention every text argument in this CLI already uses.

```bash
# quick single edit
youtrack issues update IONDEV-509 --field State="In Progress"

# bulk: multi-line markdown, spaces in field names
youtrack issues create --project 0-6 --summary "…" --fields - <<'JSON'
{
  "Type": "Task",
  "Priority": "High",
  "Sprint Goal Alignment": "Must Have",
  "Acceptance Criteria": "- one\n- two"
}
JSON
```

**Merge order.** `--fields` applies first, then each `--field` overrides it — explicit flags beat the document, so a template can be piped and one value patched.

**Parsing.** `--field NAME=VALUE` splits on the **first** `=` only, so `--field "Description=a=b"` sets `a=b`. Names match **case-insensitively** against the project schema. Repeating a flag for a multi-value field accumulates (`--field "Work Category=Infrastructure" --field "Work Category=Technical Debt"` → both). `--field NAME=` with an empty value clears the field to null — the only way to clear, and explicit.

**Marshalling** is driven by the M1 schema. The mapping below is **measured** against IONDEV: `fieldType.id` from `GET /admin/projects/0-6/customFields`, issue-side `$type` and value shapes from `GET /issues/{id}?fields=customFields(name,$type,value(...))`, correlated by field name. All 14 types occurring in IONDEV are covered.

| `fieldType.id` | multi | issue-side `$type` | write value | read flattens to | IONDEV field |
|---|---|---|---|---|---|
| `state[1]` | no | `StateIssueCustomField` | `{"name": …}` | `name` | State |
| `enum[1]` | no | `SingleEnumIssueCustomField` | `{"name": …}` | `name` | Priority, Type |
| `enum[*]` | yes | `MultiEnumIssueCustomField` | `[{"name": …}, …]` | `[name, …]` | Work Category |
| `ownedField[1]` | no | `SingleOwnedIssueCustomField` | `{"name": …}` | `name` | Subsystem |
| `user[1]` | no | `SingleUserIssueCustomField` | `{"login": …}` | `login` | Assignee |
| `user[*]` | yes | `MultiUserIssueCustomField` | `[{"login": …}, …]` | `[login, …]` | Requestor |
| `version[1]` | no | `SingleVersionIssueCustomField` | `{"name": …}` | `name` | Release Window |
| `version[*]` | yes | `MultiVersionIssueCustomField` | `[{"name": …}, …]` | `[name, …]` | Sprints |
| `build[1]` | no | `SingleBuildIssueCustomField` | `{"name": …}` | `name` | Reported In |
| `period` | no | `PeriodIssueCustomField` | `{"presentation": "1d 4h"}` | `presentation` | Original Estimate |
| `integer` | no | `SimpleIssueCustomField` | scalar | scalar | Points |
| `date` | no | `DateIssueCustomField` | epoch ms, **noon UTC** | epoch ms (snapped to noon UTC) | Due Date |
| `date and time` | no | `SimpleIssueCustomField` | epoch ms | epoch ms (verbatim) | Incident Start Time |
| `text` | no | `TextIssueCustomField` | `{"text": …}` | `text` | Acceptance Criteria |

**The two traps in that table:** `date` maps to `DateIssueCustomField` but `date and time` maps to `SimpleIssueCustomField` — same-looking types, different `$type`. And bundle-backed values carry distinct element types on read (`EnumBundleElement`, `StateBundleElement`, `OwnedBundleElement`, `VersionBundleElement`), all of which flatten by `name`; only users flatten by `login`.

`float` and `string` do not occur in IONDEV. **`float` is no longer inferred — measured 2026-07-28** on ION's `Story points` field (`fieldType.id: float`): issue-side `$type` is **`SimpleIssueCustomField`**, the write value is a bare scalar, and `3.5` round-trips byte-exact on the create path. A string write is rejected: `"3.5"` → `400` `Incompatible value format for type float`, `error_field: "value"` — exactly parallel to `integer`'s rejection of `"7"`, so `float` needs the same string→number coercion `integer` already gets in `_coerce`, not a special case. **`string` remains inferred**: it occurs on neither project probed, so nothing has been sent for it.

## Write direction — measured 2026-07-28 (M2 step 0, closed)

Probed on two throwaway draft issues in IONDEV (`POST /api/users/me/drafts`, `Type=Task` and `Type=Bug`), one write per request so failures stay attributable, each read back and compared. Both drafts deleted afterwards; drafts never receive an `IONDEV-n` id (`idReadable` is `Issue.Draft`) and are invisible to search, so nothing leaked into the project.

**The write column is confirmed as documented for 10 of 14 types**, sending `{"name": <field>, "$type": <issue-side $type>, "value": <write value>}`: `enum[1]`, `enum[*]`, `ownedField[1]`, `user[1]`, `user[*]`, `version[*]`, `period`, `integer`, `text`, and `date and time`. Round-trip is byte-exact for all ten.

The other four — `state[1]` since resolved by the create-path extension, `version[1]` and `build[1]` **not** resolved and downgraded further:

| type | outcome |
|---|---|
| `date` | Shape correct, **but the value is normalized** — see below. Not byte-exact on round-trip. |
| `state[1]` | **Measured 2026-07-28 — on ION.** Set inline at create (`{"name":"Open"}` → read back `{"name":"Open","$type":"StateBundleElement"}`, demonstrably not the `Backlog` default) *and* as a real transition on the numbered issue (→ `In Progress`), and on a draft. So the documented `{"name": …}` / `StateIssueCustomField` shape is confirmed on a real, non-draft issue. **What blocked it on IONDEV was a workflow rule** (`require_attach_task_to_feature/rule` on Task, a generated `vwe-…` rule on Bug; HTTP 400 `error_type: workflow`) — **per-project workflow configuration, not a YouTrack or draft-level limitation.** Scope the claim honestly: measured on ION, still blocked on IONDEV. |
| `version[1]` (Release Window) | **No successful write on any path, ever — never sent at all.** Bundle empty on IONDEV, so no legal value exists to write; field absent from ION. The shape is *inferred* from `version[*]`, which merely shares the bundle type. Zero write attempts anywhere. |
| `build[1]` (Reported In) | **No successful write on any path, ever.** Bundle empty on IONDEV; field absent from ION. Writing a bogus name returns HTTP 400 *from the bundle lookup* (`An nonexistent-build-type entity with the specified name ({1}) was not found`), which proves the `{"name": …}` envelope parses and reaches value resolution — but **no valid value has ever been accepted**. |

**Do not read `version[1]` and `build[1]` at the same confidence as the other twelve marshalling rows.** Neither has a confirmed round-trip on `update`, on `create`, or on a draft, and the 2026-07-28 create-path extension did not improve their standing. **No probe can close them**: it is blocked on someone populating the Release Window and Reported In bundles — project configuration, not testing effort.

**`date` snaps to 12:00 UTC of the same UTC calendar day.** Measured across four inputs — `00:00Z`, `12:00Z`, `23:59Z`, and `00:26Z` next day — every one read back at noon UTC on its own UTC date, with **no day shift**. Consequences:

- Noon UTC is the canonical form. Send it and the round-trip is exact; send midnight and it is not.
- **A naive write-then-compare test on a `date` field will fail.** Compare calendar dates, or write noon-UTC values.
- `date and time` is *not* normalized — it returns exactly what was written. The two date-ish types therefore differ on the write path as well as in `$type`, widening the trap already noted above.
- **Recommendation for M2:** accept and emit `YYYY-MM-DD` for `date` fields, converting to/from noon UTC internally. Raw epoch ms is a hostile interface for a calendar date, and the normalization makes it actively misleading. Keep epoch ms for `date and time`. The two rows in the marshalling table above now carry this distinction.

**Clearing differs by cardinality — and the wrong choice is rejected, not ignored.** Measured on a multi-value field: `[]` clears it (200), while `null` returns **400**. A single-value field takes `null`. So `--field NAME=` cannot emit one shape for both; the single/multi split is load-bearing, not cosmetic.

**Also established:** `Type` can be set in the same `POST /api/users/me/drafts` call that creates the draft, so `issues create --draft` can be born with its type-scoped field set. `DELETE /api/users/me/drafts/{id}` works and leaves no trace.

**Write drafts through `/users/me/drafts/{id}` — but the original reason for that rule is false. Corrected 2026-07-28.** Step 0 first recorded that "drafts are not writable through `/issues/{id}`", because a `POST /api/issues/{draftId}` on IONDEV returned **200 and silently applied nothing**, and flagged that as the dangerous gotcha to know before step 5. **On ION that endpoint does apply writes.** Measured on a draft (`2-5170`), twice, read back through the drafts path both times: Priority `Minor` → **`Critical`** landed; a summary change plus `Story points=42.5` **both** landed. Three values across two requests, all applied.

Why IONDEV behaved otherwise cannot be determined from ION. The likeliest explanation — **tag: INFERRED, not measured** — is that the step-0 write which "applied nothing" was one IONDEV itself rejected or no-opped for an unrelated reason; the `state[1]` row above records that writing a State a draft already holds returns 200 on IONDEV, which is exactly a 200-with-nothing-applied that is *not* caused by the endpoint.

**The recommendation survives; the justification does not.** Keep draft writes on `/users/me/drafts/{id}`, and always read back. But `POST /api/issues/{draftId}` is **not** a harmless no-op, the old claim must not be relied on as a YouTrack-wide invariant, and **nothing in step 5 may depend on that endpoint doing nothing** — treating a real mutation as inert is the more dangerous of the two errors.

**Validation is ahead of the wire — but the reason has changed. Measured 2026-07-28.**

The original rationale here was wrong and is retracted: **YouTrack does not silently ignore a bad `customFields` entry, and it does not write partially.** Measured on a draft:

| bad input | response |
|---|---|
| unknown field name | **HTTP 500** `incompatible-issue-custom-field-name-Nonexistent Field` |
| value outside a bundle | HTTP 400 `An Sideways-type entity with the specified name ({1}) was not found` |
| wrong `$type` for the field | HTTP 400 `Due to a type mismatch, the value property for entity 143-33 could not be updated` |
| bug-set field on a Task | HTTP 400 `You can only update the value for the Severity field when the value for the Type field is…` |
| ISO date string for `date` | HTTP 400 `Incompatible value format for type date` |
| `"7"` for `integer` | HTTP 400 `Incompatible value format for type integer` |

**Atomicity is already guaranteed by the server.** A batch of two valid fields plus one invalid one returned 400 and left *both* valid fields at their prior values — the server rolled the whole request back. So "never a partial write" needs no client-side enforcement, and pre-flight validation is **not** load-bearing for correctness. **On the create path the guarantee is stronger than rollback — no issue is created at all**, so create carries no partial-issue risk whatsoever; see "Create path" below.

What pre-flight validation is still worth doing for:

1. **Error quality.** The server's messages are unusable by an agent: an unsubstituted `{1}` placeholder where the field name belongs, opaque internal entity ids (`143-33`) instead of field names, and no list of legal values. Near-miss suggestions and allowed-value lists can only come from the schema.
2. **Exit-code correctness.** An unknown field name — a plain typo — returns **HTTP 500**, which maps to exit `5` (network/server) under this CLI's contract when it is unambiguously exit `6` (input). Without pre-flight, the exit code lies about whose fault it is. (Measured on IONDEV only. **Not reproducible on ION** — see Limits under "Create path" — so this mapping rests on that single measurement.)
3. **No wasted mutation risk.** Not correctness, but a rejected write still consumes a round trip and, on a real issue, can fire workflow rules.

**This opens a cheaper design than the plan assumed — decided in M2 step 1.** Because the server validates and rolls back, the schema GET is only needed on the *failure* path:

- **(A) Pre-flight always** — as originally planned: one extra `GET /admin/projects/{id}/customFields` before every write.
- **(C) Translate on failure** — attempt the write; on 400/500 fetch the schema, and turn the server's message into a named-field, near-miss, allowed-values error with exit `6`.

**Decision: (C)** — with one correction measured during step 1. `$type` is **mandatory** on every `customFields` write entry: YouTrack answers `$type is required` and infers nothing from the value shape, for any of nine shapes tried (bare string, `{"name": …}`, `{"login": …}`, scalars, lists). So marshalling cannot proceed without type information, a metadata read before the POST is unavoidable, and **(C)'s "one round trip on the happy path" was wrong — both options cost two.** What differs is only where the extra lookup comes from, and it differs per verb:

| verb | pre-write lookup | why that source |
|---|---|---|
| `issues update` | `GET /issues/{id}?fields=project(id,shortName),customFields(name,$type)` | Gives the `$type` map *and* the type-scoped field set, so unknown and out-of-scope names are caught pre-wire for free. Only bad *values* reach the server. |
| `issues create` | `GET /admin/projects/{id}/customFields` | No issue exists yet. The schema also carries allowed values, so create gets full pre-flight validation at no extra cost — effectively (A). |

Under this split the project schema is still fetched only on the failure path for `update`, which is what (C) was chosen for. Verified live: the happy path issues exactly two requests and no schema read.

**Workflow rules are a distinct failure class.** A write can be rejected by project workflow, not by input validity: HTTP 400 with `error_type: workflow`, `error_rule_name`, and `error_issue_is_draft`. That is neither exit `6` (the input was legal) nor really exit `5`. The CLI must surface `error_rule_name` verbatim — an agent told only "400" cannot tell a typo from a business rule it must satisfy.

**Decided in step 1: a new exit code `7`.** Reusing `5` would tell an agent to retry something deterministic, and reusing `4` would conflate a remote rule with the local `allow_write` gate — the caller could no longer tell "my config forbids this" from "YouTrack forbids this". The help contract documents `7` as deterministic and carries the rule name in the error body. This extends the capability's documented exit-code set, which was previously `0/2/3/4/5/6`.

Two consequences for later milestones: a workflow rejection must **not** trigger the failure-path schema fetch (it is not a value problem, and allowed values would misdirect), and `issues create --draft` must **surface** a workflow rejection when one happens (exit `7`, already designed) rather than pre-emptively refuse to send a field some project might reject.

**Retracted 2026-07-28:** this section previously concluded that "`issues create --draft` cannot set State on a draft at all, because IONDEV's rules reject every draft transition." That is an **IONDEV workflow artifact, not YouTrack behaviour.** On ION, State is settable on a draft at creation and on a numbered issue both at create and as a transition (see `state[1]` above and "Create path" below). **Step 5 must not hard-code a refusal to set State on drafts.**

**The exit-`7` path itself was not re-exercised by the create-path extension.** No ION workflow rule fired on any write there, so no rejection could be observed; the exit-`7` design still rests **solely on the original IONDEV measurement**. Tag: UNPROVABLE-HERE on ION (it has no rule that rejects these writes).

**Fields are scoped by issue Type, not just by project — measured.** IONDEV declares 45 project custom fields, but `IONDEV-509` (a Task) carries only 34; the 11 missing are the bug/incident set (`Severity`, `Steps to Reproduce`, `Reported In`, `Incident Start Time`, `Root Cause`, `Blocked Reason`, …), which appear on `IONDEV-974` (a Bug). So the project schema is a *superset*: validating a name against it alone will accept `Severity` on a Task. On update, validate against the fields actually present on the target issue; on create, against the fields the chosen `Type` carries. Report a field that exists in the project but not on this issue with a distinct message — it is a different mistake from a typo.

Two corrections from the 2026-07-28 probe. First, **YouTrack enforces type scoping rather than silently dropping** an out-of-scope field: writing `Severity` to a Task draft returns HTTP 400 `You can only update the value for the Severity field when the value for the Type field is…`. The client-side check is therefore for message quality, not to prevent silent data loss. Second, the draft field counts are **32 on a Task and 40 on a Bug**, against 34 for `IONDEV-509`; the superset relationship holds, but the exact count is not a fixed property of the Type, so validation must read the field set off the target issue (or the freshly created draft) rather than assume a per-Type count.

**Schema resolution: fetch per invocation, do not cache.** No invalidation problem, and no manifest change — the capability declares `state: false`, and a schema cache would flip it to `true` and drag in the whole staleness question. Under option (A) a write costs one extra `GET /admin/projects/{id}/customFields` before the `POST`; under (C) that GET happens only on the failure path. Either way it is never cached across invocations.

**Write gating.** Every new write path respects `allow_write`, per the standard.

## Create path — measured 2026-07-28 (M2 step 0, extended)

Step 0's first pass ran on IONDEV drafts and left the *create* verb unproven, which is what put step 2's and step 5's premises at risk. It was extended the same day against **ION (project `0-1`)**: raw HTTP, one write per request so failures stay attributable, **every write read back** — no conclusion here rests on a 2xx alone. 9 numbered issues created and all 9 deleted, each deletion verified by a follow-up `404`; 2 drafts created, one consumed by a promotion (whose resulting issue was itself deleted) and one deleted directly, both verified gone. Final sweeps of `project: ION created: Today`, instance-wide `summary: PROBE`, and `GET /api/users/me/drafts` all returned `[]`. No write of any kind was issued against IONDEV or any other project.

ION carries only 11 custom fields, spanning **7 of the marshalling table's 14 `fieldType.id` values**. That is the ceiling on what this extension could prove — see "Create-path type coverage" below before treating any row as confirmed on create.

**The pivot is positive: `POST /api/issues` accepts `customFields` inline and applies them.** One create carrying `Type`, `Priority` and `Subsystem` (three different types) returned 200 → `ION-1415`, and all three read back landed. So **step 2 needs no draft-then-promote path and no second write to set fields** — its central design premise is sound. Step 5's premise holds too: a single `POST /api/users/me/drafts` carrying six custom fields across five types landed **6 of 6, including State**.

**`$type` is mandatory on create, exactly as on update.** `{"name":"Priority","value":{"name":"Minor"}}` → `400` `$type is required`, and so does `{"name":"Story points","value":3.5}` — a bare scalar for a `float` field, the shape where inference would be most trivially available. There is no inference from value shape at creation. So **`issues create` cannot skip its schema read**: the metadata lookup before the POST is mandatory, not an optimization, and the per-verb lookup table above stands unchanged.

**⚠️ `canBeEmpty: false` does NOT mean required-at-create.** It means *"this field may not be emptied"*. A create omitting **all three** of ION's `canBeEmpty: false` fields returned **200** and the server supplied defaults — State `Backlog`, Type `Task`, Priority `Normal` (plus Assignee `s.royz`, though that field is `canBeEmpty: true`). Consequence: **`issues create` must not implement a client-side required-field pre-flight that refuses to send when a `canBeEmpty: false` field is missing** — it would reject creates YouTrack accepts, making the CLI strictly less useful than raw HTTP. The step-0 framing "pre-flight or rely on the server" resolves to **rely on the server**. A "sprint-ready" completeness check belongs to the consumer's readiness rules and should at most *warn*.

**Scope that claim precisely — one cell of the matrix is open.** What was measured is three `canBeEmpty: false` fields that are all **single-value and bundle-backed** (`state[1]`, `enum[1]`, `enum[1]`). It does **not** establish that YouTrack defaults every required field of every cardinality, and cardinality is exactly the axis the clearing rule above makes load-bearing. IONDEV's fourth required field, **`Work Category` (`enum[*]`, multi-value)**, was never written to — whether a create omitting it gets a default, an empty list, or a 400 is **unmeasured**, because writes were confined to ION. This residual does not undermine the recommendation: three of IONDEV's four required fields are the same shapes measured here and all four are bundle-backed, so a `canBeEmpty`-driven pre-flight would be wrong for at least three of the four however `Work Category` behaves.

**⚠️ The project schema's `$type` is *project-side* and must never be passed through as the write's `$type`.** `GET /api/admin/projects/{id}/customFields` — step 2's own pre-write lookup — reports `SimpleProjectCustomField` for a `date` field and for `float`, whereas the write requires the **issue-side** types `DateIssueCustomField` and `SimpleIssueCustomField`. Taking the schema's own `$type` earns a type-mismatch 400. **Build the mapping from `fieldType.id` through the marshalling table**, never from the schema's `$type` key.

**Create is atomic, and safer than update on this axis.** A mixed-validity create — two valid fields plus `Subsystem: "Sideways"` (outside the bundle) — returned `400` with the same mangled `An Sideways-type entity with the specified name ({1}) was not found`, and **no issue was created at all.** Verified twice: an immediate `summary: PROBE` query returned exactly the previously-created issues and no extra, and an end-of-probe `created: Today` sweep accounted for every one. The same held for all four rejected creates. So **create carries no partial-issue risk** — unlike update, where the server rolls field values back on an issue that continues to exist. **No client-side atomicity work and no compensating deletes**; there is nothing to clean up after a rejected create. (Internal entity ids *are* consumed by rejected creates and gaps appear in the internal id sequence, one per rejection; the visible `ION-n` sequence has no gaps. Reading those gaps as "allocate, validate, discard" is **INFERRED** from id arithmetic — a discarded entity is not observable. The load-bearing part, *no issue exists afterwards*, rests on the two sweeps.)

**The request body is not a description of the resulting issue.** ION defaults State/Type/Priority when omitted and **auto-assigns Assignee to the creating user** even though that field is `canBeEmpty: true`. So `issues create` must report the created issue by **reading it back**, not by echoing the request, or it will under-report what it wrote.

**Draft promotion exists — measured, and explicitly not built in M2.** `POST /api/issues?draftId={id}` with body `{}` promotes a draft to a real numbered issue. Measured properties, all read back:

- **Fields carry over intact** — all six custom fields of the probe draft appeared on `ION-1423` (Type, Priority, State, Story points, Sprints, Estimation).
- **The draft is consumed**, not copied: `GET /api/users/me/drafts/{id}` afterwards → `404`.
- **The promoted issue gets a new internal id**, not the draft's, so a caller holding a draft id must read the result id from the promotion response.
- **The Assignee auto-default does not fire on promotion** (Assignee came out `null`), whereas a direct `POST /api/issues` sets it. So promote-a-draft and create-directly are **not** equivalent operations — equivalent inputs produce different issues.
- Drafts are **addressable but not searchable**: `idReadable` is the literal `Issue.Draft`, they are invisible to `GET /api/issues?query=…`, yet readable via `GET /api/issues/{internalId}`. So `--draft` output must return the **internal id** — there is no readable key for the caller to hold.

**This is a follow-up, not M2 scope. No promote verb is in scope for M2** — step 5 ships `--draft` and *documents* this path so a caller is not handed an object with no way forward.

### Limits of the create-path extension — what it could not prove

Two things ION cannot decide. Both still rest **solely on the earlier IONDEV measurement** and must not be re-attributed to this probe:

| item | why ION cannot decide it | tag |
|---|---|---|
| The **HTTP 500** mapping for an unknown field name (the exit-`6`-vs-`5` correctness argument above) | ION's 11 fields gave no occasion to send a name ION would treat as unknown. | UNPROVABLE-HERE |
| The **exit-`7` workflow-rejection path** | No ION workflow rule fired on any write, so no rejection could be observed. | UNPROVABLE-HERE |

Also unexercised: **type scoping**. All 11 ION fields appear on every issue and on the draft regardless of `Type`, so ION cannot exercise the type-scoping logic above — that remains an **IONDEV-only** measurement. This is a property of ION being a small project, not a contradiction.

One error-translation set *was* confirmed on the create path: bundle miss → `400` with `{1}` unsubstituted and the value spliced into the type slot; format mismatch → `400` `Incompatible value format for type <t>` with `error_field: "value"`; missing `$type` → `400` `$type is required`. The exit-`6` mapping applies to all three.

Two incidental corrections, both worth keeping:

- **For `user[1]` allowed values read `bundle.aggregatedUsers`, never `bundle.values`.** On ION's Assignee bundle the two disagree: `values` lists 4 users **plus a login-less `ProjectTeam` entry**, and carries display names under `name`; `aggregatedUsers` lists 11 logins. Measured: `c.wootson` is in `aggregatedUsers` but not `values`, and writing it succeeded — and the server itself auto-assigned `s.royz`, also absent from `values`. So `values` is a subset that would produce false rejections. The current code already prefers `aggregatedUsers` (`bin/youtrack` ~line 1082) — step 2 must not "simplify" that. Because `aggregatedUsers` is **permission-scoped**, an unrecognized login must **never be a hard refusal** — the visible set depends on the token, so refusing would invent a rejection the server would not make. This CLI has no warning channel (JSON on stdout, errors on stderr, nothing in between), so the implementable form of that rule is to **exclude user-typed fields from the pre-wire allowed-value check entirely** and let the server judge the login. That is what `issues create` does; the bundle check still covers every non-user field, and the failure-path translation covers user fields once a write has actually been refused.
- **`period`: assert `presentation`, not `minutes`, in tests.** `1d 4h` round-tripped byte-exact as `presentation` but read back **720** minutes — ION counts a workday as 8h, from server-side project settings, so a `minutes` assertion tests a server configuration value rather than CLI behaviour. (That 1d = 8h specifically is **INFERRED** from a single 720-minute datapoint, not a settings read; the `presentation` round-trip itself is measured.)

### Create-path type coverage — 7 of the 14 rows were never sent on a create

This exists so step 2 cannot inherit an unearned "all types confirmed on create". The extension sent **7 of 14** marshalling rows on a create or draft-create request, each read back: `state[1]` (State), `enum[1]` (Type, Priority), `ownedField[1]` (Subsystem), `user[1]` (Assignee), `version[*]` (Sprints), `period` (Estimation), `date` (Start Date) — plus **`float`** (Story points), the extra type discussed under the marshalling table rather than in it. `version[*]` carried **two values in one create call, order preserved**, so the repeated-`--field` accumulation semantics work on create and not only on update.

The other **7 do not exist on ION at all**, so no create request could carry them. Confirmed against IONDEV's live schema: IONDEV's 14 distinct `fieldType.id` values are exactly this table's 14 rows, and these 7 are the complement of what ION carries. **Tag: UNPROVABLE-HERE on ION.** They split into two materially different groups, and folding them into one list understates the position of the last two:

**Group 1 — five types confirmed on the `update` path, untested on `create`:** `enum[*]`, `user[*]`, `integer`, `date and time`, **`text`**. Each is a member of step 0's confirmed-byte-exact list above, so its `{"name", "$type", "value"}` write shape has a live round-trip; only the `create` verb is untested. **`text` is the consequential one** — it is the type behind Acceptance Criteria and Definition of Done, the two fields that gate M2's own "Done when". **So the create path has not been demonstrated end-to-end for M2's acceptance criterion.**

**Group 2 — two types with no successful write anywhere, on any path:** `version[1]` and `build[1]`. This is worse than "untested on create": neither has a confirmed round-trip on update, on create, or on a draft, `version[1]` has **never been sent at all**, and `build[1]` has never had a valid value accepted. **Not closable by any probe** — the bundles are empty, so it is blocked on **YouTrack project configuration, not testing effort**. See the downgraded rows above.

**Why Group 1's residual create-path risk is assessed as low — this is reasoning, not measurement. Tag: INFERRED.** It is **the most consequential inference in the whole record**, because it is what would license step 2 proceeding against types never sent on a create. The argument: (1) all five Group 1 shapes were measured byte-exact on the **update** path, so per-type marshalling is established; (2) what the create-path extension adds is that the create endpoint accepts and applies the `customFields` array *at all* — a property of the endpoint, not of any type — and once `$type` is resolved both verbs converge on the same per-entry marshalling, with no type-specific behaviour observed across the 8 types create did carry, including the awkward ones (`date` normalization identical to update, `version[*]` accepted as a list); (3) the one place create genuinely differs — server-supplied defaults for omitted fields — is orthogonal to marshalling. **That is an argument, not evidence. It does not license writing "confirmed on create" against those rows, and it does not extend to Group 2**, whose shapes have no confirmed write anywhere to inherit from.

The honest summary: **the create *mechanism* is measured; create *marshalling* is measured for 7 of 14 types, inherited from `update` for 5 more, and unconfirmed everywhere for the remaining 2.**

**The cheap way to close Group 1** is to re-run the same create shape against IONDEV — the only project carrying all 14 types — once step 2 exists, covering `text` in particular. That needs write authorization the extension did not have, which is why the gap is recorded rather than closed.

**Group 1 remains open, and is now explicitly out of bounds — decided 2026-07-28.** The owner scoped all experiments to the **ION** project and ruled IONDEV untouchable, accepting that whatever ION cannot prove stays unproven. ION's schema was read live and carries **11 fields across 8 types — none of them `text`**, and no `enum[*]`, `user[*]`, `integer`, or `date and time` either. So all five Group 1 types are unprovable on the only project available for writes. This is a standing constraint, not a scheduling gap: closing Group 1 requires either IONDEV write authorization or a `text` field added to ION.

---

# Link model — measured 2026-07-28 (M3 step 0, closed)

Probed live against **ION** on two throwaway issues (ION-1435, ION-1436), both left link-free afterwards. Every row below is measured unless tagged otherwise.

**Link types are global, not per-project.** `GET /api/issueLinkTypes` returns the instance-wide set — 4 types here (`Relates`, `Depend`, `Duplicate`, `Subtask`). Resolution therefore needs **no project argument**, which removes a parameter the design had assumed.

**All direction phrases are unique, so a phrase alone identifies a type *and* a direction.** This is what licenses the approved `--type "subtask of"` grammar:

| Phrase | Type | Link id | `direction` |
|---|---|---|---|
| `relates to` | Relates (undirected) | `137-0` | `BOTH` |
| `is required for` | Depend | `137-1s` | `OUTWARD` |
| `depends on` | Depend | `137-1t` | `INWARD` |
| `is duplicated by` | Duplicate | `137-2s` | `OUTWARD` |
| `duplicates` | Duplicate | `137-2t` | `INWARD` |
| `parent for` | Subtask | `137-3s` | `OUTWARD` |
| `subtask of` | Subtask | `137-3t` | `INWARD` |

An **undirected** type contributes exactly one phrase: `Relates` reports `targetToSource: ""` (empty string, not a copy of `sourceToTarget`), so there is no self-collision to disambiguate. **That the 7 phrases are globally unique is measured on this instance only** — a custom link type could in principle collide, so resolution must fail loudly on an ambiguous phrase rather than pick one.

**`readOnly: true` is not a write gate — ignore it.** `Duplicate` and `Subtask` both report `readOnly: true`, and links of both were created successfully. The flag governs editing the *type definition*, not link creation. This one needs a source comment: it reads exactly like a permission gate, and treating it as one would disable `subtask of` — the single verb M3 exists to deliver.

**`GET /issues/{id}/links` returns all 7 slots regardless of content**, each with an empty `issues: []` when unused. Two consequences:

- **`links` embeds in the issue projection**, so `issues get` gains links in the same GET with no second call — the design's assumption holds.
- **Output must filter to non-empty slots.** A pass-through flatten would emit 7 mostly-empty entries on every single issue read.

**The link id encodes direction:** `{typeId}` when undirected, `{typeId}s` for OUTWARD, `{typeId}t` for INWARD. **Do not build that suffix by string arithmetic — read the ids off `GET /issues/{id}/links`.** The same payload carries both the phrase and the exact id to write to, so one call resolves everything measured; the `s`/`t` convention is an observed pattern, not a documented contract, and both verbs already have an issue in hand.

**Add — `POST /api/issues/{id}/links/{linkId}/issues`:**

- Body accepts **either** `{"idReadable": "ION-1436"}` **or** `{"id": "2-5186"}`. The readable key works, so add needs no id resolution.
- **Reciprocity is automatic.** One write populated `subtask of → ION-1436` on one side and `parent for → ION-1435` on the other.
- **Idempotent.** Repeating the identical write returned `200` and produced no duplicate entry.

**Remove — `DELETE /api/issues/{id}/links/{linkId}/issues/{targetId}`** → `200`, empty body, clears both sides. **Two asymmetries with add, both measured:**

- **The target must be the internal id.** A readable key returns `404`. So `remove` pays one resolution GET that `add` does not.
- **It is not idempotent.** Removing a link that does not exist returns `404` — and its message names the *target issue* (`Entity with id 2-5186 not found`) even though that issue exists. Passing it through would tell the caller the issue is missing when the *link* is. Must be translated.

**Write path B — the commands API works but loses.** `POST /api/commands` with `{"query": "relates to ION-1436", "issues": [{"idReadable": "ION-1435"}]}` returned `200` and created the link. It returns a bare `{}`: no confirmation of what changed and no structured error detail. **Path A is chosen** — direction-explicit, echoes the target, and carries translatable errors.

**Error shapes — the reason resolution belongs client-side:**

| Case | Server response | Translation |
|---|---|---|
| unknown link id | **`404`** `Entity with id 137-9t not found` | resolve phrases client-side; exit **6** with near-miss over the 7 legal phrases. The server's own 404 names no field and would land on exit 3. |
| nonexistent target | **`400`** `YouTrack is unable to locate an Issue-type entity unless its ID is also provided` | exit **3** naming the target. The raw message describes an id-format problem, which is not what happened. |
| **self-link** | **`200` — and silently creates nothing** | **Refuse client-side at exit 6.** The server accepts and ignores, so reporting success would be a lie about a no-op. |
| remove a nonexistent link | **`404`** naming the target issue | exit **3** naming the *link* and both issues. |

**Not proven on ION: workflow-rule rejection of a link (exit 7).** No link rule fired on ION, and whether one exists on IONDEV is unknown and now unaskable under the ION-only constraint. Tag: **UNTESTED** — do not claim exit 7 coverage for links.

# Output shape

Custom fields nest under their own key rather than merging top-level, so a field named `Summary` or `ID` cannot collide with core attributes:

```json
{
  "idReadable": "IONDEV-509",
  "summary": "…",
  "project": { "shortName": "IONDEV", "id": "0-6" },
  "fields": {
    "State": "In Progress",
    "Assignee": "s.royz",
    "Points": 3,
    "Work Category": ["Infrastructure", "Technical Debt"],
    "Acceptance Criteria": "- one\n- two"
  }
}
```

Values flatten to scalars, arrays, or null — never YouTrack's `{"name": …, "$type": …}` wire shape. Consumers read `fields["Points"]`, not `customFields[7].value.name`.

Exit codes unchanged: `0` success, `2` auth/config, `3` not found, `4` policy refusal, `5` network/server, `6` input.

---

# File organization

**Decision: stay one file.** The capability is not as large as it looks. Of 971 lines on the branch, **554 (57%) are the generated contract fences** (`capability core` 119–546, `connections` 556–681) — stamped by `capabilities sync-contract`, byte-checked by `capabilities audit`, never hand-edited or read. Hand-written domain code is **~340 lines**, and the fences do not grow with the surface.

Against the house norm on `main`, youtrack is the *smallest* capability in the repo:

```
simplbooks 7314   windmill 1909   stripe 1361
asana      2364   directo  1683   notion 1206
telegram   2202                   youtrack 971  ← smallest
```

Projected at full parity: help ~120, fences 554, domain ~630 (schema/marshalling ~150, issues ~120, sub-resources ~120, articles ~80, remaining nouns ~80, dispatch ~80), header ~30 → **≈1,330 lines**. Above `notion`, below `stripe`; comfortably mid-pack.

The costs of splitting are concrete, not hypothetical:

- **No in-process module precedent exists.** The only bundle in the repo, `telegram/service/`, is *out-of-process*: `subprocess.Popen([daemon])` against a separate shebang script with its own PEP-723 deps. An importable-module split would be the repo's first, and `audit`/`groom` would read it as drift absent a dedicated `deviations.md`.
- **It breaks single-file install.** `capabilities install youtrack --from capabilities/youtrack/bin/youtrack` — the path the verification loop below uses — requires a source-*directory* install once a bundle exists.
- SHEBANG.md is explicit: *"One file is the whole program — copyable, symlinkable, with no sibling `requirements.txt` or lockfile to keep in sync."*

**Revisit trigger:** domain code past **~1,500 lines** (total ~2,100, above `asana`), or the day a genuinely separable out-of-process engine appears — that is when a bundle earns its deviation.

## Layout within the file

Banner-delimited sections in dependency order:

```
  shebang + PEP-723 header
  """help — the contract"""              ~120 lines
  imports, constants, field projections
  # >>> contract: capability core >>>    428  [generated]
  # >>> contract: connections >>>        126  [generated]
  ── http & refs ──          _client, _request, _issue_ref, _article_ref
  ── schema & marshalling ── _schema, _validate, _marshal        ← densest
  ── issues ──               get, search, create, update
  ── issues: sub-resources ── comments, links, tags, work
  ── articles ──
  ── projects · users · groups · searches ──
  ── dispatch ──             COMMANDS table + main()
```

## Dispatch: generate argparse from a declarative table

`main()` currently hand-writes one `add_parser` line per verb. At three levels that becomes three tiers of nested `add_subparsers` and roughly triples. Instead one table describes the surface and a loop wires argparse from it:

```python
COMMANDS = {
    ("issues", "get"):              (issues_get,     [ARG_ISSUE]),
    ("issues", "search"):           (issues_search,  [ARG_QUERY, ARG_LIMIT, ARG_OFFSET, ARG_SELECT]),
    ("issues", "update"):           (issues_update,  [ARG_ISSUE, ARG_FIELD, ARG_FIELDS, ARG_SUMMARY, ARG_DESC]),
    ("issues", "comments", "list"): (comments_list,  [ARG_ISSUE, ARG_LIMIT, ARG_OFFSET]),
    …
}
```

Adding a verb is one row plus one function. argparse still supplies validation and per-level `--help`. Shared arg specs (`ARG_ISSUE`, `ARG_LIMIT`) prevent flag drift across ~30 verbs.

The table also enables a **drift test**: assert every `COMMANDS` key appears in the help docstring and vice versa. SHEBANG makes the docstring the single source of truth for the surface; nothing enforces today that it matches dispatch, and at 30 verbs it silently will not.

---

# Milestones

## M1 — See the fields, and land the grammar — ✅ SHIPPED

Read-only plus the rename. No write risk; unblocks M2. Landed in PR #14; the catalog reindex it omitted landed in PR #15 (see Verification).

1. **Noun-verb conversion of the existing surface**, flat verbs removed. Cheapest now, while there are six verbs.
2. **`issues get` returns custom fields.** Extend `ISSUE_FIELDS` to `customFields(id,name,$type,value(id,name,login,fullName,text,minutes,presentation))` and flatten to the `fields` map above.
3. **`projects fields list ID` / `projects fields get ID NAME`** — the `get_issue_fields_schema` equivalent: name, required (`canBeEmpty`), type, multi-value-ness, and allowed values for bundle-backed fields.
4. `users me` replaces `whoami`.

The type model is already confirmed: `GET /admin/projects/{id}/customFields?fields=field(name,fieldType(id,isMultiValue)),$type` returns the 14 `fieldType.id` values in the marshalling table. M1 inherits that table rather than rediscovering it.

**Done when:** `issues get IONDEV-509` shows Type/State/Assignee/Priority/Points/Acceptance Criteria, and `projects fields list 0-6` enumerates the field set with allowed values for every bundle field.

**✅ FIXED in PR #20 — Defect shipped in M1, surfaced by the 2026-07-28 create-path probe: the `"required"` key was a misnomer.** `projects fields list` / `get` now emit `canBeEmpty` under its own name (a documented breaking change), and the source carries a comment recording why it is deliberately not inverted into `required`. Record of the defect follows. Item 3 above emits `"required": not entry.get("canBeEmpty", True)` from `_shape_project_field` (`capabilities/youtrack/bin/youtrack`, ~line 1085). Since **`canBeEmpty: false` does not mean required-at-create** (measured — see the call-out under "Create path"), that key reports `required: true` for State/Type/Priority on ION while a create omitting all three succeeds and the server defaults them. So the capability's own schema output tells an agent a field is mandatory when a create may legally omit it — steering the reader toward exactly the pre-flight step 2 must not build. **Decision D1: report `canBeEmpty` faithfully instead** — pass the flag through under its own name rather than restating it as a claim it does not support. **This is a read-path defect already on `main`**, not new step-2 work, which is why the `canBeEmpty` finding cannot be left as a footnote.

## M2 — Set the fields — ✅ SHIPPED

The milestone that makes the capability usable, and the only one with real design risk. All five steps landed 2026-07-28 across PRs #18 (step 0 evidence), #19 (step 1) and #20 (steps 2–5, "Completes M2"). **The code is complete; the live "Done when" demonstration is not — see the note at the end of this section.**

0. ~~**Prove the write direction first**, on a throwaway draft issue: one write per `$type` in the marshalling table, read back, compare.~~ **✅ Done 2026-07-28** — see "Write direction — measured" above. 10 of 14 types confirmed byte-exact; `date` confirmed with a normalization rule; `state[1]`, `version[1]`, `build[1]` unprovable on IONDEV for reasons recorded there. Two of the plan's premises (silent-ignore, client-side atomicity) were disproved and have been rewritten. **✅ Extended the same day to the create path** — see "Create path — measured" above: `POST /api/issues` takes `customFields` inline (no draft-then-promote), `$type` is mandatory on create as on update, a rejected create leaves no issue, and **`canBeEmpty: false` is not required-at-create**. Two further recorded claims were falsified (draft writability via `/issues/{id}`, and "a draft cannot take State"). `state[1]` and `float` moved to measured; `version[1]` and `build[1]` did **not** move. **7 of the 14 marshalling rows were never sent on a create** — read the coverage section before assuming otherwise.
1. ~~`issues update` with `--field` / `--fields`, plus `--summary` / `--description`.~~ **✅ Done 2026-07-28.** Option (C) chosen, with the per-verb lookup correction above. `--state` is **removed** — `--field State=…` replaces it, per the "a dedicated verb for one field invites one per field" rule. Exit **7** added for workflow-rule rejections. `date` fields take and emit `YYYY-MM-DD`; `date and time` stays epoch ms. 24 new tests, every one mutation-checked; verified live against IONDEV-509 and a throwaway draft.
2. `issues create` — **✅ Done 2026-07-28 (PR #20).** `--field NAME=VALUE`, `--fields JSON|FILE|-`. `$type` is resolved from the project schema's `fieldType.id`, never from its project-side `$type`. Because the schema is already in hand, create gets full allowed-value pre-flight for free — with user-typed fields deliberately excluded from that pre-check, since the visible user set is token-scoped and refusing there would invent a rejection the server would not make. An unmapped `fieldType.id` fails by name at exit 6 rather than sending an entry the server would reject. Design record follows. A single `POST /api/issues` with inline `customFields`, on the same marshalling path, so an issue can be born sprint-ready in one call; the premise is measured, not assumed. Its pre-write lookup is the project schema (see table above), which makes full pre-flight validation free — do not copy `update`'s failure-path translation blindly. Four measured constraints from step 0's create-path extension:
   - **Resolve `$type` from `fieldType.id`, never from the schema's own `$type`** — that one is *project-side* (`SimpleProjectCustomField` for a `date` field) and passing it through earns a type-mismatch 400.
   - **No required-field pre-flight.** `canBeEmpty: false` does not gate creation; refusing to send would reject creates YouTrack accepts. Warn at most, and only in the consumer's readiness terms.
   - **Report by reading the issue back**, not by echoing the request — the server defaults State/Type/Priority and auto-assigns Assignee.
   - **No client-side atomicity work and no compensating deletes** — a rejected create creates nothing.
3. **✅ Done 2026-07-28 (PRs #19, #20).** Type-aware marshalling per the table above, replacing the hardcoded `{"name": "State", "$type": "StateIssueCustomField", …}`. Includes the `date` ↔ `YYYY-MM-DD` conversion and the `date`/`date and time` split, and `float`'s string→number coercion alongside `integer`'s. **Coverage caveat, do not lose it: 7 of the table's 14 rows were never sent on a create** — unprovable on ION, which carries none of them. Five (`enum[*]`, `user[*]`, `integer`, `date and time`, `text`) inherit their shape from the *update* path only, and the argument that this makes create-path risk low is **INFERRED, not measured** (see the coverage section). Two (`version[1]`, `build[1]`) have **no successful write on any path at all**. **`text` gates M2's own "Done when"** below.
4. **✅ Done 2026-07-28 (PRs #19, #20).** Schema-backed error translation with near-miss (`difflib`) and allowed-value messages, and distinct handling for `error_type: workflow` rejections at exit 7. Note the two mappings the create-path extension could **not** reproduce and which still rest solely on the IONDEV measurement: the **HTTP 500** unknown-field-name → exit `6` mapping, and the **exit-`7`** workflow path.
5. `issues create --draft` — **✅ Done 2026-07-28 (PR #20).** Via `POST /api/users/me/drafts` with `Type` in the create payload; measured to accept a full field set (6/6 across five types) in that one call, so a draft can be born sprint-ready. **Do not hard-code a refusal to set State on a draft** — that was an IONDEV workflow artifact. Draft writes still go to `/users/me/drafts/{id}` and must always be read back, but not because `/issues/{draftId}` is inert — it is not. Output must return the **internal id** (drafts have no readable key). **Document the promotion path `POST /api/issues?draftId={id}` and its properties** (fields carry over, the draft is consumed, the new issue has a different internal id, and the Assignee auto-default does not fire) so `--draft` does not hand the caller a dead end — **documentation only; no promote verb is in M2's scope.**

**Done when:** one `issues create` call produces an IONDEV issue satisfying the consumer's Ready-for-Sprint rules, and `issues update --field` moves each afterwards.

**⬜ STILL OPEN — the code shipped, this demonstration did not.** Those rules are gated by Acceptance Criteria and Definition of Done, both `text` fields, and `text` has **never been sent on a create** against the live server. PR #20's 42 tests parametrize all 14 marshalling rows plus `float` against exact request bodies, which pins the *client's* request shape — but a test fixture cannot prove the server accepts `text` on `POST /api/issues`, which is the residual risk the coverage section flags. **So M2 is shipped-and-tested but not live-accepted for its own acceptance criterion.**

**And it is now blocked indefinitely, by decision rather than by effort (2026-07-28).** Closing it needs one real `issues create` carrying both `text` fields, but the owner has scoped all experiments to **ION** and ruled IONDEV untouchable — and ION's live schema carries no `text` field (11 fields, 8 types, none of them `text`). Unblocking requires either IONDEV write authorization or a `text` field added to ION. Until then this criterion stays open on purpose; do not let a later reader mistake it for an oversight.

## M3 — Work the board — ✅ SHIPPED

Design approved 2026-07-28. **All experiments are confined to the ION project; IONDEV is not to be touched** — anything ION cannot prove stays recorded as unproven.

0. **✅ Probe the link model — done 2026-07-28**, see "Link model — measured" above. Scoped to links only: `users find` and paging needed no probe. Seven questions closed; it changed three things in the approved design (noted inline below) and produced one finding that would have broken the milestone silently — `readOnly: true` on `Subtask` is not a write gate.
1. **✅ `users find`** — a hard prerequisite for Assignee/Requestor/Approver. M2 can marshal `{"login": …}` blind, but nothing can confirm the login exists. `GET /api/users?query=…`, reusing the existing `USER_FIELDS` projection. **Decision: it stays a standalone lookup verb and is not wired into create/update pre-flight**, continuous with M2's deliberate exclusion of user-typed fields from the allowed-value pre-check — the visible user set is token-scoped, so a client-side refusal would invent a rejection the server would not make.
2. **✅ `issues links add` / `remove`**, plus links on `issues get`. The consumer's guide requires `parentIssue` for Sub-Tasks; today parentage is prose in descriptions ("Part of IONDEV-867") and invisible to any query. Grammar: `issues links add ISSUE --to ISSUE --type "subtask of"` — **the direction phrase is the type**, which the probe confirms is unambiguous. Bad phrases reuse the `difflib` near-miss plus `_die_bad_value` machinery M2 already shipped, so link errors need no new translation code. No `issues links list`; `issues get` covers reads. Three probe-driven changes from the approved design: **link ids are read off `GET /issues/{id}/links`, not built by suffix arithmetic**; **empty link slots are filtered out** of `issues get` (the endpoint returns all 7 regardless); and **self-links are refused client-side at exit 6**, because the server returns 200 and silently creates nothing.
3. **✅ `--offset`** on every `--limit` verb (widened from the two named here) and **`--select`** on `issues search`. The current limit silently truncates, so a sprint rollup is quietly wrong rather than obviously wrong. Paging matters more than projection. **`--select` filters the CLI's own flattened output on dotted paths** (`idReadable,summary,fields.State`), applied after shaping, so it cannot emit a wire shape a consumer has not seen. Truncation is signalled by fetching `$top = limit + 1`, returning `limit`, and emitting `has_more` — which requires an **envelope** (`{"items": […], "has_more": bool}`) and is therefore a **breaking output change**, the second in this series after M2's `canBeEmpty` rename.

**Done when:** an agent can assign to a person it looked up, file a Sub-Task linked to its parent, and page a full sprint without truncation. **All three are demonstrable on ION** — link types are instance-global and paging is project-independent, so unlike M2's acceptance criterion this one is not blocked by the ION-only constraint.

### ✅ M3 verified live against ION — 2026-07-28

Exercised through the installed CLI on two throwaway ION issues (ION-1437, ION-1438), both deleted afterwards and confirmed gone (`GET` → 404).

| Check | Result |
|---|---|
| `issues links add --type "subtask of"` | ✅ exit 0; **reciprocity through the CLI** — `subtask of → ION-1438` on one side, `parent for → ION-1437` on the other |
| `issues links remove` | ✅ exit 0; both sides cleared, and the `links` key is absent (not `[]`) when an issue has none |
| Undirected type (`relates to`) | ✅ renders as `relates to` on **both** sides — confirms `_link_phrase` never consults the empty `targetToSource` on a `BOTH` link, the one regression the read shaper could plausibly have |
| Self-link refused | ✅ exit **6**, no request sent |
| Unknown phrase (`subtask off`) | ✅ exit **6**, hint `did you mean: subtask of?` |
| Remove a link that is not there | ✅ exit **3**, `ION-1437 has no 'relates to' link to ION-1438` — blames the link, not the issue |
| Nonexistent target | ✅ exit **3**, `no issue named 'ION-99999'` — **on both `add` and `remove`.** They translate different upstream statuses to the same message: `add`'s POST returns 400, `remove`'s target lookup returns 404. The first pass shipped this for `add` only, leaving `remove` on the generic "YouTrack resource not found"; the final review caught the asymmetry. |
| `--limit 2` / `--limit 2 --offset 2` | ✅ `has_more: true` on both, and page 2 returns different keys — real paging, not a repeated first page |
| `--select idReadable,fields.State` | ✅ trims to exactly those keys inside the envelope |
| `--offset -1`, `--select idReadabel` | ✅ exit **6** each |
| `users find royz` | ✅ returns `s.royz` in the envelope |

**Still unproven, and now unprovable under the ION-only constraint:** exit **7** for a workflow-rule rejection of a link. No link rule fired on ION, and whether one exists on IONDEV cannot be asked. Tag: **UNTESTED** — do not claim exit 7 coverage for links.

## M4a — Close the unblocked tail — ⬜ NEXT

Groups A and B from "What remains", plus `projects find` paging. **7 parity items, 5 new verbs, no probe needed** — every gap is a known endpoint with no unmeasured behaviour. A and B ship together because they have a real dependency: the comment-visibility flags in A want `groups find` from B to validate group names.

1. `articles update --parent` · `articles search QUERY` · `issues comments add --permitted-users/--permitted-groups` (Group A)
2. `projects get ID` · `searches list` · `groups find [SUBSTRING]` · `groups members GROUPID` (Group B)
3. `projects find` paging — the last verb that still truncates silently (`$top: 100`, no `--limit`/`--offset`/`has_more`)

**Done when:** parity is **20 of 23**, with only `manage_issue_tags` and `log_work` outstanding, and no list verb truncates without saying so.

## M4b — Tags and work items — ⬜ AFTER M4a

Group C. The last two MCP gaps, and the only ones with unmeasured behaviour. **Both are probeable on ION — confirmed 2026-07-28 by reading the live instance**, so unlike M2's Group 1 this milestone is not blocked by the ION-only constraint.

**Step 0 — probe first, on ION.** This is the class that has already burned the project twice: M2's step 0 falsified four documented-looking claims, and M3's probe found `readOnly: true` is not a write gate — which would have shipped `subtask of` as unavailable with every test passing. Do not build either verb from the REST docs.

*Tags — measured so far:* instance-scoped, not project-scoped (`GET /issueTags` returned 17 visible), each with an `owner` and a `visibleFor` group; one tag has `visibleFor: None`, i.e. private to its owner. **Unknown, and each changes the design:** whether writing an unknown tag name creates it or returns 400 (decides pre-flight versus error translation); whether a tag owned by another user can be applied; what happens with a tag the token cannot see; whether add is idempotent as link-add was; and the removal path's shape.

*Work items — measured so far:* time tracking is **enabled** on ION, with five `workItemTypes` (`Development`, `Testing`, `Documentation`, `Investigation`, `Implementation`). **Unknown:** the duration write shape — `{"minutes": N}` versus `{"presentation": "1d 4h"}` — and its round-trip fidelity. M2 measured that a `period` custom field round-trips byte-exact as `presentation` while reading back as **minutes**, with workday length coming from server-side project settings, so a work item very likely carries the same dual representation. Also unknown: whether `type` is required, and how the work item's date behaves relative to the `date` normalization M2 measured.

**Scope boundary, so M4b does not duplicate what already ships.** ION's `estimate` and `timeSpent` are `PeriodProjectCustomField`s, which means the `Estimation` and `Spent time` *fields* are already writable through `issues update --field` on the M2 marshalling path. M4b is about the additive **work-item log** — `POST` a new entry, list the entries — which is what `log_work` actually covers. Do not add a verb that re-writes those two fields.

**Done when:** parity is **22 of 23** at parity plus 1 (`change_issue_assignee`) covered by design — every JetBrains predefined tool accounted for.

---

# What remains — as of 2026-07-28, M1–M3 merged

**9 items close the last 9 MCP gaps, one-to-one** — Group A 3, Group B 4, Group C 2. A tenth work item, `projects find` paging, closes no parity gap and is tracked separately below.

**All of Groups A and B are unblocked; Group C is not.** Every A and B gap is a known endpoint on a surface the CLI already models, with no unmeasured behaviour. Group C's two items both touch bundle-backed or unit-parsed values and need a step-0 probe first — see Group C for what specifically is unknown.

Grouped by what a caller can do afterwards, cheapest first. **Groups A and B are delivered as M4a; Group C is M4b.** The letters are substructure, not a parallel numbering scheme.

## Group A — three near-misses on verbs that already exist

Smallest change per unit of parity: each is a flag or a query swap on shipped code, not a new verb.

| Gap | Change | MCP tool closed |
|---|---|---|
| `articles update --parent` | add the flag; the create path already accepts `--parent`, so the marshalling exists | `update_article` → parity |
| `articles search QUERY` | new verb, but `articles list`'s projection and paging are reusable; it is a query swap | `search_articles` → parity |
| `issues comments add --permitted-users/--permitted-groups` | comment visibility; needs `groups find` first if group names are to be validated | `add_issue_comment` → parity |

## Group B — three read verbs, no write risk

| Gap | Notes | MCP tool closed |
|---|---|---|
| `projects get ID` | one GET, projection only | `get_project` |
| `searches list` | saved searches; a plain listing | `get_saved_issue_searches` |
| `groups find` + `groups members ID` | two verbs, one MCP tool each; `groups members` is the "exactly one op → the sub-resource is the verb" case from the grammar rules | `find_user_groups`, `get_user_group_members` |

## Group C — two write verbs that need a probe first

These are the only remaining items with unmeasured behaviour, and both touch bundle-backed or unit-parsed values — the class that cost M2 its step-0 probe.

| Gap | Why it needs measuring | MCP tool closed |
|---|---|---|
| `issues tags add` / `remove` | tags are bundle-backed and instance-scoped; whether adding an unknown tag creates it or 400s is unmeasured, and that decides whether a pre-flight or an error translation is right | `manage_issue_tags` |
| `issues work log` / `list` | duration parsing is the risk. M2 measured that `period` fields round-trip as `presentation` (`1d 4h`) while reading back as **minutes**, with the workday length coming from server-side project settings — so a work-item duration almost certainly has the same dual representation, and asserting minutes would test a server config value rather than CLI behaviour | `log_work` |

**Probe before building Group C**, on ION, in the shape M2 and M3 used: write one of each, read it back, record measured-versus-inferred. Group C is also where `--project` scoping matters, since tag and work-item types are project-configured.

## Not MCP parity, but tracked here

- **`projects find` paging.** It hardcodes `$top: 100` with no `--limit`/`--offset`/`has_more`, so it is the one list verb that still truncates silently — the exact bug M3 removed everywhere else. Cheap, and it closes the inconsistency M3's D6 deliberately widened to avoid.
- **Attachments.** In neither surface. They belong to the 44-tool community server, not JetBrains'. Only a parity item if the target changes (see below).

## Gaps that are closed by decision, not by work

Do not schedule these; they are answered.

- **`change_issue_assignee`** — covered by `issues update --field Assignee=…`. A dedicated verb for one field invites one per field.
- **`issues links list`** — deliberately absent; `issues get` returns links.
- **Sort on `issues search`** — expressible as `sort by:` inside the YouTrack query; a flag would duplicate the query language.

## Two open gaps that are *not* parity gaps

Both are evidence gaps, and both are blocked by the ION-only constraint rather than by effort:

- **Exit 7 for a workflow-rule rejection of a link** — UNTESTED. No link rule fired on ION.
- **M2's `text`-on-create criterion** — open. ION carries no `text` field. Needs IONDEV write authorization or a `text` field added to ION.

## If the parity target changes

This document measures against JetBrains' **23 predefined MCP tools**. The **44-tool community MCP server** is a strictly larger surface — it adds attachments, and a wider spread of admin and bundle operations. Adopting it as the target would roughly double the remaining work and reopen items this plan closed by decision. That is a scope decision for the owner, not a milestone; nothing below assumes it.

## Sequencing rationale

M1 before M2 is not a preference. Writing custom fields without a schema means guessing both `$type` and the legal value — `Severity 1` vs `S1`, `Normal` vs `Medium` — and every guess fails at the write, against live project data. M1 is cheap and turns M2 from guesswork into a lookup.

M2 before M3 because assignment, linking, and paging are conveniences atop a capability that can already express an issue's state. Shipping `users find` while `update` still only moves State would be motion without progress.

# Verification

Per the repo loop: change → `capabilities audit youtrack --from .` → **reindex the catalog** (below) → commit → reinstall → verify in the ionwater consumer **as a sub-agent**, never in the main context.

## Reindex `.capability-source/catalog.json` in the same PR

**Any change to a capability's payload or its manifest `summary` must reindex the source catalog in the same commit.** `.capability-source/catalog.json` records a `payload_sha256` and `summary` per capability, and `capabilities install <name> --source <id>` refuses with `catalog_drift` (exit 7) when either disagrees with the payload. M1 shipped without this and left `main` uninstallable until a follow-up PR; three review passes missed it because the catalog sits outside `capabilities/<name>/` and nothing in the plan pointed at it.

`capabilities source index <id>` does **not** fix a git-backed source: it rewrites the cached clone's catalog, and the next `install` re-clones and restores the committed one. The committed file is what has to change.

The reliable way to regenerate it — the hash covers `capabilities/<name>/` recursively, skipping `meta.json`, `stub`, `manifest.json`, `__pycache__`, and `.pyc`/`.pyo`/`.session*`, feeding `<relpath>\0<bytes>\0` per file in sorted order into one sha256:

1. Recompute every entry with that algorithm and confirm the **unchanged** capabilities reproduce byte-for-byte. If they do not, the algorithm has drifted from `_payload_sha256` in `bin/capabilities` — read it there rather than guessing.
2. Update only the changed capability's `payload_sha256` and `summary` (the latter from `<name> manifest --json`).
3. Write with the manager's own conventions — `json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"` — so the diff carries no reformatting noise.
4. Cross-check against `capabilities source index <id>` run on the same tree, reading the cached catalog **before** any `install` re-clones over it.

## Tests

`tests/test_youtrack.py` covers the existing verbs (105 as of M2); extend per milestone. These are the load-bearing ones for M1–M2:

- **One per field type** in the marshalling table — the one place a wrong constant produces a plausible-looking request that YouTrack rejects. Assert the exact request body per type; the measured shapes above are the fixtures.
- **`date` normalization**: writing `2026-08-15` must send noon UTC (`1786795200000`), and reading noon UTC back must render `2026-08-15`. Assert the *calendar date*, never epoch equality across a round trip — and cover `date and time` separately, where epoch equality *does* hold.
- **The COMMANDS/docstring drift tests** described above.
- **`WRITE_VERBS ⊆ COMMANDS`** — the two are coupled by stringly-typed joined paths, so a typo silently disables the `allow_write` gate for that verb.
- **Error translation**: a 400 carrying `{1}` and an entity id, and the HTTP **500** for an unknown field name, must both surface as exit `6` naming the field — the 500 mapping is the one most likely to regress into exit `5`.
- **Workflow rejection**: a 400 with `error_type: workflow` must surface `error_rule_name` and must *not* be reported as an input error.

Dropped from this list: the original client-side atomicity test. The server rolls a mixed-validity batch back on its own (measured), so there is nothing client-side to assert — and under option (C) the request is deliberately sent.

### M3 tests

The measured link model above supplies the fixtures. Load-bearing, in rough order of what would hurt most if it regressed:

- **`readOnly` is not a gate.** A fixture link type with `readOnly: true` must still resolve and write. This is the regression guard for the finding that would have silently broken the milestone; without it, a later "tidy-up" that respects the flag disables `subtask of` and every other test still passes.
- **Self-link is refused before any HTTP call.** `--to` equal to the issue must exit `6` **and issue no request** — asserting the exit code alone is not enough, because the server returns `200` for a self-link, so a client that sent it would look correct to a naive test.
- **All 7 phrases resolve** to the right link id and direction, case-insensitively. An unknown phrase exits `6` with a near-miss over the legal phrases. **An ambiguous phrase — two types sharing one — must fail loudly, not pick.** Phrase uniqueness is measured on one instance only; a custom link type could collide, and this test is what keeps that from becoming a silent mis-link.
- **Empty link slots are filtered.** An issue payload carrying all 7 slots with one populated must emit exactly one `links` entry.
- **`remove` resolves the internal id.** It must perform the resolution GET and send DELETE with the internal id; a readable key in the DELETE path is the measured 404.
- **`remove`'s 404 is retranslated.** A 404 whose message names the *target issue* must surface as exit `3` naming the **link**, since the issue in that message demonstrably exists.
- **`has_more` boundary.** Server returns exactly `limit` → `has_more: false`; server returns `limit + 1` → `has_more: true` **and exactly `limit` items emitted**. The off-by-one here is the whole point of the flag.
- **`--offset N` sends `$skip=N`** on both `issues search` and `issues comments list`.
- **`--select` dotted paths.** `fields.State` selects into the nested map. **Decision: a key that is neither a known core attribute nor `fields.<anything>` exits `6` with a near-miss; a `fields.X` naming a field absent from a given issue is simply omitted for that issue, not an error** — custom fields legitimately differ per project, so a search spanning projects must not fail on the first issue lacking one. Without this split, either a typo silently yields empty output or a valid cross-project search dies.
- **`issues links add` / `remove` ∈ `WRITE_VERBS`** — covered transitively by the existing `WRITE_VERBS ⊆ COMMANDS` test, but assert membership directly too: that test catches a typo in a path, not an omission.

## Consumer refresh

After a surface change lands and is installed, rebuild the ionwater consumer's context. `contextkit build` alone writes **only the codex target** — the claude target needs `contextkit build --target claude` explicitly. Both files are gitignored build artifacts, so there is nothing to commit; they must be rebuilt per machine.

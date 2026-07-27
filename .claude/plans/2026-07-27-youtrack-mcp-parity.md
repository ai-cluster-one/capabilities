# Design + plan — `youtrack` MCP parity and noun-verb CLI

**Status:** M1 shipped (PR #14, catalog repair in #15). M2–M4 outstanding. This doc is both the surface contract and the milestone plan; keep them together so they cannot drift.

**Base:** `upstream/main` (`ai-cluster-one/capabilities`), which carries the knowledge-base article verbs merged in PR #13. The `zjor/capabilities` fork's `main` runs behind upstream — branch from `upstream/main`, not `origin/main`.

**Live-verified against `ion.youtrack.cloud` / IONDEV (project `0-6`, 45 custom fields).** The marshalling and output sections below are measured, not inferred; only the *write* direction remains unproven (see the note there).

## Why

The capability was built issue-and-article shaped: read a task, comment, flip State, author articles. JetBrains' predefined MCP tool set is the surface an agent is now expected to have, and the gap is not evenly spread — it concentrates in **custom fields**, which is where consuming projects keep the material that makes an issue actionable.

The forcing example is the ionwater.io consumer. Its `docs/process/youtrack-field-guide.md` defines ~45 custom fields on the IONDEV project, ~18 required before an issue may enter a sprint (Type, Subsystem, Team, Assignee, Readiness, Priority, Effort Level, Points, Original Estimate, Acceptance Criteria, Definition of Done, …). The capability today **writes exactly one of them** (State) and **reads none** — `ISSUE_FIELDS` omits `customFields` entirely. An agent using this CLI cannot create a sprint-ready issue and cannot check whether an existing one is ready.

A second, smaller problem: the surface is a flat list of unique first-level verbs (`task`, `issues`, `article-create`, `article-comments`). That scales badly — full parity would mean ~30 top-level verbs with no grouping. This design converts it to noun-verb in the same pass.

## Baseline: measured coverage

Against the **23** predefined MCP tools listed on [Predefined MCP Tools](https://www.jetbrains.com/help/youtrack/devportal/predefined-ai-tools.html), enumerated in page order and counted directly: 6 at parity, 6 partial, 11 absent, plus 2 CLI-only verbs. (An earlier reading reported 24; that was a miscount. There is no 24th tool, and the set below is complete.)

| MCP tool | today | status |
|---|---|---|
| `get_current_user` | `whoami` | parity |
| `find_projects` | `projects --query` | parity |
| `get_article` | `article` | parity |
| `create_article` | `article-create` (also `--parent`) | parity+ |
| `update_article` | `article-update` | near — cannot re-parent |
| `add_issue_comment` | `comment` | near — no `permittedUsers`/`permittedGroups` |
| `get_issue_comments` | `comments --limit` | partial — no offset |
| `get_issue` | `task` | partial — **no custom fields at all** |
| `search_issues` | `issues QUERY --limit` | partial — no offset, 5 hardcoded fields, no sort |
| `create_issue` | `create` | partial — summary/description only |
| `update_issue` | `update --state` | partial — **State only** |
| `search_articles` | `articles` | partial — a listing, not a query |
| `get_issue_fields_schema` | — | absent |
| `find_user` | — | absent |
| `link_issues` | — | absent |
| `change_issue_assignee` | — | absent |
| `manage_issue_tags` | — | absent |
| `log_work` | — | absent |
| `get_project` | — | absent |
| `get_saved_issue_searches` | — | absent |
| `find_user_groups` | — | absent |
| `get_user_group_members` | — | absent |
| `create_draft_issue` | — | absent |
| — | `article-comments`, `article-comment` | CLI-only; MCP has no article-comment tools |

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
| `issues get ISSUE` | M1 |
| `issues search QUERY [--limit N] [--offset N] [--select F,F]` | M1 / M3 |
| `issues create --project ID --summary S [--description TEXT\|-] [--field N=V]… [--fields JSON\|-] [--draft]` | M2 |
| `issues update ISSUE [--summary S] [--description TEXT\|-] [--field N=V]… [--fields JSON\|-]` | M2 |
| `issues comments list ISSUE [--limit N] [--offset N]` | M1 |
| `issues comments add ISSUE [--text TEXT\|-]` | M1 |
| `issues links add ISSUE --to ISSUE --type TYPE` | M3 |
| `issues links remove ISSUE --to ISSUE --type TYPE` | M3 |
| `issues tags add ISSUE TAG` · `issues tags remove ISSUE TAG` | M4 |
| `issues work log ISSUE --duration D [--date D] [--type T] [--text TEXT\|-]` | M4 |
| `issues work list ISSUE` | M4 |

Links are returned by `issues get`; there is deliberately no `issues links list`.

## articles

| Command | Milestone |
|---|---|
| `articles get ID` | — |
| `articles list [--project ID] [--limit N] [--offset N]` | — |
| `articles search QUERY [--limit N] [--offset N]` | M4 |
| `articles create --project ID --summary S [--content TEXT\|-] [--parent ID]` | — |
| `articles update ID [--summary S] [--content TEXT\|-] [--parent ID]` | M4 (re-parent) |
| `articles comments list ID [--limit N]` · `articles comments add ID [--text TEXT\|-]` | — |

`articles list` keeps `list` rather than `find` because it is a project-scoped listing, not a match.

`articles create --project` becomes **required**, tightening today's optional flag: the current code sends `{"project": {"id": …}}` unconditionally, so omitting it fails server-side regardless. Making it required moves that failure to argparse, as exit `6` with a usable message.

## projects · users · groups · searches

| Command | Milestone |
|---|---|
| `projects find [SUBSTRING]` | — |
| `projects get ID` | M4 |
| `projects fields list ID` · `projects fields get ID NAME` | **M1** |
| `users me` · `users find SUBSTRING` | M1 / M3 |
| `groups find [SUBSTRING]` · `groups members GROUPID` | M4 |
| `searches list` | M4 |

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
| `date` | no | `DateIssueCustomField` | epoch ms | epoch ms | Due Date |
| `date and time` | no | `SimpleIssueCustomField` | epoch ms | epoch ms | Incident Start Time |
| `text` | no | `TextIssueCustomField` | `{"text": …}` | `text` | Acceptance Criteria |

**The two traps in that table:** `date` maps to `DateIssueCustomField` but `date and time` maps to `SimpleIssueCustomField` — same-looking types, different `$type`. And bundle-backed values carry distinct element types on read (`EnumBundleElement`, `StateBundleElement`, `OwnedBundleElement`, `VersionBundleElement`), all of which flatten by `name`; only users flatten by `login`.

`float` and `string` do not occur in IONDEV; both are expected to be `SimpleIssueCustomField` like `integer`, and are the only rows in this table not directly measured.

**Write direction is not yet proven.** Every shape above is confirmed on read; the write column assumes YouTrack accepts the same shape it emits, which is its documented convention but was not exercised against the live instance (doing so mutates real IONDEV data). M2's first task is to confirm it on a throwaway draft issue before building on it.

**Validation is atomic and ahead of the wire.** Every named field and value is checked against the schema *before* any request goes out. Unknown field name → exit `6` naming near-misses. Value outside a bundle → exit `6` listing what is allowed. Never a partial write. Correctness is never delegated to YouTrack, which silently ignores an unrecognized `customFields` entry rather than rejecting it.

**Fields are scoped by issue Type, not just by project — measured.** IONDEV declares 45 project custom fields, but `IONDEV-509` (a Task) carries only 34; the 11 missing are the bug/incident set (`Severity`, `Steps to Reproduce`, `Reported In`, `Incident Start Time`, `Root Cause`, `Blocked Reason`, …), which appear on `IONDEV-974` (a Bug). So the project schema is a *superset*: validating a name against it alone will accept `Severity` on a Task, which YouTrack then silently drops. On update, validate against the fields actually present on the target issue; on create, against the fields the chosen `Type` carries. Report a field that exists in the project but not on this issue with a distinct message — it is a different mistake from a typo.

**Schema resolution: fetch per invocation, do not cache.** A write costs one extra `GET /admin/projects/{id}/customFields` before the `POST`. One round trip, no invalidation problem, and no manifest change — the capability declares `state: false`, and a schema cache would flip it to `true` and drag in the whole staleness question. Revisit only if the extra GET measurably hurts.

**Write gating.** Every new write path respects `allow_write`, per the standard.

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

## M2 — Set the fields

The milestone that makes the capability usable, and the only one with real design risk.

0. **Prove the write direction first**, on a throwaway draft issue: one write per `$type` in the marshalling table, read back, compare. Everything else in M2 builds on that table being right.
1. `issues update` with `--field` / `--fields`, plus `--summary` / `--description` (today impossible outside the web UI).
2. `issues create` on the same marshalling path, so an issue can be born sprint-ready in one call.
3. Type-aware marshalling per the table above, replacing the hardcoded `{"name": "State", "$type": "StateIssueCustomField", …}`.
4. Atomic pre-flight validation with near-miss and allowed-value errors.
5. `issues create --draft`.

**Done when:** one `issues create` call produces an IONDEV issue satisfying the consumer's Ready-for-Sprint rules, and `issues update --field` moves each afterwards.

## M3 — Work the board

1. **`users find`** — a hard prerequisite for Assignee/Requestor/Approver. M2 can marshal `{"login": …}` blind, but nothing can confirm the login exists.
2. **`issues links add` / `remove`**, plus links on `issues get`. The consumer's guide requires `parentIssue` for Sub-Tasks; today parentage is prose in descriptions ("Part of IONDEV-867") and invisible to any query.
3. **`--offset`** on `issues search` and `issues comments list`; **`--select`** on `issues search`. The current limit silently truncates, so a sprint rollup is quietly wrong rather than obviously wrong. Paging matters more than projection.

**Done when:** an agent can assign to a person it looked up, file a Sub-Task linked to its parent, and page a full sprint without truncation.

## M4 — Long tail

Fill on demand, not speculatively: `articles search` · `articles update --parent` · `projects get` · `issues tags` · `issues work` · `searches list` · `groups find` + `groups members` · `permittedUsers`/`permittedGroups` visibility on comments and articles.

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

`tests/test_youtrack.py` covers the existing verbs; extend per milestone. Four tests are load-bearing:

- **One per field type** in the marshalling table — the one place a wrong constant produces a plausible-looking request that YouTrack rejects or, worse, silently drops.
- **The COMMANDS/docstring drift tests** described above.
- **`WRITE_VERBS ⊆ COMMANDS`** — the two are coupled by stringly-typed joined paths, so a typo silently disables the `allow_write` gate for that verb.
- **Atomicity**: a create with one invalid field among many issues no HTTP request at all.

## Consumer refresh

After a surface change lands and is installed, rebuild the ionwater consumer's context. `contextkit build` alone writes **only the codex target** — the claude target needs `contextkit build --target claude` explicitly. Both files are gitignored build artifacts, so there is nothing to commit; they must be rebuilt per machine.

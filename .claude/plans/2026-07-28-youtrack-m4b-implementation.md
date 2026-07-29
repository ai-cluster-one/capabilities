# youtrack M4b — Tags and Work Items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the last two MCP parity gaps — `manage_issue_tags` and `log_work` — taking parity from 20 to **22 of 23 at parity plus 1 covered by design**, so every JetBrains predefined tool is accounted for. Also remove the last silently-truncating list verb (`projects fields list`) and add the self-parent guard on `articles update --parent`.

**Architecture:** All changes land in the single file `capabilities/youtrack/bin/youtrack`. Four new verbs in two groups, plus two tail items. The shape of both new write paths is the same one the probe found twice: **the API takes an id and rejects a name, and an unknown name returns the byte-identical 400** — so each write verb resolves a caller-facing *name* to an id client-side before writing, exactly as `--permitted-groups` already does. Tag resolution is instance-wide and needs no project; work-item-type resolution is **project-scoped** and therefore costs one read of the issue first.

**Tech Stack:** Python 3 single-file CLI with PEP-723 inline deps (`httpx`), argparse driven by the declarative `COMMANDS` table, pytest against a real local `ThreadingHTTPServer` (no mocking library).

## Global Constraints

- **Design source of truth:** `.claude/plans/2026-07-27-youtrack-mcp-parity.md`, sections "Tags and work items — measured 2026-07-28 (M4b step 0, closed)" and "M4b — Tags and work items". Every endpoint and error string below is measured live. **Do not re-derive any of it from the REST docs** — the probe falsified three of this milestone's own stated premises, and one plausible implementation path (`POST /issues/{id}` with a `tags` array) turned out to silently destroy tags.
- **One file.** Do not split `bin/youtrack`. M4b adds ~170 lines of domain code; the revisit trigger is ~1,500 domain lines and the file is at ~1,980 total of which ~534 are generated fences.
- **Generated contract fences must not be edited** — `capability core` and `connections`. Locate them by their `# >>> contract:` / `# <<< contract:` markers rather than by line number, since every task shifts the numbers.
- **Exit codes are fixed:** `0` success, `2` auth/config, `3` not found, `4` policy refusal, `5` network/server, `6` input, `7` workflow-rule rejection.
- **Output contract:** custom-field values flatten to scalars, arrays, or null — never YouTrack's `{"name": …, "$type": …}` wire shape. `$type` never appears in output.
- **Every verb taking `--limit` returns `{"items": [...], "has_more": bool}`** via the shared `_page_params` / `_paged` pair. New list verbs get this from the start — do not add a verb that returns a bare array.
- **Every new write verb must be added to `WRITE_VERBS`, in the same step as its `COMMANDS` row.** An existing test asserts `WRITE_VERBS ⊆ COMMANDS`; adding one without the other turns the suite red. (M3 lost a round to exactly this.) M4b adds **three** write verbs: `issues tags add`, `issues tags remove`, `issues work log`.
- **The module docstring is the single source of truth for the command surface.** Two existing tests (`test_every_command_is_documented`, `test_every_documented_command_exists`) fail if it disagrees with `COMMANDS`.
- **Any payload change requires reindexing `.capability-source/catalog.json` in the same commit** (Task 6). Skipping it left `main` uninstallable after M1.
- **Run the test suite in the foreground with an explicit timeout of at least 400000 ms.** The suite takes ~270s while the default Bash tool timeout is 120s, so **it gets auto-backgrounded regardless of intent** unless the timeout is raised — which is what actually happened to every implementer who appeared to background it deliberately. Command:
  `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20` with `timeout: 400000`.
- **Live checks run from `/Users/zjor/projects/ion/agents`, never from the capabilities repo.** The `ionwater` connection lives in that project's envelope and is invisible elsewhere, so a call from the wrong directory silently falls back to a *different YouTrack instance* that also has `allow_write: true`. This has already created stray issues on the wrong server twice. `--connection ionwater` is **not** a workaround — that connection does not resolve outside the project.
- **All live experiments use the ION project only. IONDEV must not be touched** (owner decision). Verify `project.shortName == "ION"` on anything created, delete every probe artifact, and verify the deletion returns 404. The CLI has no delete verb — use the API directly.

---

## Measured facts this plan is built on

All measured live against ION on 2026-07-28; the full record with error strings is in the parity plan. Fixtures come from here, not from the docs.

### Tags

| Endpoint / case | Measured |
|---|---|
| `GET /issueTags` | `id`, `name`, `owner(login)`, `visibleFor(name)`; 17 visible, all names distinct; `query=` honoured server-side |
| `POST /issues/{id}/tags` `{"id": tagId}` | **200**, echoes `{id,name}` |
| the same with `{"name": …}` | **400** `YouTrack is unable to locate an Tag-type entity unless its ID is also provided` |
| an **unknown** name | **the byte-identical 400**, and no tag is created |
| id + name disagreeing | 200, **the id wins** |
| add twice | **200, idempotent**, no duplicate |
| a tag owned by another user | **200**, applied |
| `DELETE /issues/{id}/tags/{tagId}` | **200**, empty body |
| the same DELETE again | **404** `Entity with id 6-3 not found` — names the **tag**, which exists |
| unknown issue on any tag path | **404** `Entity with id ION-999999 not found` — names the **issue** |
| **`POST /issues/{id}` with a `tags` array** | 200 and **REPLACES the whole tag set** — it wiped two tags the caller never named |
| `GET /issues/{id}?fields=tags(id,name)` | works; `[]` when the issue has none |

### Work items

| Endpoint / case | Measured |
|---|---|
| `POST /issues/{id}/timeTracking/workItems` `{"duration":{"minutes":90}}` | **200**; reads back **both** `minutes: 90` and `presentation: "1h 30m"` |
| `{"duration":{"presentation":"1d 4h"}}` | 200 → reads back **`"12h"` / 720** — **presentation is re-rendered, not byte-exact** |
| output units | **hours and minutes only** — `2d`→`16h`, `1w`→`40h`, 5000 min→`83h 20m`. Never days or weeks |
| **`{"presentation":"90"}`** | 200 → **`minutes: 5400`, `"90h"` — a unit-less number is HOURS** |
| `"1h30m"`, `"3h 15m"`, `" 45m "`, `"1H30M"` | 200 — whitespace-tolerant, case-insensitive |
| `"1.5h"`, `"-5m"`, `"1h 2m 3s"` | **400** `Value … cannot be parsed` |
| duration omitted, `minutes: 0`, `minutes: -30`, `"0m"` | **400** `Work duration can not be negative or empty` |
| `{"duration": 90}` | 400 type mismatch — duration is always an object |
| `{"minutes":60,"presentation":"5m"}` | **400** `Conflict in period value` — send exactly one |
| `type` omitted | **200**, `type: null` — **type is optional** |
| `{"type":{"name":"Development"}}` | **400** `YouTrack is unable to locate an WorkItemType-type entity unless its ID is also provided`; an unknown name is identical |
| `{"type":{"id":"139-1"}}` | 200 |
| **`Review` (`139-5`), instance-legal, on project 0-1** | **400** `invalid_properties` — *"The selected work type is not supported for issues in this YouTrack project"* |
| `GET /admin/timeTrackingSettings/workItemTypes` vs `GET /admin/projects/0-1/timeTrackingSettings/workItemTypes` | **6 types vs 5** — the project set is the authority |
| `date` | snapped to **00:00 of the UTC day**; `12:00Z`/`23:00Z`/`01:00Z` all land on 00:00Z of the **same** date, no day shift; omitted → today 00:00Z |
| an ISO date string | **400** — *"only accepts kotlin.Long-type value"* |
| `text` | round-trips exactly, newlines preserved |
| `author` | defaults to the token's user |
| `GET …/workItems` with `$top`/`$skip` | both honoured; `[]` when none |
| unknown issue | 404 on both log and list |
| **`Spent time`** | **not writable** — 400, *"automatically calculated based on time tracking settings"*; derived from the work-item log |

### Article re-parenting

| Case | Measured |
|---|---|
| **self-parent** (by internal id **and** by readable key) | **400** `invalid_properties` — *"There is a recursive chain in the table of contents that causes an article to be a sub-article of itself"*, nothing applied. So the server already refuses it and 400 already maps to exit 6 |
| a 2-cycle | **HTTP 500, empty body** — out of scope to guard |
| a nonexistent parent id | **200 and silently clears the existing parent.** The CLI is protected only because `_parent_article_id` resolves the ref with a GET first — **do not remove that read** |

**The one thing ION cannot decide:** whether the work-item date snap is to **UTC** midnight or to the **profile timezone's** midnight. This token's profile is `Etc/UTC`, offset 0, so the two are indistinguishable. Tag: **UNPROVABLE-HERE** — it is why decision **F12** sends noon rather than midnight.

---

## Decisions locked before implementation

| # | Decision | Why |
|---|---|---|
| F1 | `issues tags add/remove ISSUE TAG` takes the tag **name**, resolved client-side to an id | Measured: the API rejects a name, and an unknown name returns the *identical* 400 — so the server can never tell the caller which mistake they made. Same situation as `permittedGroups` in M4a. |
| F2 | An unknown tag name exits **6** with a `difflib` near-miss, and the message says the tag is **not visible to this token** rather than that it does not exist | A tag private to another user is invisible here and indistinguishable from a typo (UNPROVABLE-HERE). Asserting non-existence would be a claim the CLI cannot support. |
| F3 | An **ambiguous** tag name is refused at exit 6, never resolved to whichever row a dict comprehension kept last | Names are distinct on ION but nothing server-side guarantees it. Mirrors `_resolve_group_ids` and `_resolve_link`, both of which already refuse rather than pick. |
| F4 | **Never** use `POST /issues/{id}` with a `tags` array | Measured replace semantics: it wiped two tags the caller never named. The additive `/tags` sub-resource is the only correct path. This needs a source comment — it is the obvious-looking implementation. |
| F5 | `issues tags add` performs **no** idempotency pre-check | Add is idempotent server-side (measured). A pre-check would cost a request to prevent nothing. |
| F6 | `issues tags remove`'s 404 is translated to exit **3** naming the tag *and* the issue — distinguished from an unknown-issue 404 by whether the message names the **tag id** | Both cases are 404 and differ only in message content (measured: `Entity with id 6-3 not found` vs `Entity with id ION-999999 not found`). Passing the first through would claim the tag does not exist when the issue merely does not carry it. |
| F7 | Both tag verbs **return the issue read back**, and `issues get` gains a flattened `tags` list, **omitted when empty** | Mirrors `issues links add/remove` exactly (M3), and without it add/remove is unobservable. Omitting-when-empty matches `_flatten_links`. |
| F8 | `--duration` is **required**, and a **unit-less numeric value is refused at exit 6** before any request | Measured: `90` means **90 hours**, so `--duration 90` intending minutes over-logs by 60×. This is deliberately stricter than the server — justified because the client can detect it with certainty and the failure is silent. Considered and rejected: passing it through with a documentation note, which leaves the 60× error reachable. |
| F9 | The duration is sent as `{"presentation": <raw>}` — never `{"minutes": …}`, and never both | The server owns the period grammar and the workday length; reimplementing either would drift from project settings. Sending both returns `Conflict in period value` (measured). |
| F10 | Output keeps `duration` as `{"minutes": N, "presentation": "…"}`, with `$type` stripped | Measured: `presentation` is re-rendered and `minutes` for day/week inputs reflects project workday settings, so each carries information the other loses. This is not a custom-field value, so the flatten-to-scalar rule does not apply — `visibility`, `project` and `author` are already objects in this CLI's output. |
| F11 | `--type` is resolved against the **project's** work-item types, reached via one read of the issue, and only when `--type` is given | Measured: `Review` is instance-legal and project-rejected, so resolving against the instance set would send a legal-looking id the project refuses. With no `--type`, `issues work log` stays at exactly one request. |
| F12 | `--date` accepts `YYYY-MM-DD` and sends **noon UTC**, reusing `_epoch_ms_noon_utc`; output renders the read-back epoch as `YYYY-MM-DD` in UTC | The server snaps to 00:00 of the day, which is a day **boundary**, and whether that snap uses UTC or the profile timezone is unprovable here. Noon carries ~12 hours of margin in either direction, so the calendar date — the only thing a caller means — survives either semantics. The round trip is therefore *not* byte-exact, and tests must assert the calendar date, never epoch equality. |
| F13 | `issues work list` pages and envelopes; `issues work log`, `issues tags add` and `issues tags remove` join `WRITE_VERBS` | Standard for this surface. |
| F14 | `projects fields list` gains `--limit`/`--offset` and the envelope (**BREAKING**), while `_project_fields` keeps `$top: 200` for its internal callers | It is the last silent truncation. But `projects fields get` and both write verbs' validation need the **complete** set — paging the shared helper would silently narrow validation and produce false "unknown field" errors. The paging belongs to the verb. |
| F15 | The self-parent guard on `articles update --parent` compares **resolved internal ids**, fires before the write, and is documented as message quality rather than a correctness fix | `--parent 138-277` against `ION-A-240` is one article in two notations, which a comparison of the raw arguments would miss; both ids are already in hand from reads the verb performs anyway. The server does refuse a self-parent (400 → exit 6), unlike the self-**link** case where it returned 200 and did nothing. |
| F16 | `issues search` output is **unchanged** — tags appear on `issues get` only | `ISSUE_SEARCH_FIELDS` is deliberately lean and already omits `links`, `project`, `reporter` and timestamps. Adding `tags` there would also require widening `_SEARCH_SELECT_KEYS`, i.e. a second output change for no parity gain. |

---

## File Structure

| File | Responsibility in M4b |
|---|---|
| `capabilities/youtrack/bin/youtrack` | All implementation. `TAG_FIELDS`/`WORK_ITEM_FIELDS` beside the other projections; tag verbs and work verbs in the `issues: sub-resources` region after the links block; `_utc_date` extracted beside `_flatten_field`. |
| `capabilities/youtrack/bin/youtrack` (module docstring) | The help contract — SHEBANG makes it the source of truth for the surface. |
| `capabilities/youtrack/tests/test_youtrack.py` | All tests, extending the existing local-HTTP-server pattern. |
| `.capability-source/catalog.json` | `payload_sha256` + `summary` reindex, Task 6, same commit as the last payload change. |
| `.claude/plans/2026-07-27-youtrack-mcp-parity.md` | Mark M4b shipped and recount the parity line, Task 6. |

---

### Task 1: `issues tags add` / `issues tags remove`, and tags on `issues get`

Implements **F1–F7**. Independent of every other task.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — module docstring, `TAG_FIELDS` beside the other projections, `ISSUE_FIELDS`, `_flatten_tags` beside `_flatten_links`, the tag verbs after the links block, `ARG_*`, `COMMANDS`, `WRITE_VERBS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_request`, `_die`, `_issue_ref`, `issues_get`, `difflib`.
- Produces: `TAG_FIELDS: str`; `_flatten_tags(issue)`; `_resolve_tag_id(c, name) -> str`; `_missing_tag_error(ref, tag, tag_id)`; `issues_tags_add(c, a)`; `issues_tags_remove(c, a)`; `ARG_TAG_POS`.

- [ ] **Step 1: Write the failing tests**

```python
_TAGS = [{"id": "6-3", "name": "Question", "owner": {"login": "s.royz"}},
         {"id": "6-11", "name": "DevOps", "owner": {"login": "s.royz"}},
         {"id": "6-31", "name": "Data Team", "owner": {"login": "k.shmidt"}}]


def _tag_handler(tags=None, *, delete_status=200, delete_body=None,
                 issue_tags=None):
    """Serves /issueTags for name->id resolution, the tag write, and issues get."""
    tags = _TAGS if tags is None else tags

    class TagHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def _reply(self, payload, status=200):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.__class__.requests.append(("GET", self.path, None))
            route = urllib.parse.urlparse(self.path).path
            if route == "/api/issueTags":
                self._reply(tags)
            else:                                   # the issues get read-back
                self._reply({"idReadable": "DEMO-1", "summary": "s",
                             "tags": issue_tags if issue_tags is not None
                                     else [{"id": "6-3", "name": "Question"}]})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, json.loads(raw)))
            self._reply({"id": "6-3", "name": "Question"})

        def do_DELETE(self):
            self.__class__.requests.append(("DELETE", self.path, None))
            self._reply(delete_body, delete_status)

    TagHandler.requests = []
    return TagHandler


def test_tags_add_resolves_the_name_to_an_id(tmp_path):
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Data Team")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert urllib.parse.urlparse(post[1]).path == "/api/issues/DEMO-1/tags"
    # Measured: the tag write rejects a name and takes only an id.
    assert post[2] == {"id": "6-31"}


def test_tags_add_matches_the_name_case_insensitively(tmp_path):
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "devops")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert post[2] == {"id": "6-11"}


def test_tags_add_never_sends_a_name_alongside_the_id(tmp_path):
    """Measured: with both, the id wins and the name is silently ignored."""
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Question")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert "name" not in post[2]


def test_tags_add_never_writes_the_issue_level_tags_array(tmp_path):
    """Measured: POST /issues/{id} with a tags array REPLACES the tag set and
    wiped two tags the caller never named. Only the additive sub-resource is
    acceptable, so no POST may target the issue itself."""
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Question")
    assert result.returncode == 0, result.stderr
    for method, path, body in handler.requests:
        if method == "POST":
            assert urllib.parse.urlparse(path).path.endswith("/tags")
            assert "tags" not in (body or {})


def test_tags_unknown_name_exits_6_with_near_miss_and_no_write(tmp_path):
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Questin")
    assert result.returncode == 6
    assert "Question" in result.stderr, "must offer the near-miss"
    # The server returns the same 400 for a typo as for a name-instead-of-id,
    # so the refusal has to happen here, before any write.
    assert not any(r[0] == "POST" for r in handler.requests)


def test_tags_unknown_name_does_not_claim_the_tag_does_not_exist(tmp_path):
    """A tag private to another user is invisible to this token and
    indistinguishable from a typo, so the message must not overclaim."""
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Secret")
    assert result.returncode == 6
    assert "visible" in result.stderr.lower()


def test_tags_ambiguous_name_is_refused_not_guessed(tmp_path):
    collide = [{"id": "6-3", "name": "Shared", "owner": {"login": "s.royz"}},
               {"id": "6-9", "name": "Shared", "owner": {"login": "k.shmidt"}}]
    handler = _tag_handler(collide)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "add", "DEMO-1",
                         "Shared")
    assert result.returncode == 6
    assert "ambiguous" in result.stderr.lower()
    assert not any(r[0] == "POST" for r in handler.requests)


def test_tags_remove_deletes_by_resolved_id(tmp_path):
    handler = _tag_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "remove", "DEMO-1",
                         "Data Team")
    assert result.returncode == 0, result.stderr
    delete = [r for r in handler.requests if r[0] == "DELETE"][0]
    assert urllib.parse.urlparse(delete[1]).path == "/api/issues/DEMO-1/tags/6-31"


def test_tags_remove_of_a_tag_the_issue_lacks_blames_the_tag(tmp_path):
    """Measured: that 404's message names the TAG, which exists — passing it
    through would say the tag is missing when the issue merely lacks it."""
    handler = _tag_handler(delete_status=404,
                           delete_body={"error": "Not Found",
                                        "error_description":
                                            "Entity with id 6-31 not found"})
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "remove", "DEMO-1",
                         "Data Team")
    assert result.returncode == 3
    assert "Data Team" in result.stderr
    assert "DEMO-1" in result.stderr


def test_tags_remove_of_an_unknown_issue_is_not_reported_as_a_missing_tag(tmp_path):
    """The same status, a different message: this 404 names the ISSUE."""
    handler = _tag_handler(delete_status=404,
                           delete_body={"error": "Not Found",
                                        "error_description":
                                            "Entity with id DEMO-1 not found"})
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "tags", "remove", "DEMO-1",
                         "Data Team")
    assert result.returncode == 3
    assert "has no" not in result.stderr, \
        "must not blame the tag when the issue is what is missing"


def test_issues_get_flattens_tags_to_names(tmp_path):
    handler = _tag_handler(issue_tags=[{"id": "6-3", "name": "Question"},
                                       {"id": "6-11", "name": "DevOps"}])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tags"] == ["Question", "DevOps"]


def test_issues_get_omits_the_tags_key_when_there_are_none(tmp_path):
    """Matches _flatten_links, which omits `links` rather than emitting []."""
    handler = _tag_handler(issue_tags=[])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    assert "tags" not in json.loads(result.stdout)


def test_issue_fields_projection_requests_tags(tmp_path):
    handler = _tag_handler()
    with serve(handler) as base:
        run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    path = [r[1] for r in handler.requests if r[0] == "GET"][0]
    assert "tags(" in urllib.parse.unquote(path)


def test_tag_write_verbs_are_gated(tmp_path):
    verbs = _write_verbs()
    assert "issues tags add" in verbs
    assert "issues tags remove" in verbs
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "tags" -v`
Expected: FAIL — argparse exits 2, `issues tags` is not a known verb.

- [ ] **Step 3: Write the implementation**

Add beside the other field projections:

```python
TAG_FIELDS = "id,name,owner(login)"
```

Extend `ISSUE_FIELDS` with `tags(id,name)` (leave `ISSUE_SEARCH_FIELDS` alone — **F16**).

Add beside `_flatten_links`:

```python
def _flatten_tags(issue):
    """Reduce tags to their names, dropping the key when there are none.

    Matches _flatten_links: an absent key rather than an empty list, so a
    consumer handles "no tags" the same way it already handles "no links".
    """
    if not isinstance(issue, dict) or "tags" not in issue:
        return issue
    names = [t.get("name") for t in (issue.pop("tags") or [])
             if isinstance(t, dict) and t.get("name")]
    if names:
        issue["tags"] = names
    return issue
```

Wire it into `issues_get` alongside `_flatten_links`.

Add the tag section after the links block:

```python
# ── issues: tags ─────────────────────────────────────────────────────────
#
# Measured 2026-07-28, see .claude/plans/2026-07-27-youtrack-mcp-parity.md.
#
# The write path takes an id and REJECTS a name — and an unknown name returns
# the byte-identical 400 — so the name a caller knows has to be resolved here
# or it cannot be accepted at all. Same shape as permittedGroups.
#
# Do NOT implement add/remove as POST /issues/{id} with a `tags` array. That
# endpoint works, but it REPLACES the whole tag set: measured, it wiped two
# tags the caller never named. Only the additive sub-resource below is safe.

def _resolve_tag_id(c, name: str) -> str:
    """A tag name -> its id.

    Fetches /issueTags once with a fixed `$top` and no `query`: the complete
    visible set is needed both to resolve a name and to notice a duplicate, and
    a filtered subset cannot show a collision.

    Tag names are not guaranteed unique — 17 distinct on ION, but nothing
    server-side enforces it — so a collision is refused rather than resolved to
    whichever row a comprehension kept last, mirroring _resolve_link and
    _resolve_group_ids.
    """
    rows = _request(c, "GET", "/issueTags",
                    params={"fields": TAG_FIELDS, "$top": 1000})
    by_name: dict = {}
    for row in (rows if isinstance(rows, list) else []):
        if isinstance(row, dict) and row.get("name"):
            by_name.setdefault(row["name"].casefold(), []).append(row)
    matches = by_name.get(name.strip().casefold(), [])
    if len(matches) > 1:
        _die(6, "ambiguous_tag_name",
             f"{name!r} is ambiguous on this instance: it names "
             f"{len(matches)} tags",
             "rename one of the colliding tags so the name is unique")
    if not matches or not matches[0].get("id"):
        known = sorted({r["name"] for lst in by_name.values() for r in lst})
        near = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
        hint = (f"did you mean: {', '.join(near)}?" if near
                else f"tags visible to this token: {', '.join(known)}")
        # Deliberately "not visible" rather than "does not exist": a tag
        # private to another user is invisible here and indistinguishable from
        # a typo, so non-existence is not something this CLI can assert.
        _die(6, "unknown_tag", f"no tag named {name!r} is visible to this token",
             hint)
    return matches[0]["id"]


def issues_tags_add(c, a):
    ref = _issue_ref(a.issue)
    tag_id = _resolve_tag_id(c, a.tag)
    # Idempotent server-side (measured), so no pre-check: a second identical
    # add returns 200 and creates no duplicate.
    _request(c, "POST", f"/issues/{ref}/tags",
             params={"fields": "id,name"}, json={"id": tag_id})
    return issues_get(c, argparse.Namespace(issue=ref))


def _missing_tag_error(ref: str, tag: str, tag_id: str):
    """Retranslate remove's 404 — but only the one that names the tag.

    Measured: removing a tag the issue does not carry returns 404 whose message
    names the *tag id*, which exists as a tag; an unknown *issue* returns 404
    naming the issue instead. They are separable only by message content, so
    this fires on the tag id alone and lets the issue case fall through to the
    generic not-found mapping.
    """
    def on_error(response):
        if response.status_code == 404 and tag_id in response.text:
            _die(3, "not_found", f"{ref} has no {tag!r} tag",
                 "read the issue to see the tags it does have", status=404)
    return on_error


def issues_tags_remove(c, a):
    ref = _issue_ref(a.issue)
    tag_id = _resolve_tag_id(c, a.tag)
    _request(c, "DELETE", f"/issues/{ref}/tags/{tag_id}",
             on_error=_missing_tag_error(ref, a.tag, tag_id))
    return issues_get(c, argparse.Namespace(issue=ref))
```

Add the arg spec, the `COMMANDS` rows and the `WRITE_VERBS` entries **together**:

```python
ARG_TAG_POS       = (("tag",), {"metavar": "TAG"})
```

```python
    ("issues", "tags", "add"):         (issues_tags_add, [ARG_ISSUE, ARG_TAG_POS]),
    ("issues", "tags", "remove"):      (issues_tags_remove, [ARG_ISSUE, ARG_TAG_POS]),
```

```python
WRITE_VERBS = {..., "issues tags add", "issues tags remove"}
```

Add to the docstring's `Write:` block:

```
  youtrack issues tags add ISSUE TAG         Tag an issue. TAG is the tag name;
  youtrack issues tags remove ISSUE TAG      it is resolved to an id here
                                             because YouTrack's tag endpoint
                                             rejects a name. Tags are
                                             instance-wide, not per project, and
                                             a tag owned by someone else can be
                                             applied. A name that is not visible
                                             to this token exits 6 before any
                                             write. add is idempotent; removing
                                             a tag the issue does not carry
                                             exits 3.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "tags" -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20` with `timeout: 400000`
Expected: 169 pre-existing plus the new ones, zero failures. `ISSUE_FIELDS` grew, so check any test asserting on that projection string and preserve its intent.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): issues tags add and issues tags remove

Closes manage_issue_tags. TAG is the tag name, resolved to an id here because
YouTrack's tag endpoint rejects a name and returns the byte-identical 400 for
an unknown one (measured) — so the server can never tell the caller which
mistake they made.

Deliberately NOT implemented as POST /issues/{id} with a tags array: that
endpoint replaces the whole tag set, and was measured wiping two tags the
caller never named.

An unresolvable name exits 6 with a near-miss and says the tag is not visible
rather than that it does not exist, because a tag private to another user is
invisible to this token and indistinguishable from a typo. An ambiguous name is
refused rather than guessed. Removing a tag the issue does not carry exits 3
blaming the tag, since that 404's message names the tag — which exists.

issues get now returns tags flattened to names, omitting the key when empty,
exactly as links do."
```

---

### Task 2: `issues work log` and `issues work list`

Implements **F8–F13**. Independent of Task 1.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — docstring, `WORK_ITEM_FIELDS`, `_utc_date` extracted beside `_flatten_field`, the work section after the tags block, `ARG_*`, `COMMANDS`, `WRITE_VERBS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_request`, `_die`, `_issue_ref`, `_read_text`, `_epoch_ms_noon_utc`, `_page_params`, `_paged`, `difflib`.
- Produces: `WORK_ITEM_FIELDS: str`; `_utc_date(ms) -> str`; `_shape_work_item(item) -> dict`; `_duration_value(raw) -> dict`; `_resolve_work_item_type(c, ref, name) -> str`; `_work_item_error()`; `issues_work_log(c, a)`; `issues_work_list(c, a)`; `ARG_DURATION`, `ARG_DATE`, `ARG_WORK_TYPE`.

- [ ] **Step 1: Write the failing tests**

```python
_WORK_ITEM = {"id": "162-1", "date": 1784505600000,
              "duration": {"minutes": 90, "presentation": "1h 30m",
                           "$type": "DurationValue"},
              "text": None, "type": None,
              "author": {"login": "s.royz"}, "created": 1785273759478}

_PROJECT_WORK_TYPES = [{"id": "139-0", "name": "Development"},
                       {"id": "139-1", "name": "Testing"}]


def _work_handler(items=None, types=None, *, post_status=200, post_body=None):
    """Serves the issue read (for its project), the project's work-item types,
    the work-item POST, and the work-item list."""
    items = [_WORK_ITEM] if items is None else items
    types = _PROJECT_WORK_TYPES if types is None else types

    class WorkHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def _reply(self, payload, status=200):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.__class__.requests.append(("GET", self.path, None))
            route = urllib.parse.urlparse(self.path).path
            if route.endswith("/timeTrackingSettings/workItemTypes"):
                self._reply(types)
            elif route.endswith("/timeTracking/workItems"):
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                top = int(query.get("$top", ["100"])[0])
                self._reply(items[:top])
            else:                                   # the issue, for its project
                self._reply({"id": "2-1", "idReadable": "DEMO-1",
                             "project": {"id": "0-1", "shortName": "DEMO"}})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, json.loads(raw)))
            self._reply(post_body if post_body is not None else _WORK_ITEM,
                        post_status)

    WorkHandler.requests = []
    return WorkHandler


def test_work_log_sends_duration_as_presentation(tmp_path):
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "1h 30m")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert urllib.parse.urlparse(post[1]).path == \
        "/api/issues/DEMO-1/timeTracking/workItems"
    # Measured: minutes and presentation together return "Conflict in period
    # value", and the server owns the grammar (and the workday length).
    assert post[2]["duration"] == {"presentation": "1h 30m"}
    assert "minutes" not in post[2]["duration"]


def test_work_log_without_a_type_makes_exactly_one_request(tmp_path):
    """No --type means no project lookup, so no issue read either."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "45m")
    assert result.returncode == 0, result.stderr
    assert len(handler.requests) == 1
    assert handler.requests[0][0] == "POST"


def test_work_log_refuses_a_unit_less_duration_before_any_request(tmp_path):
    """Measured: YouTrack reads a bare number as HOURS, so --duration 90
    intending minutes logs 90 hours. Refused rather than passed through."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "90")
    assert result.returncode == 6
    assert "90m" in result.stderr or "unit" in result.stderr.lower()
    assert not handler.requests, "must not reach the server"


def test_work_log_accepts_unit_forms_the_server_parses(tmp_path):
    for raw in ["90m", "1h30m", "1h 30m", "2d", "1w", "1H30M"]:
        handler = _work_handler()
        with serve(handler) as base:
            result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                             "--duration", raw)
        assert result.returncode == 0, f"{raw}: {result.stderr}"


def test_work_log_resolves_the_type_against_the_projects_set(tmp_path):
    """Measured: a type valid instance-wide can be rejected by the project
    (Review), so resolution must read the project's list, which costs one read
    of the issue to learn its project."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--type", "testing")
    assert result.returncode == 0, result.stderr
    gets = [urllib.parse.urlparse(r[1]).path for r in handler.requests
            if r[0] == "GET"]
    assert "/api/admin/projects/0-1/timeTrackingSettings/workItemTypes" in gets
    post = [r for r in handler.requests if r[0] == "POST"][0]
    # Measured: the type write takes an id and rejects a name.
    assert post[2]["type"] == {"id": "139-1"}


def test_work_log_unknown_type_exits_6_with_near_miss_and_no_write(tmp_path):
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--type", "Developmnt")
    assert result.returncode == 6
    assert "Development" in result.stderr
    assert not any(r[0] == "POST" for r in handler.requests)


def test_work_log_type_absent_from_the_project_is_refused_here(tmp_path):
    """Review exists instance-wide but not on this project — the near-miss must
    be over the PROJECT's set, so an instance-only name is simply unknown."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--type", "Review")
    assert result.returncode == 6
    assert not any(r[0] == "POST" for r in handler.requests)


def test_work_log_sends_the_date_as_noon_utc_epoch_ms(tmp_path):
    """The server snaps to 00:00 of the day and whether that snap is UTC or
    profile-timezone is unmeasurable on ION, so noon is sent for its ~12h of
    margin. Assert the request, never epoch equality on the round trip."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--date", "2026-07-20")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert post[2]["date"] == 1784548800000        # 2026-07-20T12:00:00Z


def test_work_log_rejects_a_malformed_date(tmp_path):
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--date", "20/07/2026")
    assert result.returncode == 6
    assert not handler.requests


def test_work_log_renders_the_read_back_date_as_a_calendar_day(tmp_path):
    """The server stores 00:00 of the day; epoch ms is a hostile interface for
    a calendar date, exactly as for `date` custom fields."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "1h 30m")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["date"] == "2026-07-20"


def test_work_log_emits_both_duration_representations_without_type(tmp_path):
    """presentation is re-rendered by the server and minutes reflects the
    project's workday length, so each carries what the other loses. $type never
    reaches output."""
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "1h 30m")
    assert result.returncode == 0, result.stderr
    duration = json.loads(result.stdout)["duration"]
    assert duration == {"minutes": 90, "presentation": "1h 30m"}
    assert "$type" not in duration


def test_work_log_passes_text_through(tmp_path):
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "5m", "--text", "line one\nline two")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert post[2]["text"] == "line one\nline two"


def test_work_log_translates_an_unparseable_duration(tmp_path):
    handler = _work_handler(
        post_status=400,
        post_body={"error": "bad_request",
                   "error_description": "Value 1.5h cannot be parsed"})
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "1.5h")
    assert result.returncode == 6
    assert "1.5h" in result.stderr


def test_work_log_translates_a_non_positive_duration(tmp_path):
    handler = _work_handler(
        post_status=400,
        post_body={"error": "invalid_properties",
                   "error_description": "Work duration can not be negative or empty"})
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "0m")
    assert result.returncode == 6


def test_work_list_pages_and_envelopes(tmp_path):
    items = [dict(_WORK_ITEM, id=f"162-{n}") for n in range(5)]
    handler = _work_handler(items)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "list", "DEMO-1",
                         "--limit", "2", "--offset", "1")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(handler.requests[0][1]).query)
    assert query["$top"] == ["3"]           # limit + 1
    assert query["$skip"] == ["1"]
    payload = json.loads(result.stdout)
    assert len(payload["items"]) == 2
    assert payload["has_more"] is True
    assert payload["items"][0]["date"] == "2026-07-20"
    assert "$type" not in payload["items"][0]["duration"]


def test_work_list_of_an_issue_with_no_items(tmp_path):
    handler = _work_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "list", "DEMO-1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"items": [], "has_more": False}


def test_work_log_is_gated_and_list_is_not(tmp_path):
    verbs = _write_verbs()
    assert "issues work log" in verbs
    assert "issues work list" not in verbs
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "work" -v`
Expected: FAIL — argparse exits 2, `issues work` is not a known verb.

- [ ] **Step 3: Extract the date renderer, then write the implementation**

`_flatten_field` already renders a `date` custom field as `YYYY-MM-DD` inline. Extract it so the work item uses one convention, with no behaviour change:

```python
def _utc_date(ms: int) -> str:
    """Epoch ms -> YYYY-MM-DD in UTC.

    Both a `date` custom field (snapped by the server to noon UTC) and a work
    item's date (snapped to 00:00) are calendar days, and epoch ms is a hostile
    interface for one.
    """
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
```

Then `_flatten_field`'s `DateIssueCustomField` branch becomes `return _utc_date(value)`.

Add the projection beside the others:

```python
WORK_ITEM_FIELDS = ("id,date,duration(minutes,presentation),text,"
                    "type(id,name),author(login),created")
```

Add the work section after the tags block:

```python
# ── issues: work items ───────────────────────────────────────────────────
#
# Measured 2026-07-28, see .claude/plans/2026-07-27-youtrack-mcp-parity.md.
#
# Three traps, all measured:
#   1. A unit-less duration is read as HOURS — "90" logs 90h, not 90m.
#   2. `type` takes an id and rejects a name, and work-item types are
#      PROJECT-scoped: `Review` exists instance-wide and project 0-1 refuses
#      it. Resolution must read the project's set, never the instance's.
#   3. The date is snapped to 00:00 of the day — the opposite corner from a
#      `date` custom field's noon — and an ISO string is rejected outright.
#
# `Spent time` is derived from these entries and is not writable directly
# ("automatically calculated based on time tracking settings"), so there is
# deliberately no verb that sets it.

_UNIT_LESS_DURATION_RE = re.compile(r"^\d+$")


def _duration_value(raw: str) -> dict:
    """The `duration` object, as a presentation string the server parses.

    Only one of minutes/presentation may be sent — both returns "Conflict in
    period value" — and presentation is chosen because YouTrack owns the period
    grammar and the workday length (1d = 8h on ION, from project settings).

    A unit-less number is refused. This is deliberately stricter than the
    server, which accepts it and reads it as HOURS: `--duration 90` meaning 90
    minutes would silently log 90 hours, and the client can detect that with
    certainty.
    """
    value = raw.strip()
    if not value:
        _die(6, "input", "--duration cannot be empty")
    if _UNIT_LESS_DURATION_RE.match(value):
        _die(6, "input",
             f"--duration {value!r} has no unit, and YouTrack reads a bare "
             f"number as HOURS — {value} would log {value} hours",
             f"write {value}m for minutes, or 1h 30m / 2h / 2d (1d is the "
             "project's workday, not 24h)")
    return {"presentation": value}


def _resolve_work_item_type(c, ref: str, name: str) -> str:
    """A work-item type name -> its id, scoped to the issue's project.

    Measured: the type write rejects a name, and a type that exists
    instance-wide can still be refused by the project ("The selected work type
    is not supported for issues in this YouTrack project"). So the project's own
    list is the authority, and reaching it costs one read of the issue.
    """
    issue = _request(c, "GET", f"/issues/{ref}", params={"fields": "project(id)"})
    project = ((issue or {}).get("project") or {}).get("id")
    if not project:
        _die(5, "bad_response", "YouTrack returned an issue without a project")
    rows = _request(c, "GET",
                    f"/admin/projects/{project}/timeTrackingSettings/workItemTypes",
                    params={"fields": "id,name", "$top": 200})
    wanted = name.strip().casefold()
    known = []
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        known.append(row["name"])
        if row["name"].casefold() == wanted and row.get("id"):
            return row["id"]
    near = difflib.get_close_matches(name, sorted(known), n=3, cutoff=0.6)
    hint = (f"did you mean: {', '.join(near)}?" if near
            else f"work item types on this project: {', '.join(sorted(known))}")
    _die(6, "unknown_work_item_type",
         f"no work item type named {name!r} on this issue's project", hint)


def _work_item_error():
    """Translate the two duration rejections; let everything else fall through."""
    def on_error(response):
        try:
            body = response.json()
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        detail = str(body.get("error_description") or body.get("error") or "")
        if "cannot be parsed" in detail:
            _die(6, "invalid_duration", detail,
                 "durations are whole numbers with units: 45m, 1h 30m, 2d. "
                 "Fractions, negatives and seconds are rejected",
                 status=response.status_code)
        if "negative or empty" in detail:
            _die(6, "invalid_duration", detail,
                 "log a positive duration", status=response.status_code)
    return on_error


def _shape_work_item(item):
    """One work item, with the date as a calendar day and no $type anywhere.

    `duration` keeps BOTH representations: the server re-renders presentation
    (1d 4h reads back as 12h, and output never uses day units), while minutes
    reflects the project's workday length — so each carries what the other
    loses.
    """
    if not isinstance(item, dict):
        return item
    duration = item.get("duration") or {}
    shaped = {"id": item.get("id"),
              "duration": {"minutes": duration.get("minutes"),
                           "presentation": duration.get("presentation")},
              "text": item.get("text")}
    date = item.get("date")
    shaped["date"] = _utc_date(date) if isinstance(date, int) else None
    work_type = item.get("type") or {}
    shaped["type"] = work_type.get("name")
    shaped["author"] = (item.get("author") or {}).get("login")
    if isinstance(item.get("created"), int):
        shaped["created"] = item["created"]
    return shaped


def issues_work_log(c, a):
    ref = _issue_ref(a.issue)
    body = {"duration": _duration_value(a.duration)}
    if a.date is not None:
        # Noon, not midnight: the server snaps to 00:00 of the day, and whether
        # that snap uses UTC or the profile timezone is unmeasured — noon keeps
        # ~12h of margin either way, so the calendar date survives.
        body["date"] = _epoch_ms_noon_utc(a.date, "--date")
    if a.text is not None:
        body["text"] = _read_text(a.text)
    if a.type is not None:
        body["type"] = {"id": _resolve_work_item_type(c, ref, a.type)}
    return _shape_work_item(
        _request(c, "POST", f"/issues/{ref}/timeTracking/workItems",
                 params={"fields": WORK_ITEM_FIELDS}, json=body,
                 on_error=_work_item_error()))


def issues_work_list(c, a):
    rows = _request(c, "GET", f"/issues/{_issue_ref(a.issue)}/timeTracking/workItems",
                    params=_page_params(a, WORK_ITEM_FIELDS))
    rows = rows if isinstance(rows, list) else []
    return _paged([_shape_work_item(r) for r in rows], a.limit)
```

Add the arg specs, `COMMANDS` rows and `WRITE_VERBS` entry together:

```python
ARG_DURATION      = (("--duration",), {"required": True, "metavar": "D"})
ARG_DATE          = (("--date",), {"metavar": "YYYY-MM-DD"})
ARG_WORK_TYPE     = (("--type",), {"metavar": "NAME"})
```

```python
    ("issues", "work", "log"):         (issues_work_log, [ARG_ISSUE, ARG_DURATION,
                                                          ARG_DATE, ARG_WORK_TYPE,
                                                          ARG_TEXT]),
    ("issues", "work", "list"):        (issues_work_list, [ARG_ISSUE, ARG_LIMIT50,
                                                           ARG_OFFSET]),
```

> `ARG_WORK_TYPE` is separate from `ARG_LINK_TYPE`, which is `required` — a work item's type is optional (measured: omitting it yields `type: null`).

Docstring — add to `Read:`:

```
  youtrack issues work list ISSUE [--limit N] [--offset N]
                                             Work items logged on an issue:
                                             date, duration, type, author, text.
```

and to `Write:`:

```
  youtrack issues work log ISSUE             Log time against an issue. Adds an
        --duration D [--date YYYY-MM-DD]      entry; it never rewrites a field.
        [--type NAME] [--text TEXT|-]         --duration needs a unit — 45m,
                                             1h 30m, 2d — because YouTrack reads
                                             a bare number as HOURS, so a
                                             unit-less value is refused here. 1d
                                             is the project's workday, not 24h,
                                             and the server re-renders the
                                             duration it stores, so the reply
                                             carries both minutes and its own
                                             presentation. --type is optional
                                             and is validated against this
                                             project's work item types, which
                                             can differ from the instance's.
                                             --date is a calendar day and
                                             defaults to today. The Spent time
                                             field is calculated from these
                                             entries and cannot be set directly.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "work" -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20` with `timeout: 400000`
Expected: zero failures. The `_utc_date` extraction must not change `date`-field behaviour — if a `date` custom-field test fails, the extraction is wrong; fix the helper, do not weaken the test.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): issues work log and issues work list

Closes log_work, the last MCP parity gap. Adds a work-item entry and lists the
entries; it never rewrites Spent time, which is calculated from these entries
and rejects a direct write outright (measured).

Three measured traps drive the design:

- A unit-less duration is read as HOURS, so --duration 90 meaning 90 minutes
  would log 90 hours. Refused at exit 6 before any request — deliberately
  stricter than the server, because the client can detect it with certainty and
  the failure is otherwise silent.
- --type takes an id, rejects a name, and work-item types are PROJECT-scoped:
  Review exists instance-wide and project 0-1 refuses it. The name is resolved
  against the project's own set, which costs one read of the issue, and only
  when --type is given — so a plain log is still a single request.
- The date is snapped to 00:00 of the day, the opposite corner from a date
  custom field's noon, and an ISO string is rejected. --date takes YYYY-MM-DD
  and is sent as noon UTC: whether the snap uses UTC or the profile timezone is
  unmeasurable on ION, and noon keeps ~12h of margin either way.

duration is emitted with both minutes and presentation, since the server
re-renders presentation (1d 4h reads back as 12h, never in day units) while
minutes reflects the project's workday length."
```

---

### Task 3: `projects fields list` paging

Implements **F14**. The last silently-truncating verb in the surface.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `_project_fields`, `projects_fields_list`, `COMMANDS`, docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params`, `_paged`.
- Produces: `_project_fields(c, project, params=None)`; `projects fields list` returning the envelope.

- [ ] **Step 1: Write the failing test**

```python
def test_projects_fields_list_pages_and_envelopes(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "list", "0-6",
                         "--limit", "2", "--offset", "1")
    assert result.returncode == 0, result.stderr
    path = [r[1] for r in ProjectFieldsHandler.requests if r[0] == "GET"][0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert query["$top"] == ["3"]        # limit + 1, replacing the hardcoded 200
    assert query["$skip"] == ["1"]
    payload = json.loads(result.stdout)
    assert "items" in payload and isinstance(payload["items"], list)
    assert payload["has_more"] is False


def test_project_field_validation_still_reads_the_complete_set(tmp_path):
    """The paging belongs to the verb, not to _project_fields: `fields get` and
    both write verbs validate against the schema, and a paged helper would
    narrow validation into false 'unknown field' errors."""
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "get", "0-6",
                         "Points")
    assert result.returncode == 0, result.stderr
    path = [r[1] for r in ProjectFieldsHandler.requests if r[0] == "GET"][0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert query["$top"] == ["200"]
    assert "$skip" not in query
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "projects_fields_list_pages or complete_set" -v`
Expected: FAIL — `--limit`/`--offset` are unrecognised, argparse exits 2.

- [ ] **Step 3: Write the implementation**

```python
def _project_fields(c, project, params=None):
    # $top=200 is deliberate headroom for the internal callers — `projects
    # fields get` and both write verbs' validation need the COMPLETE set, since
    # a truncated schema turns a legal field name into a false "unknown field".
    # Only the `projects fields list` verb passes paging params.
    raw = _request(c, "GET", f"/admin/projects/{project}/customFields",
                   params=params or {"fields": PROJECT_FIELD_FIELDS, "$top": 200})
    return [_shape_project_field(e) for e in raw if isinstance(e, dict)]


def projects_fields_list(c, a):
    return _paged(_project_fields(c, a.project,
                                  _page_params(a, PROJECT_FIELD_FIELDS)), a.limit)
```

```python
    ("projects", "fields", "list"):   (projects_fields_list, [ARG_PROJECT_POS,
                                                              ARG_LIMIT100,
                                                              ARG_OFFSET]),
```

Docstring: update the `projects fields list` line to `ID [--limit N] [--offset N]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "projects_fields" -v`
Expected: **four pre-existing tests fail** — `test_projects_fields_list_shapes_schema`, `test_projects_fields_report_can_be_empty_not_required`, `test_projects_fields_list_drops_nameless_bundle_entries` and `test_projects_fields_list_skips_non_dict_entries` all read `projects fields list` output as a bare array. Update them to read `payload["items"]`, **preserving their original intent** — do not weaken an assertion to make it pass.

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20` with `timeout: 400000`
Expected: zero failures. `projects fields get` and the create/update validation paths must be untouched.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack)!: page projects fields list

The last verb in the surface that truncated silently — it hardcoded \$top: 200
with no way to page or to know the result was cut short. M3 and M4a fixed every
other list verb.

The paging is on the verb, not on _project_fields: that helper is also the
lookup behind projects fields get and behind both write verbs' value
validation, and those need the complete set — a paged schema would turn a legal
field name into a false 'unknown field' error.

BREAKING CHANGE: projects fields list now returns {\"items\": [...],
\"has_more\": bool} instead of a bare JSON array, matching every other --limit
verb."
```

---

### Task 4: self-parent guard on `articles update --parent`

Implements **F15**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `articles_update`, docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_article_ref`, `_parent_article_id`, `_die`.
- Produces: the guard inside `articles_update`.

- [ ] **Step 1: Write the failing tests**

```python
def test_articles_update_refuses_self_parent_by_the_same_ref(tmp_path):
    handler = _reparent_handler()          # reuse the Task-3 M4a handler shape
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "A-1")
    assert result.returncode == 6
    assert not any(r[0] == "POST" for r in handler.requests)


def test_articles_update_refuses_self_parent_across_notations(tmp_path):
    """The measured reason this compares resolved internal ids: --parent 5-1
    against A-1 is one article in two notations, which comparing the raw
    arguments would miss."""
    handler = _reparent_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "5-1")
    assert result.returncode == 6
    assert not any(r[0] == "POST" for r in handler.requests)


def test_articles_update_still_reparents_to_a_different_article(tmp_path):
    """The guard must not break the legitimate case."""
    handler = _reparent_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "A-2")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert post[2] == {"parentArticle": {"id": "5-2"}}
```

> `_reparent_handler` must serve `A-1`/`5-1` and `A-2`/`5-2` in the same project. Factor it out of the existing `test_articles_update_reparents` handler rather than writing a third copy, and keep that test passing unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "self_parent" -v`
Expected: FAIL — the self-parent reaches the POST today.

- [ ] **Step 3: Write the implementation**

In `articles_update`'s parent branch, add the guard **after** both ids are resolved and **before** the write:

```python
    if a.parent:
        # The same-project rule is enforced against *this* article's project, so
        # re-parenting needs one read of the target before the write.
        target = _request(c, "GET", f"/articles/{_article_ref(a.article)}",
                          params={"fields": "id,project(id)"})
        project_id = ((target.get("project") or {}).get("id")
                      if isinstance(target, dict) else None)
        if not project_id:
            _die(5, "bad_response", "YouTrack returned an article without a project")
        parent_id = _parent_article_id(c, a.parent, project_id)
        # Compare resolved internal ids, not the arguments: `--parent 5-1`
        # against `A-1` is one article in two notations. YouTrack does refuse a
        # self-parent (400, "recursive chain … sub-article of itself"), so this
        # is a clearer message and one saved round trip rather than a
        # correctness fix — unlike the self-link guard, where the server
        # returned 200 and silently created nothing.
        if isinstance(target, dict) and parent_id == target.get("id"):
            _die(6, "input", f"{a.article} cannot be its own parent",
                 "an article cannot be a sub-article of itself; YouTrack "
                 "refuses this too")
        body["parentArticle"] = {"id": parent_id}
```

Docstring: note on the `articles update` entry that a self-parent is refused.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "articles" -v`
Expected: PASS, including every pre-existing article test.

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20` with `timeout: 400000`
Expected: zero failures.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): refuse an article as its own parent

Measured correction to the plan's premise: YouTrack does NOT silently accept a
self-parent — it returns 400 'recursive chain … sub-article of itself' and
applies nothing, which already maps to exit 6. So this guard is a canonical
message and one saved round trip, not the correctness fix the self-link guard
is (there the server returned 200 and created nothing).

The comparison is on resolved internal ids, since --parent 5-1 against A-1 is
one article in two notations and comparing the arguments would miss it. Both
ids are already in hand from reads the verb performs anyway.

Not guarded, and recorded rather than hidden: a deeper cycle (A under B, B
under A) returns HTTP 500 with an empty body. Detecting it needs an ancestor
walk and is out of scope."
```

---

### Task 5: Live verification against ION

No new code. Every step runs from `/Users/zjor/projects/ion/agents` — **not** the capabilities repo — or the connection silently falls back to a different YouTrack instance.

- [ ] **Step 1: Install the branch build and audit**

```bash
cd /Users/zjor/src/ai-cluster-one-capabilities
capabilities audit youtrack --from .
capabilities install youtrack --from capabilities/youtrack/bin/youtrack
```

- [ ] **Step 2: Confirm the connection target before any write**

```bash
cd /Users/zjor/projects/ion/agents
youtrack connections | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['default'], [k['value'] for k in d['connections'][d['default']]['keys'] if k['key']=='base_url'])"
```
Expected: `ionwater ['https://ion.youtrack.cloud']`. **Anything else, stop.**

- [ ] **Step 3: Create one throwaway ION issue**

```bash
cd /Users/zjor/projects/ion/agents
youtrack issues create --project 0-1 --summary "PROBE M4b verify (safe to delete)" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['idReadable'], d['project']['shortName'])"
```
**If `project.shortName` is not `ION`, stop and delete it.** Use that key as `$K` below.

- [ ] **Step 4: Exercise the tag verbs**

```bash
youtrack issues tags add $K "Question"
youtrack issues tags add $K "Question"                  # idempotent
youtrack issues tags add $K "Data Team"                 # owned by another user
youtrack issues tags add $K "devops"                    # case-insensitive
youtrack issues get $K | python3 -c "import json,sys; print(json.load(sys.stdin).get('tags'))"
youtrack issues tags remove $K "Question"
youtrack issues tags remove $K "Question"; echo "missing tag exit=$?"
youtrack issues tags add $K "Questin"; echo "typo exit=$?"
```
Expected: adds succeed and the second is a no-op; `issues get` lists the tag **names**; removing a tag the issue no longer carries exits **3** blaming the tag; the typo exits **6** with a near-miss and writes nothing. **Critically: confirm the earlier tags survive each add** — the measured replace-semantics trap would show up here as tags disappearing.

- [ ] **Step 5: Exercise the work verbs**

```bash
youtrack issues work log $K --duration "1h 30m"
youtrack issues work log $K --duration "45m" --type Development --date 2026-07-20 --text "probe entry"
youtrack issues work log $K --duration "1d 4h"          # re-rendered, not byte-exact
youtrack issues work list $K --limit 2
youtrack issues work list $K --limit 2 --offset 2
youtrack issues work log $K --duration 90; echo "unit-less exit=$?"
youtrack issues work log $K --duration "1.5h"; echo "unparseable exit=$?"
youtrack issues work log $K --duration "30m" --type Review; echo "instance-only type exit=$?"
youtrack issues work log $K --duration "30m" --type Developmnt; echo "typo exit=$?"
```
Expected: the `1d 4h` entry reads back as `12h`/720 (**record it — this is the falsified round-trip**); `--date 2026-07-20` reads back as `2026-07-20`; the list pages with `has_more`; `--duration 90` exits **6** with the hours warning and sends nothing; `1.5h` exits **6**; `Review` and `Developmnt` both exit **6** with a near-miss over ION's five project types.

- [ ] **Step 6: Confirm `Spent time` moved, and is still not directly writable**

```bash
youtrack issues get $K | python3 -c "import json,sys; print(json.load(sys.stdin)['fields'].get('Spent time'))"
youtrack issues update $K --field "Spent time=1h"; echo "direct write exit=$?"
```
Expected: `Spent time` reflects the sum of the logged entries; the direct write is refused by the server.

- [ ] **Step 7: Exercise `projects fields list` paging**

```bash
youtrack projects fields list 0-1 --limit 3
youtrack projects fields list 0-1 --limit 3 --offset 3
youtrack projects fields get 0-1 Estimation
```
Expected: two different pages inside an envelope with `has_more`; `fields get` still resolves a field defined past the first page.

- [ ] **Step 8: Exercise the self-parent guard on a throwaway article**

Create one article in project `0-1`, attempt `articles update <id> --parent <same id>` in both notations (readable key and internal id), expect exit **6** with no write, then delete the article and verify 404.

- [ ] **Step 9: Delete the probe issue and verify it is gone**

Use the API directly (the CLI has no delete verb), then confirm `GET` returns 404 for both the internal id and the readable key. Sweep `summary: PROBE` and `project: ION created: Today`, and confirm the instance tag count is still **17**.

- [ ] **Step 10: Record the results in the parity plan**

Add an "✅ M4b verified live against ION" table under the M4b milestone, listing what was exercised and anything that could not be.

- [ ] **Step 11: Commit the verification note**

```bash
git add .claude/plans/2026-07-27-youtrack-mcp-parity.md
git commit -m "docs(youtrack): record M4b live verification against ION"
```

---

### Task 6: Catalog reindex, `SUMMARY`, status markers, consumer refresh

The step M1 skipped, which left `main` uninstallable.

- [ ] **Step 1: Read the hashing algorithm from source, do not guess it**

Read `_payload_sha256` in `bin/capabilities`. It skips `meta.json`, `stub`, `manifest.json`, anything under `__pycache__`, and `.pyc`/`.pyo`/`.session*`, feeding `<relpath>\0<bytes>\0` per file in sorted order into one sha256.

- [ ] **Step 2: Recompute every entry and confirm the untouched ones reproduce**

Transcribe that function and run it over all catalog entries. **Every capability except `youtrack` must reproduce its committed hash byte-for-byte.** If an untouched one differs, the transcription has drifted — reread the source rather than adjusting the expected value.

- [ ] **Step 3: Reinstall from source before reading the summary**

```bash
capabilities install youtrack --from capabilities/youtrack/bin/youtrack
```
`youtrack manifest --json` reports the **installed** build, so reading the summary before reinstalling yields the previous one. (This bit M3's Task 8 and M4a's Task 7.)

- [ ] **Step 4: Update `SUMMARY` — it is stale after every milestone**

`SUMMARY` is what `capabilities list` shows and what ContextKit injects into the consuming agent's context, so a consumer cannot discover a verb missing from it. M4b adds issue **tags** and **work-item logging**; the current text mentions neither. Update it, **then** recompute the hash, since `SUMMARY` is part of the payload.

- [ ] **Step 5: Update youtrack's `payload_sha256` and `summary` with the manager's conventions**

```python
json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
```
Expected diff: one or two changed lines, no reformatting noise.

- [ ] **Step 6: Update the parity plan's status markers and recount**

Mark M4b ✅ in the status block and the milestone heading. In the parity table, `manage_issue_tags` and `log_work` both move to parity. Then **recount the table's statuses and make the count line say what the count says** — **22 at parity, 0 near/partial, 0 absent, plus 1 covered by design**, summing to 23 over the 23 MCP rows. Do not edit the numbers by hand. Classify each row on its leading status glyph: a substring match on "absent" reads `search_issues` as absent, because its status cell says "sort still absent, deliberately".

- [ ] **Step 7: Audit and reinstall**

```bash
capabilities audit youtrack --from .
```
Expected: `ok: true`, no failures.

- [ ] **Step 8: Refresh the consumer context**

```bash
cd /Users/zjor/projects/ion/agents
contextkit build --target claude
```
`contextkit build` alone writes only the codex target. Both files are gitignored.

- [ ] **Step 9: Commit**

```bash
git add .capability-source/catalog.json .claude/plans/2026-07-27-youtrack-mcp-parity.md capabilities/youtrack/bin/youtrack
git commit -m "chore(source): reindex youtrack catalog and mark M4b shipped"
```

---

## Verification

```bash
cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q
```

Expected: 169 pre-existing plus ~35 new, all passing. `test_every_command_is_documented` and `test_every_documented_command_exists` cover the four new verbs; `test_write_verbs_match_commands` covers the three new write verbs.

Then the parity table's statuses must recount to 22 at parity, 0 absent and 1 covered by design, and the count line in the parity plan must say exactly that.

## Rollback

Every task is one commit on `feat/youtrack-m4b-tags-and-work`. To undo a task, `git revert <sha>`. To abandon M4b, `git checkout main && git branch -D feat/youtrack-m4b-tags-and-work`; no installed artifact changes until Task 5 Step 1, and reinstalling from `upstream/main` restores the M4a build.

## Out of scope

- **Cycle detection deeper than self-parent** on `articles update --parent`. A 2-cycle returns HTTP 500 with an empty body (measured); guarding it needs an ancestor walk.
- **A work-item update or delete verb.** `log_work` is additive; the DELETE endpoint exists (measured, used for probe cleanup) but no MCP tool covers it.
- **Tag creation.** Writing an unknown tag name does not create one (measured), and creating instance-wide tags from a CLI is a different, higher-consequence operation.
- **A `Spent time` writer.** Impossible: the field is server-calculated and rejects a direct write.
- **`issues tags list`** — `issues get` returns tags, mirroring the deliberate absence of `issues links list`.
- The **44-tool community server** as a parity target, and the two evidence gaps blocked by the ION-only constraint: exit 7 for a link workflow rejection, and M2's `text`-on-create criterion.

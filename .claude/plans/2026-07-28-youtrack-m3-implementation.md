# youtrack M3 — Work the Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `youtrack` capability the three verbs and two flags an agent needs to work a sprint board — look up a user, link issues to their parents, and page a result set without silent truncation.

**Architecture:** All changes land in the single file `capabilities/youtrack/bin/youtrack`, per the plan's standing one-file decision. Link direction phrases (`subtask of`, `parent for`) are resolved client-side against `GET /issues/{id}/links`, which returns both the phrase and the direction-encoded link id in one call. Paging gains a shared `_page_params` / `_paged` pair so every `--limit` verb reports truncation the same way.

**Tech Stack:** Python 3 single-file CLI with PEP-723 inline deps (`httpx`), argparse driven by the declarative `COMMANDS` table, pytest against a real local `ThreadingHTTPServer` (no mocking library).

## Global Constraints

- **Design source of truth:** `.claude/plans/2026-07-27-youtrack-mcp-parity.md` — the "Link model — measured 2026-07-28" section supplies every fixture below. Read it before Task 6.
- **One file.** Do not split `bin/youtrack` into modules. The plan's revisit trigger is ~1,500 lines of domain code; M3 adds ~120.
- **Branch:** `feat/youtrack-m3-work-the-board`, already created from `upstream/main`. Never branch from `origin/main` — the fork runs behind.
- **All live experiments run against the ION project only. IONDEV must not be touched** (owner decision, 2026-07-28). Anything ION cannot prove is recorded as unproven, not worked around.
- **Run every live CLI check from a directory where the project-tier connection resolves** (`/Users/zjor/projects/ion/agents`) **or pass `--connection ionwater` explicitly.** A `cd` elsewhere silently falls back to a different YouTrack instance that also has `allow_write: true`; this already caused two issues to be created on the wrong server during step 0.
- **Exit codes are fixed:** `0` success, `2` auth/config, `3` not found, `4` policy refusal, `5` network/server, `6` input, `7` workflow-rule rejection.
- **Output contract:** values flatten to scalars, arrays, or null — never YouTrack's `{"name": …, "$type": …}` wire shape.
- **`readOnly: true` on a link type is NOT a write gate.** Measured: `Subtask` and `Duplicate` both report it and both accept writes. Never branch on it.
- **Every new write verb must be added to `WRITE_VERBS`** or the `allow_write` gate silently does not apply to it.
- **Any payload change requires reindexing `.capability-source/catalog.json` in the same commit** (Task 8). Skipping this left `main` uninstallable after M1.

---

## Decisions locked before implementation

| # | Decision | Why |
|---|---|---|
| D1 | `--type` takes the **direction phrase** (`"subtask of"`), not a type name plus a direction flag | Measured: all 7 phrases are unique, so a phrase identifies type *and* direction. No second flag. |
| D2 | Link ids are **read from `GET /issues/{id}/links`**, never built by appending `s`/`t` to a type id | The suffix is an observed pattern, not a documented contract. The same call returns the phrase, so reading costs nothing extra. |
| D3 | An **ambiguous** phrase exits `6` rather than picking | Phrase uniqueness is measured on one instance; a custom link type could collide. |
| D4 | **Self-links are refused client-side** before any HTTP call | Measured: the server returns `200` and silently creates nothing. Sending it would report success for a no-op. |
| D5 | **Empty link slots are filtered** from `issues get` | The endpoint returns all 7 slots on every issue regardless of content. |
| D6 | **Every verb with a `--limit` flag** gains `--offset` and the `{"items": …, "has_more": …}` envelope | Uniform contract. **This is a deliberate widening beyond the plan's `issues search` + `issues comments list`** — it also changes `articles list` and `articles comments list`. Fixing truncation on one list verb while leaving three silently truncating reproduces the very bug. `projects find` is untouched: it has no `--limit` (M4). |
| D7 | `--select` rejects an **unknown key shape** at exit `6`; a `fields.X` **absent from a given issue is omitted**, not an error | Custom fields differ per project, so a cross-project search must not die on the first issue lacking a field — but a typo must not silently yield empty output. |
| D8 | `remove` resolves the target's internal id with one extra GET | Measured: DELETE requires the internal id; a readable key returns `404`. |

**D6 is the one to veto if any is going to be vetoed** — it is the only decision that breaks consumers beyond what was approved.

---

## File Structure

| File | Responsibility in M3 |
|---|---|
| `capabilities/youtrack/bin/youtrack` | All implementation. New helpers in the existing banner sections: paging + select next to the other shaping helpers; link resolution in a new `── issues: links ──` block before dispatch. |
| `capabilities/youtrack/bin/youtrack` (module docstring) | The help contract. SHEBANG makes it the single source of truth for the surface; the drift tests fail if it disagrees with `COMMANDS`. |
| `capabilities/youtrack/tests/test_youtrack.py` | All tests. Extends the existing local-HTTP-server pattern; no new dependency. |
| `.capability-source/catalog.json` | `payload_sha256` + `summary` reindex, Task 8, same commit as the last payload change. |
| `.claude/plans/2026-07-27-youtrack-mcp-parity.md` | Mark M3 shipped, Task 8. |

---

### Task 1: `users find`

Smallest verb, no dependencies. Establishes the paging helpers every later list task reuses.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — module docstring, helpers near `_positive_limit` (~line 826), `users_find` after `users_me` (~line 1200), `ARG_*` block (~line 1419), `COMMANDS` (~line 1441)
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_nonnegative_offset(value: int) -> None`; `_page_params(a, fields: str) -> dict`; `_paged(rows, limit: int) -> dict` returning `{"items": list, "has_more": bool}`; `users_find(c, a)`; `ARG_SUBSTRING_REQ`, `ARG_OFFSET`.

- [ ] **Step 1: Write the failing test**

In `tests/test_youtrack.py`:

```python
def test_users_find_sends_query_and_paging(tmp_path):
    class UsersFindHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps([
                {"id": "1-1", "login": "s.royz", "fullName": "Sergey Royz"},
                {"id": "1-2", "login": "s.other", "fullName": "Other Person"},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    UsersFindHandler.requests = []
    with serve(UsersFindHandler) as base:
        result = run_cli(tmp_path, base, "users", "find", "royz",
                         "--limit", "2", "--offset", "5")
    assert result.returncode == 0, result.stderr
    path = UsersFindHandler.requests[0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert query["query"] == ["royz"]
    assert query["$top"] == ["3"]          # limit + 1, so truncation is detectable
    assert query["$skip"] == ["5"]
    payload = json.loads(result.stdout)
    assert payload["items"][0]["login"] == "s.royz"
    assert payload["has_more"] is False    # 2 rows returned for limit 2


def test_users_find_rejects_negative_offset(tmp_path):
    with serve(Handler) as base:
        result = run_cli(tmp_path, base, "users", "find", "x", "--offset", "-1")
    assert result.returncode == 6
    assert "offset" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k users_find -v`
Expected: FAIL — argparse rejects `users find` with exit 2 ("invalid choice"), so the assertions on `returncode` fail.

- [ ] **Step 3: Write the minimal implementation**

Add beside `_positive_limit` (~line 826):

```python
def _nonnegative_offset(value: int) -> None:
    if value < 0:
        _die(6, "input", "--offset cannot be negative")


def _page_params(a, fields: str) -> dict:
    """Paging params that fetch one extra row so truncation is detectable.

    `$top = limit + 1` is deliberate: YouTrack has no total-count field on these
    endpoints, so the only way to know the caller's page was cut short is to ask
    for one more row than we intend to return. `_paged` discards it.
    """
    _positive_limit(a.limit)
    _nonnegative_offset(a.offset)
    params = {"fields": fields, "$top": a.limit + 1}
    if a.offset:
        params["$skip"] = a.offset
    return params


def _paged(rows, limit: int) -> dict:
    """The list envelope: `limit` items, plus whether the server had more."""
    rows = rows if isinstance(rows, list) else []
    return {"items": rows[:limit], "has_more": len(rows) > limit}
```

Add after `users_me` (~line 1201):

```python
def users_find(c, a):
    params = _page_params(a, USER_FIELDS)
    params["query"] = a.substring
    return _paged(_request(c, "GET", "/users", params=params), a.limit)
```

Add to the `ARG_*` block:

```python
ARG_SUBSTRING_REQ = (("substring",), {})
ARG_OFFSET        = (("--offset",), {"type": int, "default": 0})
```

Add to `COMMANDS`, directly after the `("users", "me")` row:

```python
    ("users", "find"):                (users_find, [ARG_SUBSTRING_REQ, ARG_LIMIT50, ARG_OFFSET]),
```

Add to the module docstring, in the `Read:` block after the `users me` line — the drift tests require the exact `youtrack users find` prefix:

```
  youtrack users find SUBSTRING [--limit N] [--offset N]
                                             Find users by substring across
                                             login, name and email. The visible
                                             set is token-scoped, so absence
                                             here does not prove a login is
                                             invalid.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k users_find -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole suite — the drift tests are the point**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: all pass. `test_every_command_is_documented` fails if the docstring line was missed; `test_every_documented_command_exists` fails if it was mistyped.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): users find, with the shared paging helpers

Adds the M3 prerequisite for Assignee/Requestor/Approver: an agent can now
confirm a login exists before writing it. Deliberately not wired into the
create/update pre-flight — the visible user set is token-scoped, so absence
does not prove a login is invalid.

Carries the paging helpers the rest of M3 reuses. \$top is limit+1 because
these endpoints expose no total count, so over-fetching one row is the only
way to detect a truncated page."
```

---

### Task 2: `--offset` and the `has_more` envelope on every `--limit` verb

Implements **D6**. Breaking output change: four existing verbs stop returning a bare array.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `issues_search` (~line 1327), `issues_comments_list` (~line 1335), `articles_list` (~line 1357), `articles_comments_list` (~line 1368), `COMMANDS` rows for all four, module docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params`, `_paged`, `ARG_OFFSET` from Task 1.
- Produces: all four verbs return `{"items": [...], "has_more": bool}`.

- [ ] **Step 1: Write the failing tests — the boundary is the whole point**

```python
def _paging_handler(rows):
    class PagingHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            top = int(query["$top"][0])
            body = json.dumps(rows[:top]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    PagingHandler.requests = []
    return PagingHandler


def test_search_reports_has_more_false_at_exactly_limit(tmp_path):
    rows = [{"idReadable": f"DEMO-{n}", "summary": "s"} for n in range(2)]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO",
                         "--limit", "2")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["items"]) == 2
    assert payload["has_more"] is False


def test_search_reports_has_more_true_and_trims_to_limit(tmp_path):
    rows = [{"idReadable": f"DEMO-{n}", "summary": "s"} for n in range(5)]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO",
                         "--limit", "2")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["items"]) == 2, "the extra probe row must not be emitted"
    assert payload["has_more"] is True
    assert [i["idReadable"] for i in payload["items"]] == ["DEMO-0", "DEMO-1"]


def test_comments_list_sends_skip_and_envelopes(tmp_path):
    rows = [{"id": "4-1", "text": "hi"}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "list", "DEMO-1",
                         "--limit", "10", "--offset", "20")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert query["$skip"] == ["20"]
    assert query["$top"] == ["11"]
    assert json.loads(result.stdout)["has_more"] is False


def test_offset_zero_is_omitted_from_the_request(tmp_path):
    handler = _paging_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert "$skip" not in query
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "has_more or skip_and_envelopes or offset_zero" -v`
Expected: FAIL — `--offset` is not a recognised flag (exit 2), and `issues search` emits a bare array so `payload["items"]` raises `TypeError`.

- [ ] **Step 3: Write the minimal implementation**

Replace the four verb bodies:

```python
def issues_search(c, a):
    rows = _request(c, "GET", "/issues",
                    params={**_page_params(a, ISSUE_SEARCH_FIELDS),
                            "query": a.query})
    return _paged([_flatten_custom_fields(i) for i in rows], a.limit)


def issues_comments_list(c, a):
    return _paged(_request(c, "GET", f"/issues/{_issue_ref(a.issue)}/comments",
                           params=_page_params(a, COMMENT_FIELDS)), a.limit)
```

```python
def articles_list(c, a):
    params = _page_params(a, ARTICLE_LIST_FIELDS)
    if a.project:
        params["project"] = a.project
    return _paged(_request(c, "GET", "/articles", params=params), a.limit)


def articles_comments_list(c, a):
    return _paged(_request(c, "GET", f"/articles/{_article_ref(a.article)}/comments",
                           params=_page_params(a, ARTICLE_COMMENT_FIELDS)), a.limit)
```

> **Read `articles_list` before editing it.** Its current project-scoping line may differ from the sketch above; keep whatever it does today and only change the params source and the return wrapper.

Add `ARG_OFFSET` to all four `COMMANDS` rows:

```python
    ("issues", "search"):             (issues_search, [ARG_QUERY, ARG_LIMIT100, ARG_OFFSET]),
    ("issues", "comments", "list"):   (issues_comments_list, [ARG_ISSUE, ARG_LIMIT50, ARG_OFFSET]),
    ("articles", "list"):             (articles_list, [ARG_PROJECT_OPT, ARG_LIMIT100, ARG_OFFSET]),
    ("articles", "comments", "list"): (articles_comments_list, [ARG_ARTICLE, ARG_LIMIT50, ARG_OFFSET]),
```

Update those four lines in the module docstring to show `[--offset N]`, and add this paragraph to the docstring's output section:

```
  Every verb that takes --limit returns {"items": [...], "has_more": bool}
  rather than a bare array. has_more is true when the server had more rows
  than --limit; page on with --offset. This is how a sprint rollup detects
  that it was cut short instead of quietly reporting a partial total.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "has_more or skip_and_envelopes or offset_zero" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the whole suite and fix the pre-existing tests this breaks**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: **several existing tests fail** — any that index a bare array from these four verbs, including `test_issues_search_emits_same_fields_shape` (~line 617). Update each to read `payload["items"]`. Do **not** relax an assertion to make it pass; the shape genuinely changed and the test should assert the new shape.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack)!: page every list verb and report truncation

--offset on all four --limit verbs, and an {items, has_more} envelope so a
truncated page is detectable. Previously --limit cut the result set silently,
which made a sprint rollup quietly wrong rather than obviously wrong.

Applied to all four --limit verbs, not just the two M3 named: fixing
truncation on one while three others still truncate silently reproduces the
bug the milestone exists to remove. projects find is untouched — it has no
--limit (M4).

BREAKING CHANGE: issues search, issues comments list, articles list and
articles comments list now return {\"items\": [...], \"has_more\": bool}
instead of a bare JSON array."
```

---

### Task 3: `--select` on `issues search`

Implements **D7**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — helper beside `_paged`, `issues_search`, `ARG_*`, `COMMANDS`, docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_paged` (Task 1), enveloped `issues_search` (Task 2).
- Produces: `_SEARCH_SELECT_KEYS: set[str]`; `_select_keys(raw: str) -> list[str]`; `_apply_select(issue: dict, keys: list[str]) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
def test_select_filters_core_and_nested_field_keys(tmp_path):
    rows = [{"idReadable": "DEMO-1", "summary": "s", "description": "long text",
             "customFields": [
                 {"name": "State", "$type": "StateIssueCustomField",
                  "value": {"name": "Open"}},
                 {"name": "Points", "$type": "SimpleIssueCustomField",
                  "value": 3},
             ]}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO",
                         "--select", "idReadable,fields.State")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"] == [
        {"idReadable": "DEMO-1", "fields": {"State": "Open"}}
    ]


def test_select_omits_a_field_absent_from_this_issue(tmp_path):
    rows = [{"idReadable": "DEMO-1", "summary": "s", "customFields": []}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO",
                         "--select", "idReadable,fields.Points")
    assert result.returncode == 0, result.stderr
    # A field this issue does not carry is absent, not an error: custom fields
    # differ per project and a cross-project search must survive it.
    assert json.loads(result.stdout)["items"] == [{"idReadable": "DEMO-1"}]


def test_select_rejects_an_unknown_key_shape(tmp_path):
    handler = _paging_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO",
                         "--select", "idReadabel")
    assert result.returncode == 6
    assert "idReadable" in result.stderr, "must offer the near-miss"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k select -v`
Expected: FAIL — `--select` is not a recognised flag, argparse exits 2.

- [ ] **Step 3: Write the minimal implementation**

Add beside `_paged`:

```python
# The top-level keys `issues search` can emit, after flattening. `fields.<name>`
# addresses one entry inside the flattened custom-field map.
_SEARCH_SELECT_KEYS = {"id", "idReadable", "summary", "description", "fields"}


def _select_keys(raw: str) -> list[str]:
    """Validate the key *shape* only.

    A bare key must be one this verb emits — a typo there would otherwise
    silently yield empty output. A `fields.X` key is accepted without checking
    X, because custom fields differ per project and a cross-project search must
    not fail on the first issue that lacks one (D7).
    """
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        _die(6, "input", "--select needs at least one key")
    for key in keys:
        if key.startswith("fields.") and len(key) > len("fields."):
            continue
        if key in _SEARCH_SELECT_KEYS:
            continue
        near = difflib.get_close_matches(key, sorted(_SEARCH_SELECT_KEYS), n=3,
                                         cutoff=0.6)
        hint = (f"did you mean: {', '.join(near)}?" if near else
                f"selectable: {', '.join(sorted(_SEARCH_SELECT_KEYS))}, "
                "or fields.<custom field name>")
        _die(6, "unknown_select_key", f"cannot select {key!r}", hint)
    return keys


def _apply_select(issue: dict, keys: list[str]) -> dict:
    picked: dict = {}
    for key in keys:
        if key.startswith("fields."):
            name = key[len("fields."):]
            fields = issue.get("fields") or {}
            if name in fields:
                picked.setdefault("fields", {})[name] = fields[name]
        elif key in issue:
            picked[key] = issue[key]
    return picked
```

Change `issues_search` to apply it after flattening:

```python
def issues_search(c, a):
    keys = _select_keys(a.select) if a.select else None
    rows = _request(c, "GET", "/issues",
                    params={**_page_params(a, ISSUE_SEARCH_FIELDS),
                            "query": a.query})
    shaped = [_flatten_custom_fields(i) for i in rows]
    if keys:
        shaped = [_apply_select(i, keys) for i in shaped]
    return _paged(shaped, a.limit)
```

Add the arg spec and wire it:

```python
ARG_SELECT        = (("--select",), {"metavar": "KEY,KEY"})
```

```python
    ("issues", "search"):             (issues_search, [ARG_QUERY, ARG_LIMIT100, ARG_OFFSET, ARG_SELECT]),
```

Update the `issues search` docstring line to `[--select KEY,KEY]` and describe it:

```
                                             --select trims the output to the
                                             named keys: core attributes
                                             (idReadable, summary, …) or
                                             fields.<name> for one custom
                                             field. A named custom field the
                                             issue does not carry is omitted,
                                             not an error.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k select -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): --select trims issues search output

Filters the CLI's own flattened shape on dotted keys, so it cannot emit a wire
shape a consumer has not seen. A wide search over 45-custom-field issues is
mostly payload the caller did not ask for.

An unknown key shape exits 6 with a near-miss, but fields.X naming a field
absent from a given issue is simply omitted: custom fields differ per project,
so failing there would break every cross-project search."
```

---

### Task 4: Links on `issues get`

Read side first, so the write verbs in Tasks 5–6 have something to verify against. Implements **D5**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `_CF_PROJECTION` area (~line 811), `_flatten_custom_fields` neighbourhood (~line 859), `issues_get`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LINK_PROJECTION: str`; `_link_phrase(link: dict) -> str | None`; `_flatten_links(issue: dict) -> dict`.

- [ ] **Step 1: Write the failing test — 7 slots in, 1 entry out**

```python
# Measured shape: GET /issues/{id}/links returns every possible slot, empty or
# not — 7 on an instance with 4 link types. See the parity plan's "Link model".
_ALL_LINK_SLOTS = [
    {"direction": "BOTH", "linkType": {"name": "Relates",
     "sourceToTarget": "relates to", "targetToSource": ""}, "issues": []},
    {"direction": "OUTWARD", "linkType": {"name": "Depend",
     "sourceToTarget": "is required for", "targetToSource": "depends on"},
     "issues": []},
    {"direction": "INWARD", "linkType": {"name": "Depend",
     "sourceToTarget": "is required for", "targetToSource": "depends on"},
     "issues": []},
    {"direction": "OUTWARD", "linkType": {"name": "Duplicate",
     "sourceToTarget": "is duplicated by", "targetToSource": "duplicates"},
     "issues": []},
    {"direction": "INWARD", "linkType": {"name": "Duplicate",
     "sourceToTarget": "is duplicated by", "targetToSource": "duplicates"},
     "issues": []},
    {"direction": "OUTWARD", "linkType": {"name": "Subtask",
     "sourceToTarget": "parent for", "targetToSource": "subtask of"},
     "issues": []},
    {"direction": "INWARD", "linkType": {"name": "Subtask",
     "sourceToTarget": "parent for", "targetToSource": "subtask of"},
     "issues": [{"idReadable": "DEMO-9"}]},
]


def test_issues_get_filters_empty_link_slots(tmp_path):
    class LinkReadHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps({
                "idReadable": "DEMO-1", "summary": "s",
                "customFields": [],
                "links": _ALL_LINK_SLOTS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    LinkReadHandler.requests = []
    with serve(LinkReadHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["links"] == [{"type": "subtask of", "issues": ["DEMO-9"]}]


def test_issues_get_requests_the_links_projection(tmp_path):
    class LinkFieldHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps({"idReadable": "DEMO-1", "customFields": [],
                               "links": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    LinkFieldHandler.requests = []
    with serve(LinkFieldHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(LinkFieldHandler.requests[0]).query)
    assert "links(" in query["fields"][0]
    # An issue with no links must emit no `links` key at all, not [].
    assert "links" not in json.loads(result.stdout)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "link_slots or links_projection" -v`
Expected: FAIL — `ISSUE_FIELDS` has no `links(...)`, so `"links(" in query["fields"][0]` is False and the payload carries the raw 7-slot array.

- [ ] **Step 3: Write the minimal implementation**

Add beside `_CF_PROJECTION` and extend `ISSUE_FIELDS` only (not `ISSUE_SEARCH_FIELDS` — a wide search does not need links):

```python
LINK_PROJECTION = ("links(direction,linkType(name,sourceToTarget,targetToSource),"
                   "issues(idReadable))")
```

```python
ISSUE_FIELDS = ("id,idReadable,summary,description,created,updated,resolved,"
                "project(id,name,shortName),reporter(id,login,fullName),"
                + _CF_PROJECTION + "," + LINK_PROJECTION)
```

Add beside `_flatten_custom_fields`:

```python
def _link_phrase(link: dict) -> str | None:
    """The phrase naming this link from the read issue's side.

    INWARD reads as targetToSource ("subtask of"), OUTWARD and BOTH as
    sourceToTarget. An undirected type reports targetToSource as "" — measured —
    so BOTH must not consult it.
    """
    link_type = link.get("linkType") or {}
    key = "targetToSource" if link.get("direction") == "INWARD" else "sourceToTarget"
    return (link_type.get(key) or "").strip() or None


def _flatten_links(issue):
    """Drop the empty slots and reduce each link to phrase + issue keys.

    The endpoint returns every possible slot on every issue — 7 where there are
    4 link types — so passing them through would put 7 near-empty entries on
    every single issue read (measured).
    """
    if not isinstance(issue, dict) or "links" not in issue:
        return issue
    links = issue.pop("links") or []
    shaped = []
    for link in links:
        if not isinstance(link, dict):
            continue
        targets = [i.get("idReadable") for i in (link.get("issues") or [])
                   if isinstance(i, dict) and i.get("idReadable")]
        phrase = _link_phrase(link)
        if targets and phrase:
            shaped.append({"type": phrase, "issues": targets})
    if shaped:
        issue["links"] = shaped
    return issue
```

Change `issues_get` to compose both shapers:

```python
def issues_get(c, a):
    return _flatten_links(_flatten_custom_fields(
        _request(c, "GET", f"/issues/{_issue_ref(a.issue)}",
                 params={"fields": ISSUE_FIELDS})))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "link_slots or links_projection" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole suite**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: all pass. `test_issues_get_requests_custom_fields` (~line 606) asserts on the `fields` param — check it still holds now that `ISSUE_FIELDS` is longer.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): return links from issues get

Parentage was previously invisible: the consumer's guide wants a parent link
for Sub-Tasks and today that lives as prose in descriptions (\"Part of
IONDEV-867\"), unqueryable.

Empty slots are filtered. The endpoint returns every possible link slot on
every issue — 7 where the instance has 4 link types — so a pass-through would
put 7 near-empty entries on every read. An issue with no links emits no links
key at all."
```

---

### Task 5: Link phrase resolution and `issues links add`

The core of M3. Implements **D1**, **D2**, **D3** and **D4**, plus the `readOnly` regression guard.

Resolution and the verb land together deliberately: the resolution helpers have no caller of their own, so splitting them would leave a task whose tests cannot pass until the next one.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — new `── issues: links ──` section after the comments verbs (~line 1345), `WRITE_VERBS` (~line 159), `ARG_*` block, `COMMANDS`, module docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_link_phrase` and `issues_get` (Task 4), `_issue_ref`.
- Produces: `_link_slots(c, ref: str) -> list`; `_resolve_link(c, ref: str, phrase: str) -> str` returning the direction-encoded link id; `_link_target_error(target: str)` returning an `on_error` callable; `issues_links_add(c, a)`; `ARG_LINK_TO`, `ARG_LINK_TYPE`.

- [ ] **Step 1: Write the failing tests**

```python
def _link_slots_with_ids():
    """`_ALL_LINK_SLOTS` plus the measured direction-encoded ids."""
    ids = ["137-0", "137-1s", "137-1t", "137-2s", "137-2t", "137-3s", "137-3t"]
    slots = []
    for link_id, slot in zip(ids, _ALL_LINK_SLOTS):
        entry = json.loads(json.dumps(slot))
        entry["id"] = link_id
        entry["issues"] = []
        # Measured: Duplicate and Subtask report readOnly true, and both accept
        # writes. The flag governs editing the type definition, not linking.
        entry["linkType"]["readOnly"] = entry["linkType"]["name"] in (
            "Duplicate", "Subtask")
        slots.append(entry)
    return slots


class _LinkHandler(BaseHTTPRequestHandler):
    """Serves the link slots for GET, records POST/DELETE."""
    requests = []
    slots = None

    def log_message(self, *_args):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, None))
        # Match on the path only. A substring test would also hit the `links(...)`
        # projection inside the ?fields= query that issues_get sends.
        route = urllib.parse.urlparse(self.path).path
        if route.endswith("/links"):
            self._reply(self.__class__.slots)
        elif route.startswith("/api/issues/"):
            self._reply({"id": "2-99", "idReadable": "DEMO-9"})
        else:
            self._reply({"error": "missing"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.__class__.requests.append(("POST", self.path, json.loads(raw)))
        self._reply({"idReadable": "DEMO-9"})

    def do_DELETE(self):
        self.__class__.requests.append(("DELETE", self.path, None))
        self._reply({})


@pytest.fixture
def link_handler():
    _LinkHandler.requests = []
    _LinkHandler.slots = _link_slots_with_ids()
    return _LinkHandler


@pytest.mark.parametrize("phrase,link_id", [
    ("relates to", "137-0"),
    ("is required for", "137-1s"),
    ("depends on", "137-1t"),
    ("is duplicated by", "137-2s"),
    ("duplicates", "137-2t"),
    ("parent for", "137-3s"),
    ("subtask of", "137-3t"),
])
def test_every_direction_phrase_resolves(tmp_path, link_handler, phrase, link_id):
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", phrase)
    assert result.returncode == 0, result.stderr
    posts = [r for r in link_handler.requests if r[0] == "POST"]
    assert len(posts) == 1
    assert f"/links/{link_id}/issues" in posts[0][1]


def test_readonly_link_type_is_still_writable(tmp_path, link_handler):
    # Regression guard. `Subtask` reports readOnly true and accepts writes; a
    # later tidy-up that treats the flag as a gate would disable the one verb
    # M3 exists to deliver, and every other test would still pass.
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask of")
    assert result.returncode == 0, result.stderr
    assert any(r[0] == "POST" for r in link_handler.requests)


def test_phrase_matching_is_case_insensitive(tmp_path, link_handler):
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "SubTask Of")
    assert result.returncode == 0, result.stderr


def test_unknown_phrase_exits_6_with_near_miss(tmp_path, link_handler):
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask off")
    assert result.returncode == 6
    assert "subtask of" in result.stderr
    assert not any(r[0] == "POST" for r in link_handler.requests)


def test_ambiguous_phrase_fails_rather_than_picking(tmp_path, link_handler):
    # Phrase uniqueness is measured on one instance only; a custom link type
    # could collide. Picking one silently would create the wrong link.
    slots = _link_slots_with_ids()
    slots.append({"id": "137-4s", "direction": "OUTWARD",
                  "linkType": {"name": "Custom", "sourceToTarget": "subtask of",
                               "targetToSource": "parent of",
                               "readOnly": False},
                  "issues": []})
    _LinkHandler.slots = slots
    with serve(_LinkHandler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask of")
    assert result.returncode == 6
    assert "ambiguous" in result.stderr.lower()
    assert not any(r[0] == "POST" for r in _LinkHandler.requests)
```

Then, in the same file, the tests for the verb these helpers serve:


```python
def test_self_link_is_refused_without_any_request(tmp_path, link_handler):
    # Measured: the server returns 200 for a self-link and silently creates
    # nothing. Asserting the exit code alone would pass for a client that sent
    # it, so assert that no write left the process.
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-1", "--type", "relates to")
    assert result.returncode == 6
    assert "itself" in result.stderr.lower()
    assert not any(r[0] == "POST" for r in link_handler.requests)


def test_links_add_sends_the_readable_key(tmp_path, link_handler):
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask of")
    assert result.returncode == 0, result.stderr
    post = [r for r in link_handler.requests if r[0] == "POST"][0]
    assert post[2] == {"idReadable": "DEMO-9"}


def test_links_add_translates_a_bad_target(tmp_path):
    class BadTargetHandler(_LinkHandler):
        requests = []
        slots = None

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, None))
            self._reply({"error": "Bad Request", "error_description":
                         "YouTrack is unable to locate an Issue-type entity "
                         "unless its ID is also provided"}, 400)

    BadTargetHandler.requests = []
    BadTargetHandler.slots = _link_slots_with_ids()
    with serve(BadTargetHandler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-404", "--type", "relates to")
    # The raw message describes an id-format problem, which is not what
    # happened. Exit 3 naming the target is the honest translation.
    assert result.returncode == 3
    assert "DEMO-404" in result.stderr


def test_links_add_is_a_write_verb():
    assert "issues links add" in _write_verbs()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "phrase or readonly_link" -v`
Expected: FAIL — `issues links add` is not in `COMMANDS`, so argparse exits 2 on every one.

- [ ] **Step 3: Write the resolution helpers**

Add a new banner section after `issues_comments_add`:

```python
# ── issues: links ────────────────────────────────────────────────────────
#
# Measured 2026-07-28, see .claude/plans/2026-07-27-youtrack-mcp-parity.md.
#
# GET /issues/{id}/links returns every possible slot — 7 where the instance has
# 4 link types — each with a direction-encoded id (`137-3s` OUTWARD, `137-3t`
# INWARD, bare `137-0` undirected). That suffix convention is an observed
# pattern, not a documented contract, so ids are read from this call rather
# than built. The same payload carries the phrase, so one GET resolves both.
#
# `linkType.readOnly` is NOT a write gate: Duplicate and Subtask both report it
# and both accept links. It governs editing the type definition. Do not branch
# on it.

def _link_slots(c, ref: str) -> list:
    slots = _request(c, "GET", f"/issues/{ref}/links",
                     params={"fields": "id,direction,"
                                       "linkType(name,sourceToTarget,targetToSource)"})
    return slots if isinstance(slots, list) else []


def _resolve_link(c, ref: str, phrase: str) -> str:
    """The direction-encoded link id for a direction phrase.

    Fails on an ambiguous phrase rather than picking: uniqueness is measured on
    one instance, and a custom link type could collide.
    """
    wanted = phrase.strip().casefold()
    slots = _link_slots(c, ref)
    matches, phrases = [], []
    for slot in slots:
        text = _link_phrase(slot)
        if not text or not slot.get("id"):
            continue
        phrases.append(text)
        if text.casefold() == wanted:
            matches.append(slot["id"])
    if len(matches) > 1:
        _die(6, "ambiguous_link_type",
             f"{phrase!r} is ambiguous on this instance: it names "
             f"{len(matches)} link directions",
             "rename the colliding custom link type, or file the link in the "
             "YouTrack UI")
    if not matches:
        near = difflib.get_close_matches(phrase, phrases, n=3, cutoff=0.6)
        hint = (f"did you mean: {', '.join(near)}?" if near
                else f"link directions: {', '.join(sorted(set(phrases)))}")
        _die(6, "unknown_link_type", f"no link direction named {phrase!r}", hint)
    return matches[0]
```

- [ ] **Step 4: Write the verb, and register it**


Append to the `── issues: links ──` section:

```python
def _link_target_error(target: str):
    """Translate the measured add-path failures.

    A nonexistent target returns 400 "YouTrack is unable to locate an
    Issue-type entity unless its ID is also provided" — a message about id
    format, which is not what went wrong.
    """
    def on_error(response):
        if response.status_code == 400 and "unable to locate" in response.text:
            _die(3, "not_found", f"no issue named {target!r}",
                 "check the target key; links accept a readable key like "
                 "PROJ-123", status=400)
    return on_error


def issues_links_add(c, a):
    ref, target = _issue_ref(a.issue), _issue_ref(a.to)
    if ref.casefold() == target.casefold():
        _die(6, "input", f"{ref} cannot be linked to itself",
             "YouTrack accepts a self-link with 200 and silently creates "
             "nothing, so this is refused here rather than reported as done")
    link_id = _resolve_link(c, ref, a.type)
    _request(c, "POST", f"/issues/{ref}/links/{link_id}/issues",
             params={"fields": "idReadable"}, json={"idReadable": target},
             on_error=_link_target_error(target))
    # Report by reading the issue back: one write populates both sides, and the
    # POST response echoes only the target.
    return issues_get(c, argparse.Namespace(issue=ref))
```

Add to `WRITE_VERBS` — **`issues links add` only.** `issues links remove` joins it in Task 6, alongside its `COMMANDS` row: the existing `test_write_verbs_match_commands` asserts `WRITE_VERBS ⊆ COMMANDS`, so a write verb listed before its dispatch row exists turns the suite red.

```python
WRITE_VERBS = {"issues create", "issues update", "issues comments add",
               "issues links add",
               "articles create", "articles update", "articles comments add"}
```

Add the arg specs and the `COMMANDS` row:

```python
ARG_LINK_TO       = (("--to",), {"required": True})
ARG_LINK_TYPE     = (("--type",), {"required": True, "metavar": "PHRASE"})
```

```python
    ("issues", "links", "add"):        (issues_links_add, [ARG_ISSUE, ARG_LINK_TO, ARG_LINK_TYPE]),
```

Add to the docstring's `Write:` block:

```
  youtrack issues links add ISSUE --to ISSUE --type PHRASE
                                             Link two issues. --type takes the
                                             direction phrase, not a type name:
                                             "subtask of", "parent for",
                                             "relates to", "depends on", "is
                                             required for", "duplicates", "is
                                             duplicated by". One write fills in
                                             both sides. Linking an issue to
                                             itself is refused.
```

> `issues_links_add` calls `issues_get`, which needs `argparse` in scope. It is already imported at module top; do not re-import.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "self_link or links_add or phrase or readonly_link" -v`
Expected: PASS — 13 tests (7 parametrized phrases, plus readOnly, case-insensitivity, unknown phrase, ambiguous phrase, self-link, readable-key body, bad target, and the `WRITE_VERBS` membership check).

- [ ] **Step 6: Run the whole suite**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): issues links add, with client-side phrase resolution

--type takes the direction phrase, so no second flag is needed and the caller
cannot pair a type with a direction it does not have. All 7 phrases on the
instance are unique (measured), so a phrase identifies both type and direction.

Ids are read from GET /issues/{id}/links rather than built by appending s/t to
a type id: that suffix is an observed pattern, not a documented contract, and
the same call already carries the phrase. An ambiguous phrase fails rather than
picking — uniqueness is measured on one instance and a custom link type could
collide, where guessing would silently create the wrong link.

linkType.readOnly is not a write gate: Duplicate and Subtask both report it and
both accept links. A test pins this, because respecting the flag would disable
subtask of while every other test still passed.

Self-links are refused before any request. Measured: YouTrack returns 200 and
silently creates nothing, so sending one would report success for a no-op — the
test asserts no POST leaves the process, not just the exit code.

A nonexistent target returns 400 with a message about id format, which is not
what went wrong; it is translated to exit 3 naming the target. The verb reports
by reading the issue back, since one write populates both sides while the POST
response echoes only the target."
```

---

### Task 6: `issues links remove`

Implements **D8**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `── issues: links ──` section, `COMMANDS`, docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_resolve_link` and `_link_target_error` (Task 5), `issues_get`.
- Produces: `issues_links_remove(c, a)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_links_remove_resolves_the_internal_target_id(tmp_path, link_handler):
    # Measured asymmetry: POST accepts a readable key, DELETE demands the
    # internal id and 404s on a readable one. So remove pays a resolution GET.
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "remove", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask of")
    assert result.returncode == 0, result.stderr
    deletes = [r for r in link_handler.requests if r[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][1].endswith("/links/137-3t/issues/2-99")


def test_links_remove_retranslates_the_missing_link_404(tmp_path):
    class NoSuchLinkHandler(_LinkHandler):
        requests = []
        slots = None

        def do_DELETE(self):
            self.__class__.requests.append(("DELETE", self.path, None))
            # Measured: the message names the *target issue*, which exists.
            self._reply({"error": "Not Found", "error_description":
                         "Entity with id 2-99 not found"}, 404)

    NoSuchLinkHandler.requests = []
    NoSuchLinkHandler.slots = _link_slots_with_ids()
    with serve(NoSuchLinkHandler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "remove", "DEMO-1",
                         "--to", "DEMO-9", "--type", "subtask of")
    assert result.returncode == 3
    # Must blame the link, not the issue: the issue in YouTrack's message exists.
    assert "subtask of" in result.stderr
    assert "link" in result.stderr.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k links_remove -v`
Expected: FAIL — argparse exits 2, the verb does not exist.

- [ ] **Step 3: Write the implementation**

Append to the `── issues: links ──` section:

```python
def _missing_link_error(ref: str, target: str, phrase: str):
    """Retranslate remove's 404.

    Measured: deleting a link that does not exist returns 404 whose message
    names the *target issue* — which exists. Passing it through would tell the
    caller the issue is missing when the link is.
    """
    def on_error(response):
        if response.status_code == 404:
            _die(3, "not_found",
                 f"{ref} has no {phrase!r} link to {target}",
                 "read the issue to see the links it does have", status=404)
    return on_error


def issues_links_remove(c, a):
    ref, target = _issue_ref(a.issue), _issue_ref(a.to)
    link_id = _resolve_link(c, ref, a.type)
    # DELETE needs the internal id; a readable key 404s (measured).
    resolved = _request(c, "GET", f"/issues/{target}", params={"fields": "id"},
                        on_error=_link_target_error(target))
    internal = (resolved or {}).get("id")
    if not internal:
        _die(3, "not_found", f"no issue named {target!r}")
    _request(c, "DELETE", f"/issues/{ref}/links/{link_id}/issues/{internal}",
             on_error=_missing_link_error(ref, target, a.type))
    return issues_get(c, argparse.Namespace(issue=ref))
```

Add the `COMMANDS` row **and** the `WRITE_VERBS` entry in the same step — the existing `test_write_verbs_match_commands` asserts `WRITE_VERBS ⊆ COMMANDS`, so these two must always land together:

```python
    ("issues", "links", "remove"):     (issues_links_remove, [ARG_ISSUE, ARG_LINK_TO, ARG_LINK_TYPE]),
```

```python
WRITE_VERBS = {"issues create", "issues update", "issues comments add",
               "issues links add", "issues links remove",
               "articles create", "articles update", "articles comments add"}
```

Add to the docstring's `Write:` block:

```
  youtrack issues links remove ISSUE --to ISSUE --type PHRASE
                                             Remove a link. Not idempotent:
                                             removing a link that is not there
                                             exits 3.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k links_remove -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole suite**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): issues links remove

Pays one resolution GET because the endpoints are asymmetric: POST accepts a
readable key, DELETE demands the internal id and 404s on a readable one
(measured).

Its 404 is retranslated. YouTrack's message names the target issue, which
exists — the missing entity is the link — so passing it through would send the
caller looking for a deleted issue."
```

---

### Task 7: Live verification against ION

No new code. This is where the shipped verbs meet the real server, under the ION-only constraint.

**Files:** none modified.

- [ ] **Step 1: Install the branch build**

```bash
cd /Users/zjor/src/ai-cluster-one-capabilities
capabilities audit youtrack --from .
capabilities install youtrack --from capabilities/youtrack/bin/youtrack
youtrack --version 2>/dev/null || youtrack manifest --json | head -5
```
Expected: audit clean, install succeeds.

- [ ] **Step 2: Create two throwaway ION issues, verifying the target project on each**

Run from `/Users/zjor/projects/ion/agents` so the `ionwater` connection resolves:

```bash
cd /Users/zjor/projects/ion/agents
for n in A B; do
  youtrack issues create --project 0-1 --summary "PROBE M3 verify $n (safe to delete)" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['idReadable'], d['project']['shortName'])"
done
```
Expected: two `ION-*` keys, each printing `ION`. **If either prints anything other than `ION`, stop and delete it** — the connection fell back to the wrong instance.

- [ ] **Step 3: Exercise the link verbs end to end**

Substitute the two real keys for `$A` and `$B`:

```bash
youtrack issues links add $A --to $B --type "subtask of"
youtrack issues get $A   | python3 -c "import json,sys; print(json.load(sys.stdin).get('links'))"
youtrack issues get $B   | python3 -c "import json,sys; print(json.load(sys.stdin).get('links'))"
youtrack issues links remove $A --to $B --type "subtask of"
youtrack issues get $A   | python3 -c "import json,sys; print(json.load(sys.stdin).get('links'))"
```
Expected: `[{'type': 'subtask of', 'issues': ['<B>']}]` on A and `[{'type': 'parent for', 'issues': ['<A>']}]` on B — reciprocity through the CLI. After remove, `None` on A.

- [ ] **Step 4: Exercise the refusals and paging**

```bash
youtrack issues links add $A --to $A --type "relates to"; echo "self-link exit=$?"      # expect 6
youtrack issues links add $A --to $B --type "subtask off"; echo "typo exit=$?"          # expect 6, near-miss
youtrack issues links remove $A --to $B --type "relates to"; echo "absent exit=$?"      # expect 3
youtrack issues search "project: ION" --limit 2 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['items']), d['has_more'])"
youtrack issues search "project: ION" --limit 2 --offset 2 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print([i['idReadable'] for i in d['items']])"
youtrack issues search "project: ION" --limit 2 --select idReadable,fields.State
youtrack users find royz | python3 -c "import json,sys; print(json.load(sys.stdin)['items'])"
```
Expected: exits 6, 6, 3; `2 True`; a second page with different keys from the first; trimmed output; a non-empty user list.

- [ ] **Step 5: Delete both probe issues and verify they are gone**

The CLI has no delete verb, so call the API directly. Substitute the two real keys:

```bash
cd /Users/zjor/projects/ion/agents
python3 - <<'PY'
import json, urllib.error, urllib.request
KEYS = ["ION-XXXX", "ION-YYYY"]          # <-- the two keys from Step 2
BASE = "https://ion.youtrack.cloud/api"
tok = None
with open("/Users/zjor/projects/ion/agents/.env.local", encoding="utf-8") as fh:
    for line in fh:
        if line.strip().startswith("YOUTRACK_ION_TOKEN="):
            tok = line.strip().split("=", 1)[1].strip().strip("'\"")

def call(method, path):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

for key in KEYS:
    deleted = call("DELETE", f"/issues/{key}")
    check = call("GET", f"/issues/{key}?fields=idReadable")
    print(f"{key}: DELETE={deleted} verify={check} "
          f"{'gone' if check == 404 else 'STILL PRESENT'}")
PY
```
Expected: `DELETE=200 verify=404 gone` for both. Anything else means a probe issue is still live — fix it before moving on.

- [ ] **Step 6: Record the results in the parity plan**

Add an "M3 — verified live" note under the M3 milestone stating what was exercised and what remains unproven: **exit 7 for links was never observed on ION** and stays UNTESTED.

- [ ] **Step 7: Commit the verification note**

```bash
git add .claude/plans/2026-07-27-youtrack-mcp-parity.md
git commit -m "docs(youtrack): record M3 live verification against ION"
```

---

### Task 8: Catalog reindex, audit, and consumer refresh

The step M1 skipped, which left `main` uninstallable.

**Files:**
- Modify: `.capability-source/catalog.json`, `.claude/plans/2026-07-27-youtrack-mcp-parity.md`

- [ ] **Step 1: Read the hashing algorithm from source, do not guess it**

Read `_payload_sha256` in `bin/capabilities`. The hash covers `capabilities/<name>/` recursively, skipping `meta.json`, `stub`, `manifest.json`, `__pycache__`, and `.pyc`/`.pyo`/`.session*`, feeding `<relpath>\0<bytes>\0` per file in sorted order into one sha256.

- [ ] **Step 2: Recompute every entry and confirm the unchanged capabilities reproduce byte-for-byte**

Run this from the repo root. It only *reports* — it writes nothing:

```bash
python3 - <<'PY'
import hashlib, json, pathlib
SKIP_NAMES = {"meta.json", "stub", "manifest.json", "__pycache__"}
SKIP_SUFFIX = (".pyc", ".pyo")

def payload_sha256(root: pathlib.Path) -> str:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_NAMES or path.name.startswith(".session"):
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        files.append(path)
    digest = hashlib.sha256()
    for path in files:
        rel = str(path.relative_to(root))
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()

catalog = json.loads(pathlib.Path(".capability-source/catalog.json").read_text())
entries = catalog.get("capabilities", catalog)
for name in sorted(entries):
    got = payload_sha256(pathlib.Path("capabilities") / name)
    want = entries[name].get("payload_sha256")
    print(f"{'OK  ' if got == want else 'DIFF'} {name:<12} committed={want} computed={got}")
PY
```
Expected: `OK` for every capability **except** `youtrack`, which must read `DIFF`. **If any untouched capability reports `DIFF`, stop** — the reimplementation above has drifted from `_payload_sha256` in `bin/capabilities`; reread that function rather than adjusting the expected value. The catalog's exact nesting also varies; if `entries` comes out wrong, print `catalog` and adapt the accessor.

- [ ] **Step 3: Update only youtrack's `payload_sha256` and `summary`**

Paste the youtrack hash Step 2 computed into `NEW_HASH`, then run this from the repo root:

```bash
python3 - <<'PY'
import json, pathlib, subprocess
NEW_HASH = "<the computed youtrack hash printed by Step 2>"

path = pathlib.Path(".capability-source/catalog.json")
doc = json.loads(path.read_text())
entries = doc.get("capabilities", doc)          # adapt if the nesting differs
summary = json.loads(subprocess.run(
    ["youtrack", "manifest", "--json"],
    capture_output=True, text=True, check=True).stdout)["summary"]
entries["youtrack"]["payload_sha256"] = NEW_HASH
entries["youtrack"]["summary"] = summary
path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print("updated")
PY
git diff --stat .capability-source/catalog.json
```

Expected diff: exactly two changed lines in `.capability-source/catalog.json`. More than that means the serialization conventions do not match; revert and re-check.

- [ ] **Step 4: Cross-check against the manager's own indexer**

```bash
capabilities source index <id>
```
Read the cached catalog **before** any `install` re-clones over it, and confirm it agrees.

- [ ] **Step 5: Update the parity plan's status markers**

Mark M3 and its steps 1–3 ✅ in both the status block at the top and the M3 section. Update the MCP parity table: `find_user`, `link_issues`, `get_issue_comments` and `search_issues` all move to parity, and the "as of" count line changes from 9 to 13 at parity.

- [ ] **Step 6: Audit, then reinstall from the directory to prove the catalog is right**

```bash
capabilities audit youtrack --from .
capabilities install youtrack --source <id>
```
Expected: no `catalog_drift` (exit 7). That failure is the whole reason this task exists.

- [ ] **Step 7: Refresh the consumer context**

```bash
cd /Users/zjor/projects/ion/agents
contextkit build --target claude
```
`contextkit build` alone writes only the codex target. Both files are gitignored build artifacts — nothing to commit.

- [ ] **Step 8: Commit**

```bash
git add .capability-source/catalog.json .claude/plans/2026-07-27-youtrack-mcp-parity.md
git commit -m "chore(source): reindex youtrack catalog and mark M3 shipped

Any payload change must reindex .capability-source/catalog.json in the same
commit or install refuses with catalog_drift. M1 shipped without this and left
main uninstallable until a follow-up PR."
```

---

## Verification

Full suite plus the two structural guards, from `capabilities/youtrack`:

```bash
python3 -m pytest tests/test_youtrack.py -q
```

Expected: 105 pre-existing tests plus ~22 new, all passing. `test_every_command_is_documented` and `test_every_documented_command_exists` cover the three new verbs; `test_write_verbs_match_commands` covers the two new write verbs.

## Rollback

Every task is a single commit on `feat/youtrack-m3-work-the-board`; nothing is pushed. To undo one task, `git revert <sha>`. To abandon M3 entirely, `git checkout main && git branch -D feat/youtrack-m3-work-the-board` — no installed artifact changes until Task 7 Step 1, and reinstalling from `upstream/main` restores the M2 build.

## Out of scope

`projects find` paging (no `--limit` today), `articles search`, `articles update --parent`, `projects get`, `issues tags`, `issues work`, `searches list`, `groups find`/`members`, comment and article visibility — all M4. Exit 7 for links stays UNTESTED under the ION-only constraint. M2's `text`-on-create criterion stays blocked: ION carries no `text` field.

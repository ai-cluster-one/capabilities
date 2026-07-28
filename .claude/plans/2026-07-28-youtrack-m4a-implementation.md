# youtrack M4a — Close the Unblocked Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven MCP parity gaps that need no probe — group lookup, project and saved-search reads, article search and re-parenting, comment visibility — taking parity from 13 to 20 of 23, and remove the last silently-truncating list verb.

**Architecture:** All changes land in the single file `capabilities/youtrack/bin/youtrack`. Five new verbs plus two flag additions, in dependency order: `groups find` must exist before comment visibility, because `permittedGroups` requires a group **id** and rejects a name, so a caller-facing `--permitted-groups` needs a name→id lookup. Every new list verb takes `--limit`/`--offset` and returns the `{"items": …, "has_more": …}` envelope from the start, so M4a adds no new silently-truncating verb.

**Tech Stack:** Python 3 single-file CLI with PEP-723 inline deps (`httpx`), argparse driven by the declarative `COMMANDS` table, pytest against a real local `ThreadingHTTPServer` (no mocking library).

## Global Constraints

- **Design source of truth:** `.claude/plans/2026-07-27-youtrack-mcp-parity.md`, sections "M4a — Close the unblocked tail" and "What remains". Every endpoint below is measured live; do not re-derive from the REST docs.
- **One file.** Do not split `bin/youtrack`. M4a adds ~150 lines of domain code; the plan's revisit trigger is ~1,500 and current domain code is ~1,050.
- **Generated contract fences must not be edited** — `capability core` and `connections`. Locate them by their `# >>> contract:` / `# <<< contract:` markers rather than by line number, since every task shifts the numbers.
- **Exit codes are fixed:** `0` success, `2` auth/config, `3` not found, `4` policy refusal, `5` network/server, `6` input, `7` workflow-rule rejection.
- **Output contract:** values flatten to scalars, arrays, or null — never YouTrack's `{"name": …, "$type": …}` wire shape.
- **Every verb taking `--limit` returns `{"items": [...], "has_more": bool}`** via the shared `_page_params` / `_paged` pair. New list verbs get this from the start — do not add a verb that returns a bare array.
- **Every new write verb must be added to `WRITE_VERBS`, in the same step as its `COMMANDS` row.** An existing test asserts `WRITE_VERBS ⊆ COMMANDS`; adding one without the other turns the suite red. (M3 lost a round to exactly this.)
- **The module docstring is the single source of truth for the command surface.** Two existing tests fail if it disagrees with `COMMANDS`.
- **Any payload change requires reindexing `.capability-source/catalog.json` in the same commit** (Task 7). Skipping it left `main` uninstallable after M1.
- **Run the test suite in the FOREGROUND**, never backgrounded, never via a monitor. It takes ~4 minutes. Every M3 implementer that backgrounded it lost the result and had to be resumed:
  `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
- **Live checks run from `/Users/zjor/projects/ion/agents`, never from the capabilities repo.** The `ionwater` connection lives in that project's envelope and is invisible elsewhere, so a call from the wrong directory silently falls back to a *different YouTrack instance* that also has `allow_write: true`. This has already created stray issues on the wrong server twice. `--connection ionwater` is not a workaround — that connection does not resolve outside the project.
- **All live experiments use the ION project only. IONDEV must not be touched** (owner decision). Verify `project.shortName == "ION"` on anything created.

---

## Measured facts this plan is built on

All measured live against ION on 2026-07-28. Fixtures come from here, not from the docs.

| Endpoint | Shape |
|---|---|
| `GET /groups` | `id`, `name`, `description`, `usersCount`; `$type` is `NestedGroup` |
| `GET /groups/{id}/users` | standard user shape (`id`, `login`, `fullName`, `email`) |
| `GET /admin/projects/{id}` | `name`, `shortName`, `description`, `archived`, `leader` |
| `GET /savedQueries` | `id`, `name`, `query`, `owner(login)`, `visibleFor(name)` |
| `GET /articles?query=…` | **the same endpoint `articles list` uses.** `summary: DWH` → 1 hit, bare `DWH` → 3 (full-text), nonsense → 0 |

**An unknown field in a projection is silently dropped, not an error.** Asking for `issuesCount` on a project returned a response with that key simply absent. So a typo in a projection fails silently — projections must be written carefully, and a test asserting on returned keys is worth more than one asserting the request string.

**Comment visibility, measured:**

- `permittedUsers` resolves by **`login`** — `{"login": "s.royz"}` → 200.
- `permittedGroups` requires the group **`id`** and **rejects a name** — `{"name": "ION Team"}` → `400` `unable to locate an UserGroup-type entity unless its ID is also provided`; `{"id": "3-4"}` → 200.
- `visibility` needs an explicit **`$type`** — omitting it returns `400` with a type mismatch, the same trap M2 hit on custom fields. Send `{"$type": "LimitedVisibility", …}`.
- A **bogus group name and a name-instead-of-id return the identical 400**, so the server cannot tell the caller which mistake they made. Client-side resolution is the only way.
- A **rejected comment write leaves no comment**, verified by reading the list back.
- An unrestricted comment reads back `visibility: {"$type": "UnlimitedVisibility"}`, so the read projection needs `$type` to distinguish restricted from public.

---

## Decisions locked before implementation

| # | Decision | Why |
|---|---|---|
| E1 | `groups find` ships **before** comment visibility, and visibility resolves group **names → ids** client-side | `permittedGroups` rejects names. Without the lookup, a name-based flag cannot work at all. |
| E2 | An unknown group name exits **6** with a `difflib` near-miss over visible group names | The server's 400 cannot distinguish "wrong shape" from "no such group"; only the client can. |
| E3 | Every new list verb (`groups find`, `groups members`, `searches list`, `articles search`) takes `--limit`/`--offset` and returns the envelope | Otherwise M4a reintroduces the silent truncation M3 removed. |
| E4 | `projects get` returns a single object, **not** an envelope | It is a single-entity read, like `issues get` and `articles get`. |
| E5 | `articles search` has **no `--project`** flag | The surface contract specifies `articles search QUERY [--limit N] [--offset N]`. Project scoping is what `articles list --project` is for. |
| E6 | `articles update --parent` **reuses** create's parent resolution, extracted to a helper, including its same-project pre-check | Create already resolves ref→id and refuses a cross-project parent. Two behaviours for one concept would be a defect. Costs update one GET to learn its own project. |
| E7 | `groups members` takes a group **id**, not a name | Matches the surface contract, and mirrors the established look-it-up-first pattern (`users find` → login, `groups find` → id). |
| E8 | Adding `visibility` to `COMMENT_FIELDS` changes `issues comments list` output additively | A new key on an existing object. Not breaking; no envelope change. |

---

## File Structure

| File | Responsibility in M4a |
|---|---|
| `capabilities/youtrack/bin/youtrack` | All implementation. New field projections beside the existing ones; `groups`/`searches` verbs in the `projects · users · groups · searches` section; the parent-article helper beside `articles_create`; visibility helpers beside `issues_comments_add`. |
| `capabilities/youtrack/bin/youtrack` (module docstring) | The help contract — SHEBANG makes it the source of truth for the surface. |
| `capabilities/youtrack/tests/test_youtrack.py` | All tests, extending the existing local-HTTP-server pattern. |
| `.capability-source/catalog.json` | `payload_sha256` + `summary` reindex, Task 7, same commit as the last payload change. |
| `.claude/plans/2026-07-27-youtrack-mcp-parity.md` | Mark M4a shipped, Task 7. |

---

### Task 1: `groups find` and `groups members`

First because comment visibility (Task 4) cannot work without `groups find`.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — module docstring, field projections near `USER_FIELDS`, new verbs in the `projects · users · groups · searches` section, `ARG_*` block, `COMMANDS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params(a, fields)`, `_paged(rows, limit)`, `_request`, `USER_FIELDS` — all existing.
- Produces: `GROUP_FIELDS: str`; `groups_find(c, a)`; `groups_members(c, a)`; `ARG_GROUP_POS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_groups_find_pages_and_envelopes(tmp_path):
    rows = [
        {"id": "3-4", "name": "Administrative Team", "description": None,
         "usersCount": 1},
        {"id": "3-8", "name": "Reports Feature Group", "description": "migrated",
         "usersCount": 17},
    ]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "groups", "find", "Team", "--limit", "2")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert query["$top"] == ["3"]              # limit + 1
    assert query["query"] == ["Team"]
    payload = json.loads(result.stdout)
    assert payload["items"][0]["name"] == "Administrative Team"
    assert payload["has_more"] is False


def test_groups_find_substring_is_optional(tmp_path):
    handler = _paging_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "groups", "find")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert "query" not in query


def test_groups_members_lists_users(tmp_path):
    rows = [{"id": "1-1", "login": "s.royz", "fullName": "Sergey Royz",
             "email": "s@example.com"}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "groups", "members", "3-4", "--limit", "5")
    assert result.returncode == 0, result.stderr
    assert urllib.parse.urlparse(handler.requests[0]).path == "/api/groups/3-4/users"
    assert json.loads(result.stdout)["items"][0]["login"] == "s.royz"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "groups_find or groups_members" -v`
Expected: FAIL — argparse exits 2, `groups` is not a known verb.

- [ ] **Step 3: Write the implementation**

Add beside the other field projections (near `USER_FIELDS`):

```python
GROUP_FIELDS = "id,name,description,usersCount"
```

Add to the `projects · users · groups · searches` section:

```python
def groups_find(c, a):
    params = _page_params(a, GROUP_FIELDS)
    if a.substring:
        params["query"] = a.substring
    return _paged(_request(c, "GET", "/groups", params=params), a.limit)


def groups_members(c, a):
    return _paged(_request(c, "GET", f"/groups/{a.group}/users",
                           params=_page_params(a, USER_FIELDS)), a.limit)
```

Add the arg spec:

```python
ARG_GROUP_POS     = (("group",), {})
```

Add the `COMMANDS` rows:

```python
    ("groups", "find"):               (groups_find, [ARG_SUBSTRING_OPT, ARG_LIMIT100, ARG_OFFSET]),
    ("groups", "members"):            (groups_members, [ARG_GROUP_POS, ARG_LIMIT100, ARG_OFFSET]),
```

Add to the docstring's `Read:` block:

```
  youtrack groups find [SUBSTRING] [--limit N] [--offset N]
                                             Find user groups by substring on
                                             name. Returns the group id, which
                                             is what comment visibility needs —
                                             permittedGroups rejects a name.
  youtrack groups members GROUPID [--limit N] [--offset N]
                                             List the users in a group. Takes
                                             the group id from `groups find`.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "groups_find or groups_members" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
Expected: 151 pre-existing plus 3 new, zero failures. The two docstring-drift tests cover the new verbs.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): groups find and groups members

Closes find_user_groups and get_user_group_members, and unblocks comment
visibility: permittedGroups requires a group id and rejects a name (measured),
so a name-based --permitted-groups needs this lookup to exist first.

Both verbs page and envelope from the start rather than being retrofitted, so
M4a adds no new silently-truncating list verb."
```

---

### Task 2: `projects get` and `searches list`

Two independent read verbs, no dependencies either way.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — docstring, projections, `projects`/`searches` verbs, `COMMANDS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params`, `_paged`, `_request`.
- Produces: `PROJECT_GET_FIELDS: str`; `SAVED_QUERY_FIELDS: str`; `projects_get(c, a)`; `searches_list(c, a)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_projects_get_returns_a_single_object_not_an_envelope(tmp_path):
    class ProjectGetHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps({
                "id": "0-1", "name": "ION", "shortName": "ION",
                "description": None, "archived": False,
                "leader": {"login": "s.royz", "fullName": "Sergey Royz"},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ProjectGetHandler.requests = []
    with serve(ProjectGetHandler) as base:
        result = run_cli(tmp_path, base, "projects", "get", "0-1")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # Single-entity read: no envelope, matching issues get and articles get.
    assert "items" not in payload
    assert payload["shortName"] == "ION"
    assert payload["leader"]["login"] == "s.royz"
    assert urllib.parse.urlparse(ProjectGetHandler.requests[0]).path == \
        "/api/admin/projects/0-1"


def test_searches_list_pages_and_envelopes(tmp_path):
    rows = [{"id": "7-0", "name": "Assigned to me", "query": "for: me",
             "owner": {"login": "s.royz"}, "visibleFor": {"name": "All Users"}}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "searches", "list", "--limit", "5")
    assert result.returncode == 0, result.stderr
    assert urllib.parse.urlparse(handler.requests[0]).path == "/api/savedQueries"
    payload = json.loads(result.stdout)
    assert payload["items"][0]["query"] == "for: me"
    assert payload["has_more"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "projects_get or searches_list" -v`
Expected: FAIL — argparse exits 2 for both.

- [ ] **Step 3: Write the implementation**

Add beside the other projections:

```python
PROJECT_GET_FIELDS = ("id,name,shortName,description,archived,"
                      "leader(login,fullName)")
SAVED_QUERY_FIELDS = "id,name,query,owner(login),visibleFor(name)"
```

Add the verbs:

```python
def projects_get(c, a):
    return _request(c, "GET", f"/admin/projects/{a.project}",
                    params={"fields": PROJECT_GET_FIELDS})


def searches_list(c, a):
    return _paged(_request(c, "GET", "/savedQueries",
                           params=_page_params(a, SAVED_QUERY_FIELDS)), a.limit)
```

Add the `COMMANDS` rows:

```python
    ("projects", "get"):              (projects_get, [ARG_PROJECT_POS]),
    ("searches", "list"):             (searches_list, [ARG_LIMIT100, ARG_OFFSET]),
```

Add to the docstring's `Read:` block:

```
  youtrack projects get ID                   One project: name, shortName,
                                             description, archived, leader.
  youtrack searches list [--limit N] [--offset N]
                                             Saved issue searches visible to
                                             the token, with their queries.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "projects_get or searches_list" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
Expected: zero failures.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): projects get and searches list

Closes get_project and get_saved_issue_searches.

projects get returns a single object rather than the paged envelope, matching
issues get and articles get — it is a single-entity read. searches list pages
like every other list verb."
```

---

### Task 3: `articles search` and `articles update --parent`

Both articles, so they share a task. Implements **E5** and **E6**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — docstring, `articles_create` (extract helper), `articles_update`, new `articles_search`, `COMMANDS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params`, `_paged`, `_article_ref`, `ARTICLE_LIST_FIELDS`, `ARTICLE_FIELDS`, `_read_text`.
- Produces: `articles_search(c, a)`; `_parent_article_id(c, parent_ref: str, project_id: str) -> str` extracted from `articles_create`.

- [ ] **Step 1: Write the failing tests**

```python
def test_articles_search_sends_the_query_to_the_articles_endpoint(tmp_path):
    rows = [{"idReadable": "IONDEV-A-36", "summary": "DWH (TimeScale)"}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "articles", "search", "summary: DWH",
                         "--limit", "3")
    assert result.returncode == 0, result.stderr
    parsed = urllib.parse.urlparse(handler.requests[0])
    # Measured: articles search is the same endpoint as articles list, plus query.
    assert parsed.path == "/api/articles"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["query"] == ["summary: DWH"]
    assert query["$top"] == ["4"]
    payload = json.loads(result.stdout)
    assert payload["items"][0]["idReadable"] == "IONDEV-A-36"
    assert payload["has_more"] is False


def test_articles_update_reparents(tmp_path):
    class ReparentHandler(BaseHTTPRequestHandler):
        requests = []

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
            route = urllib.parse.urlparse(self.path).path
            if route.endswith("/A-2"):
                self._reply({"id": "5-2", "idReadable": "A-2",
                             "project": {"id": "0-1"}})
            else:
                self._reply({"id": "5-1", "idReadable": "A-1",
                             "project": {"id": "0-1"}})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, json.loads(raw)))
            self._reply({"id": "5-1", "idReadable": "A-1"})

    ReparentHandler.requests = []
    with serve(ReparentHandler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "A-2")
    assert result.returncode == 0, result.stderr
    post = [r for r in ReparentHandler.requests if r[0] == "POST"][0]
    assert post[2] == {"parentArticle": {"id": "5-2"}}


def test_articles_update_refuses_a_cross_project_parent(tmp_path):
    class CrossProjectHandler(BaseHTTPRequestHandler):
        requests = []

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
            route = urllib.parse.urlparse(self.path).path
            if route.endswith("/A-2"):
                self._reply({"id": "5-2", "idReadable": "A-2",
                             "project": {"id": "0-9"}})   # different project
            else:
                self._reply({"id": "5-1", "idReadable": "A-1",
                             "project": {"id": "0-1"}})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, None))
            self._reply({})

    CrossProjectHandler.requests = []
    with serve(CrossProjectHandler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "A-2")
    assert result.returncode == 6
    # Same pre-check articles create already performs — must not reach the write.
    assert not any(r[0] == "POST" for r in CrossProjectHandler.requests)


def test_articles_update_still_requires_something_to_change(tmp_path):
    with serve(Handler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1")
    assert result.returncode == 6
    assert "summary" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "articles_search or articles_update_reparents or cross_project or something_to_change" -v`
Expected: FAIL — `articles search` is unknown to argparse; `articles update --parent` is an unrecognised flag.

- [ ] **Step 3: Extract the parent resolver from `articles_create`**

Replace the inline parent block in `articles_create` with a call to a new helper, so both verbs share one behaviour (**E6**). Read `articles_create` first and preserve its exact checks:

```python
def _parent_article_id(c, parent_ref: str, project_id: str) -> str:
    """Resolve a parent article reference to its id, refusing a cross-project one.

    Shared by `articles create` and `articles update` so re-parenting cannot
    develop behaviour that differs from parenting at creation.
    """
    parent = _request(c, "GET", f"/articles/{_article_ref(parent_ref)}",
                      params={"fields": "id,idReadable,project(id)"})
    parent_id = parent.get("id") if isinstance(parent, dict) else None
    parent_project = ((parent.get("project") or {}).get("id")
                      if isinstance(parent, dict) else None)
    if not parent_id or not parent_project:
        _die(5, "bad_response",
             "YouTrack returned a parent article without id/project")
    if parent_project != project_id:
        _die(6, "input",
             f"--parent belongs to project {parent_project!r}, not {project_id!r}")
    return parent_id
```

In `articles_create`, the parent branch becomes:

```python
    if a.parent:
        body["parentArticle"] = {"id": _parent_article_id(c, a.parent, a.project)}
```

- [ ] **Step 4: Add `articles search` and `--parent` on update**

```python
def articles_search(c, a):
    params = _page_params(a, ARTICLE_LIST_FIELDS)
    params["query"] = a.query
    return _paged(_request(c, "GET", "/articles", params=params), a.limit)
```

In `articles_update`, add the parent handling. It needs the target's own project to run the same-project check, which costs one GET:

```python
def articles_update(c, a):
    summary = a.summary if (a.summary is not None and a.summary.strip()) else None
    content = _read_text(a.content) if a.content is not None else None
    if content is not None and not content.strip():
        content = None
    if summary is None and content is None and not a.parent:
        _die(6, "input",
             "articles update needs a non-empty --summary, --content or --parent")
    body = {}
    if summary is not None:
        body["summary"] = summary
    if content is not None:
        body["content"] = content
    if a.parent:
        # The same-project rule is enforced against *this* article's project, so
        # re-parenting needs one read of the target before the write.
        target = _request(c, "GET", f"/articles/{_article_ref(a.article)}",
                          params={"fields": "id,project(id)"})
        project_id = ((target.get("project") or {}).get("id")
                      if isinstance(target, dict) else None)
        if not project_id:
            _die(5, "bad_response", "YouTrack returned an article without a project")
        body["parentArticle"] = {"id": _parent_article_id(c, a.parent, project_id)}
    return _request(c, "POST", f"/articles/{_article_ref(a.article)}",
                    params={"fields": ARTICLE_FIELDS}, json=body)
```

Add the `COMMANDS` row for search, and `ARG_PARENT` to the update row:

```python
    ("articles", "search"):           (articles_search, [ARG_QUERY, ARG_LIMIT100, ARG_OFFSET]),
    ("articles", "update"):           (articles_update, [ARG_ARTICLE, ARG_SUMMARY_OPT,
                                                         ARG_CONTENT, ARG_PARENT]),
```

> Read the existing `("articles", "update")` row before editing and keep its current argspecs, adding `ARG_PARENT` to them.

Docstring: add `articles search`, and update the `articles update` line to show `[--parent ID|URL]`:

```
  youtrack articles search QUERY [--limit N] [--offset N]
                                             Full-text / query-language search
                                             over articles. Same endpoint as
                                             `articles list`, with a query.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "articles" -v`
Expected: PASS, including the pre-existing article tests — the `articles_create` refactor must not change its behaviour.

- [ ] **Step 6: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
Expected: zero failures. If a pre-existing `articles create --parent` test fails, the extraction changed behaviour — fix the helper, do not weaken the test.

- [ ] **Step 7: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): articles search, and articles update --parent

Closes search_articles and brings update_article to parity.

articles search is GET /articles?query= — the same endpoint articles list
already uses (measured), so it inherits that projection and paging rather than
introducing its own.

Re-parenting reuses create's parent resolution, extracted to a shared helper
including its cross-project refusal, so re-parenting cannot drift from
parenting at creation. That check runs against the target article's own
project, which costs update one read before the write."
```

---

### Task 4: comment visibility

Depends on Task 1. Implements **E1** and **E2**.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — docstring, `COMMENT_FIELDS`, visibility helpers beside `issues_comments_add`, `COMMANDS`
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `GROUP_FIELDS` and the `/groups` endpoint (Task 1), `difflib`, `_request`, `_die`.
- Produces: `_resolve_group_ids(c, names: list[str]) -> list[dict]`; `_comment_visibility(c, users: list[str], groups: list[str]) -> dict | None`; `ARG_PERMITTED_USERS`, `ARG_PERMITTED_GROUPS`.

- [ ] **Step 1: Write the failing tests**

```python
def _visibility_handler(groups):
    """Serves /groups for name->id resolution, records the comment POST."""
    class VisibilityHandler(BaseHTTPRequestHandler):
        requests = []

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
            self._reply(groups)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, json.loads(raw)))
            self._reply({"id": "4-1", "text": "hi"})

    VisibilityHandler.requests = []
    return VisibilityHandler


_GROUPS = [{"id": "3-4", "name": "Administrative Team"},
           {"id": "3-9", "name": "ION Team"}]


def test_comment_permitted_groups_are_sent_as_ids(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-groups", "ION Team")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    # Measured: permittedGroups rejects a name; only an id works.
    assert post[2]["visibility"] == {
        "$type": "LimitedVisibility",
        "permittedGroups": [{"id": "3-9"}],
    }


def test_comment_permitted_users_are_sent_as_logins(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-users", "s.royz")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert post[2]["visibility"] == {
        "$type": "LimitedVisibility",
        "permittedUsers": [{"login": "s.royz"}],
    }
    # No group lookup is needed when no group was named.
    assert not any(r[0] == "GET" for r in handler.requests)


def test_comment_visibility_always_carries_the_type(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-users", "s.royz",
                         "--permitted-groups", "ION Team")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    # Measured: omitting $type returns 400 with a type mismatch.
    assert post[2]["visibility"]["$type"] == "LimitedVisibility"
    assert post[2]["visibility"]["permittedUsers"] == [{"login": "s.royz"}]
    assert post[2]["visibility"]["permittedGroups"] == [{"id": "3-9"}]


def test_unknown_group_name_exits_6_with_near_miss_and_no_write(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-groups", "ION Teem")
    assert result.returncode == 6
    assert "ION Team" in result.stderr, "must offer the near-miss"
    # The server cannot distinguish a bad name from a name-instead-of-id, so the
    # refusal must happen client-side, before any write.
    assert not any(r[0] == "POST" for r in handler.requests)


def test_comment_without_visibility_flags_sends_no_visibility_key(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert "visibility" not in post[2]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "permitted or visibility" -v`
Expected: FAIL — `--permitted-groups` / `--permitted-users` are unrecognised flags, argparse exits 2.

- [ ] **Step 3: Write the implementation**

Extend `COMMENT_FIELDS` so a restricted comment is visible on read (**E8**):

```python
COMMENT_FIELDS = ("id,text,created,updated,author(id,login,fullName),"
                  "visibility($type,permittedUsers(login),permittedGroups(id,name))")
```

Add beside `issues_comments_add`:

```python
def _resolve_group_ids(c, names: list) -> list:
    """Group names -> `{"id": …}` entries.

    Measured: `permittedGroups` requires the group id and rejects a name, and a
    bogus name returns the same 400 as a name-instead-of-id — so the server
    cannot tell the caller which mistake they made. Resolving here is the only
    way to say "no such group" precisely.
    """
    rows = _request(c, "GET", "/groups",
                    params={"fields": GROUP_FIELDS, "$top": 1000})
    by_name = {(r.get("name") or "").casefold(): r
               for r in (rows if isinstance(rows, list) else [])
               if isinstance(r, dict)}
    resolved = []
    for name in names:
        entry = by_name.get(name.strip().casefold())
        if entry is None or not entry.get("id"):
            known = sorted(r["name"] for r in by_name.values() if r.get("name"))
            near = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
            hint = (f"did you mean: {', '.join(near)}?" if near
                    else f"groups visible to this token: {', '.join(known)}")
            _die(6, "unknown_group", f"no group named {name!r}", hint)
        resolved.append({"id": entry["id"]})
    return resolved


def _comment_visibility(c, users: list, groups: list):
    """The `visibility` object, or None when the caller restricted nothing.

    `$type` is mandatory — omitting it returns 400 with a type mismatch
    (measured), exactly as custom-field entries do.
    """
    if not users and not groups:
        return None
    visibility = {"$type": "LimitedVisibility"}
    if users:
        visibility["permittedUsers"] = [{"login": u.strip()} for u in users]
    if groups:
        visibility["permittedGroups"] = _resolve_group_ids(c, groups)
    return visibility
```

Change `issues_comments_add`:

```python
def issues_comments_add(c, a):
    text = _read_text(a.text if a.text is not None else "-")
    if not text.strip():
        _die(6, "input", "comment text cannot be empty")
    body = {"text": text}
    visibility = _comment_visibility(c, a.permitted_users, a.permitted_groups)
    if visibility is not None:
        body["visibility"] = visibility
    return _request(c, "POST", f"/issues/{_issue_ref(a.issue)}/comments",
                    params={"fields": COMMENT_FIELDS}, json=body)
```

Add the arg specs and extend the `COMMANDS` row:

```python
ARG_PERMITTED_USERS  = (("--permitted-users",), {"action": "append", "default": [],
                                                 "metavar": "LOGIN"})
ARG_PERMITTED_GROUPS = (("--permitted-groups",), {"action": "append", "default": [],
                                                  "metavar": "NAME"})
```

```python
    ("issues", "comments", "add"):    (issues_comments_add, [ARG_ISSUE, ARG_TEXT,
                                                             ARG_PERMITTED_USERS,
                                                             ARG_PERMITTED_GROUPS]),
```

Docstring — update the `issues comments add` entry:

```
  youtrack issues comments add ISSUE [--text TEXT|-]
        [--permitted-users LOGIN]…           Restrict who can see the comment.
        [--permitted-groups NAME]…           Users are named by login, groups by
                                             name — the name is resolved to an
                                             id here because YouTrack's
                                             permittedGroups rejects names. An
                                             unknown group exits 6 before any
                                             write. Omit both flags and the
                                             comment is unrestricted.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k "permitted or visibility" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
Expected: zero failures. Pre-existing comment tests may assert on the response projection; `COMMENT_FIELDS` grew, so check them and preserve their intent if they need updating.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack): comment visibility on issues comments add

Brings add_issue_comment to parity. --permitted-users names logins;
--permitted-groups names groups and resolves them to ids, because YouTrack's
permittedGroups requires an id and rejects a name (measured).

An unknown group exits 6 with a near-miss before any write, since the server
returns the same 400 for a bogus name as for a name-instead-of-id and so cannot
tell the caller which mistake they made.

visibility always carries an explicit \$type: omitting it returns a type
mismatch, the same trap custom fields have. COMMENT_FIELDS now reads visibility
back, additively."
```

---

### Task 5: `projects find` paging

The last verb that truncates silently.

**Files:**
- Modify: `capabilities/youtrack/bin/youtrack` — `projects_find`, `COMMANDS`, docstring
- Test: `capabilities/youtrack/tests/test_youtrack.py`

**Interfaces:**
- Consumes: `_page_params`, `_paged`.
- Produces: `projects find` returning the envelope.

- [ ] **Step 1: Write the failing test**

```python
def test_projects_find_pages_and_envelopes(tmp_path):
    rows = [{"id": "0-1", "name": "ION", "shortName": "ION"},
            {"id": "0-6", "name": "ION Development", "shortName": "IONDEV"}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "projects", "find", "ION",
                         "--limit", "2", "--offset", "1")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert query["$top"] == ["3"]        # limit + 1, replacing the hardcoded 100
    assert query["$skip"] == ["1"]
    assert query["query"] == ["ION"]
    payload = json.loads(result.stdout)
    assert payload["items"][0]["shortName"] == "ION"
    assert payload["has_more"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k projects_find_pages -v`
Expected: FAIL — `--limit`/`--offset` are unrecognised, argparse exits 2.

- [ ] **Step 3: Write the implementation**

```python
def projects_find(c, a):
    params = _page_params(a, PROJECT_FIELDS)
    if a.substring:
        params["query"] = a.substring
    return _paged(_request(c, "GET", "/admin/projects", params=params), a.limit)
```

```python
    ("projects", "find"):             (projects_find, [ARG_SUBSTRING_OPT, ARG_LIMIT100, ARG_OFFSET]),
```

Docstring: update the `projects find` line to `[--limit N] [--offset N]`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -k projects_find -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite in the foreground**

Run: `cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q 2>&1 | tail -20`
Expected: **pre-existing `projects find` tests will fail** — the verb now returns an envelope instead of a bare array. Update them to read `payload["items"]`, preserving their original intent. Do not weaken an assertion to make it pass.

- [ ] **Step 6: Commit**

```bash
git add capabilities/youtrack/bin/youtrack capabilities/youtrack/tests/test_youtrack.py
git commit -m "feat(youtrack)!: page projects find

The last list verb that truncated silently — it hardcoded \$top: 100 with no way
to page or to know the result was cut short. M3 fixed this everywhere else.

BREAKING CHANGE: projects find now returns {\"items\": [...], \"has_more\": bool}
instead of a bare JSON array, matching every other --limit verb."
```

---

### Task 6: Live verification against ION

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

- [ ] **Step 3: Exercise the read verbs**

```bash
cd /Users/zjor/projects/ion/agents
youtrack groups find Team --limit 3
youtrack projects get 0-1
youtrack searches list --limit 3
youtrack articles search "summary: DWH" --limit 3
youtrack projects find ION --limit 2
youtrack projects find ION --limit 2 --offset 1
```
Expected: envelopes with `has_more` on every list verb; `projects get` a bare object with `shortName: ION`; `articles search` returning a hit for `DWH`; the two `projects find` pages differing.

- [ ] **Step 4: Exercise `groups members` with a real id from Step 3**

Take a group id from `groups find` output and run `youtrack groups members <id> --limit 5`. Expected: a user list in an envelope.

- [ ] **Step 5: Exercise comment visibility on a throwaway ION issue**

```bash
cd /Users/zjor/projects/ion/agents
youtrack issues create --project 0-1 --summary "PROBE M4a verify (safe to delete)" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['idReadable'], d['project']['shortName'])"
```
**If `project.shortName` is not `ION`, stop and delete it.** Then, with that key as `$K`:

```bash
youtrack issues comments add $K --text "probe: group restricted" --permitted-groups "ION Team"
youtrack issues comments add $K --text "probe: unrestricted"
youtrack issues comments list $K --limit 5
youtrack issues comments add $K --text "nope" --permitted-groups "ION Teem"; echo "bad group exit=$?"
```
Expected: the restricted comment reads back with `visibility.$type == "LimitedVisibility"` and the group resolved; the unrestricted one `UnlimitedVisibility`; the typo exits **6** with a near-miss and writes nothing.

- [ ] **Step 6: Delete the probe issue and verify it is gone**

Use the API directly (the CLI has no delete verb), then confirm `GET` returns 404.

- [ ] **Step 7: Record the results in the parity plan**

Add an "M4a verified live" table under the M4a milestone, listing what was exercised and anything that could not be.

- [ ] **Step 8: Commit the verification note**

```bash
git add .claude/plans/2026-07-27-youtrack-mcp-parity.md
git commit -m "docs(youtrack): record M4a live verification against ION"
```

---

### Task 7: Catalog reindex, status markers, consumer refresh

The step M1 skipped, which left `main` uninstallable.

- [ ] **Step 1: Read the hashing algorithm from source, do not guess it**

Read `_payload_sha256` in `bin/capabilities`. It skips `meta.json`, `stub`, `manifest.json`, anything under `__pycache__`, and `.pyc`/`.pyo`/`.session*`, feeding `<relpath>\0<bytes>\0` per file in sorted order into one sha256.

- [ ] **Step 2: Recompute every entry and confirm the untouched ones reproduce**

Transcribe that function and run it over all catalog entries. **Every capability except `youtrack` must reproduce its committed hash byte-for-byte.** If an untouched one differs, the transcription has drifted — reread the source rather than adjusting the expected value.

- [ ] **Step 3: Reinstall from source before reading the summary**

```bash
capabilities install youtrack --from capabilities/youtrack/bin/youtrack
```
`youtrack manifest --json` reports the **installed** build, so reading the summary before reinstalling yields the previous one. (This bit M3's Task 8.)

- [ ] **Step 4: Consider whether `SUMMARY` needs updating**

M4a adds group lookup, saved searches, project reads, and article search. `SUMMARY` is what `capabilities list` shows and what ContextKit injects into a consuming agent's context — if it does not mention these, a consumer will not know they exist. Update it if so, **then** recompute the hash, since `SUMMARY` is part of the payload.

- [ ] **Step 5: Update youtrack's `payload_sha256` and `summary`, and write with the manager's conventions**

```python
json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
```
Expected diff: one or two changed lines, no reformatting noise.

- [ ] **Step 6: Update the parity plan's status markers**

Mark M4a ✅ in the status block and the milestone heading. In the parity table, `update_article`, `add_issue_comment`, `search_articles`, `get_project`, `get_saved_issue_searches`, `find_user_groups` and `get_user_group_members` all move to parity. Update the count line to **20 at parity, 0 near/partial, 2 absent (`manage_issue_tags`, `log_work`), plus 1 covered by design** — and verify it sums to 23.

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
git commit -m "chore(source): reindex youtrack catalog and mark M4a shipped"
```

---

## Verification

```bash
cd capabilities/youtrack && python3 -m pytest tests/test_youtrack.py -q
```

Expected: 151 pre-existing plus ~14 new, all passing. `test_every_command_is_documented` and `test_every_documented_command_exists` cover the five new verbs; `test_write_verbs_match_commands` covers the unchanged write set (M4a adds no new write verb — comment visibility extends one that is already in `WRITE_VERBS`).

## Rollback

Every task is one commit on `feat/youtrack-m4a-unblocked-tail`. To undo a task, `git revert <sha>`. To abandon M4a, `git checkout main && git branch -D feat/youtrack-m4a-unblocked-tail`; no installed artifact changes until Task 6 Step 1, and reinstalling from `upstream/main` restores the M3 build.

## Out of scope

M4b — `issues tags add`/`remove` and `issues work log`/`list` — which needs its own probe first. Also out: the 44-tool community server as a parity target, and the two evidence gaps blocked by the ION-only constraint (exit 7 for link workflow rejections, M2's `text`-on-create criterion).

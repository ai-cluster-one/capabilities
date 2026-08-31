import ast
import contextlib
import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "youtrack", CAPABILITY / "youtrack")
    if path.is_file()), CAPABILITY / "bin" / "youtrack")


def _source_tree():
    return ast.parse(CLI.read_text())


def _command_paths():
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "COMMANDS" for t in node.targets):
            return {tuple(ast.literal_eval(k)) for k in node.value.keys}
    raise AssertionError("COMMANDS table not found in bin/youtrack")


def test_every_command_is_documented():
    doc = ast.get_docstring(_source_tree())
    missing = [" ".join(p) for p in sorted(_command_paths())
               if f"youtrack {' '.join(p)}" not in doc]
    assert not missing, f"commands absent from help docstring: {missing}"


def _write_verbs():
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "WRITE_VERBS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("WRITE_VERBS not found in bin/youtrack")


def test_write_verbs_match_commands():
    assert _write_verbs() <= {" ".join(p) for p in _command_paths()}


def _issue_type_map():
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_ISSUE_TYPE_BY_FIELD_TYPE"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("_ISSUE_TYPE_BY_FIELD_TYPE not found in bin/youtrack")


def test_every_documented_command_exists():
    doc = ast.get_docstring(_source_tree())
    contract = {"help", "connections", "doctor", "stub", "manifest", "guide",
                "ids", "refs"}
    paths = _command_paths()
    documented = set()
    for line in doc.splitlines():
        if not line.startswith("  "):
            continue
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "youtrack" and parts[1] not in contract:
            for width in (3, 2):
                if tuple(parts[1:1 + width]) in paths:
                    documented.add(tuple(parts[1:1 + width]))
                    break
            else:
                documented.add(tuple(parts[1:3]))
    assert documented <= paths, f"documented but not in COMMANDS: {sorted(documented - paths)}"


class Handler(BaseHTTPRequestHandler):
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
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if self.path.startswith("/api/users/me"):
            self._reply({"id": "1-1", "login": "agent"})
        else:
            self._reply({"error": "missing"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.__class__.requests.append(("POST", self.path, self.headers, payload))
        if self.path.startswith("/api/issues/DEMO-1/comments"):
            self._reply({"id": "4-1", "text": payload["text"]})
        elif self.path.startswith("/api/issues"):
            self._reply({"id": "2-1", "idReadable": "DEMO-1", **payload})
        else:
            self._reply({"error": "missing"}, 404)


def run_cli(tmp_path, base_url, *args):
    config = tmp_path / "config"
    policy = config / "capabilities" / "settings.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": True}},
    }))
    registry = config / "youtrack" / "connections.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({
        "default": "test",
        "connections": {"test": {
            "base_url": base_url,
            "secret_env": "YOUTRACK_TOKEN",
            "allow_write": True,
        }},
    }))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "YOUTRACK_URL": base_url,
        "YOUTRACK_TOKEN": "perm:test",
    })
    return subprocess.run(
        [str(CLI), *args], cwd=tmp_path, env=env, text=True,
        capture_output=True, timeout=30,
    )


@contextlib.contextmanager
def serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


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


def test_users_me_smoke(tmp_path):
    class UsersMeHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps({"id": "1-1", "login": "agent"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    UsersMeHandler.requests = []
    with serve(UsersMeHandler) as base_url:
        result = run_cli(tmp_path, base_url, "users", "me")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["login"] == "agent"
    assert UsersMeHandler.requests[0].startswith("/api/users/me?")


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
    # The read projection must keep asking for visibility, or an unknown-field
    # silent drop server-side would regress this invisibly (measured: YouTrack
    # drops an unrecognised field from `fields=` rather than erroring).
    assert "visibility($type" in query["fields"][0]
    assert json.loads(result.stdout)["has_more"] is False


def test_offset_zero_is_omitted_from_the_request(tmp_path):
    handler = _paging_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert "$skip" not in query


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


def test_projects_find_smoke(tmp_path):
    class ProjectsHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps([
                {"id": "0-6", "name": "Demo", "shortName": "DEMO"},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ProjectsHandler.requests = []
    with serve(ProjectsHandler) as base_url:
        result = run_cli(tmp_path, base_url, "projects", "find", "Demo")

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["items"][0]["shortName"] == "DEMO"
    assert ProjectsHandler.requests[0].startswith("/api/admin/projects?")
    assert "query=Demo" in ProjectsHandler.requests[0]


def test_projects_find_pages_and_envelopes(tmp_path):
    rows = [{"id": "0-1", "name": "ION", "shortName": "ION"},
            {"id": "0-6", "name": "ION Development", "shortName": "IONDEV"}]
    handler = _paging_handler(rows)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "projects", "find", "ION",
                         "--limit", "2", "--offset", "1")
    assert result.returncode == 0, result.stderr
    assert urllib.parse.urlparse(handler.requests[0]).path == "/api/admin/projects"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert query["$top"] == ["3"]        # limit + 1, replacing the hardcoded 100
    assert query["$skip"] == ["1"]
    assert query["query"] == ["ION"]
    payload = json.loads(result.stdout)
    assert payload["items"][0]["shortName"] == "ION"
    assert payload["has_more"] is False


def test_projects_find_substring_is_optional(tmp_path):
    handler = _paging_handler([])
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "projects", "find")
    assert result.returncode == 0, result.stderr
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.requests[0]).query)
    assert "query" not in query


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
    assert urllib.parse.urlparse(handler.requests[0]).path == "/api/groups"
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
    assert urllib.parse.urlparse(handler.requests[0]).path == "/api/groups"
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


PROJECT_FIELDS_PAYLOAD = [
    {"id": "1", "canBeEmpty": False, "$type": "EnumProjectCustomField",
     "field": {"name": "Priority", "fieldType": {"id": "enum[1]", "isMultiValue": False}},
     "bundle": {"id": "b1", "values": [{"name": "Critical"}, {"name": "Normal"}]}},
    {"id": "2", "canBeEmpty": True, "$type": "UserProjectCustomField",
     "field": {"name": "Assignee", "fieldType": {"id": "user[1]", "isMultiValue": False}},
     "bundle": {"id": "b2", "values": [{"name": "Sergey Royz"}],
                "aggregatedUsers": [{"login": "s.royz"}, {"login": "j.howell"}]}},
    {"id": "3", "canBeEmpty": True, "$type": "SimpleProjectCustomField",
     "field": {"name": "Points", "fieldType": {"id": "integer", "isMultiValue": False}},
     "bundle": None},
]


class ProjectFieldsHandler(Handler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if "/customFields" in self.path:
            # Honors $top/$skip like a real backend would, so paging tests
            # against this fixture exercise genuine truncation/has_more
            # behaviour rather than always returning the fixed payload.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            top = int(query["$top"][0])
            skip = int(query.get("$skip", ["0"])[0])
            self._reply(PROJECT_FIELDS_PAYLOAD[skip:skip + top])
        else:
            self._reply({"error": "missing"}, 404)


def test_projects_fields_list_shapes_schema(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"] == [
        {"name": "Priority", "type": "enum[1]", "multiValue": False,
         "canBeEmpty": False, "values": ["Critical", "Normal"]},
        {"name": "Assignee", "type": "user[1]", "multiValue": False,
         "canBeEmpty": True, "values": ["s.royz", "j.howell"]},
        {"name": "Points", "type": "integer", "multiValue": False,
         "canBeEmpty": True, "values": None},
    ]


def test_projects_fields_report_can_be_empty_not_required(tmp_path):
    """Measured: canBeEmpty=false does not make a field required at create —
    the server defaults it. Reporting `required` said the opposite, so the key
    is YouTrack's own flag and no `required` key is emitted at all."""
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        listed = run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
        got = run_cli(tmp_path, base, "projects", "fields", "get", "0-6", "Priority")
    assert listed.returncode == 0, listed.stderr
    assert got.returncode == 0, got.stderr
    for row in json.loads(listed.stdout)["items"]:
        assert "required" not in row, "the misnamed `required` key must be gone"
        assert isinstance(row["canBeEmpty"], bool)
    one = json.loads(got.stdout)
    assert one["canBeEmpty"] is False and "required" not in one


def test_projects_fields_get_is_case_insensitive(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "get", "0-6", "priority")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "Priority"


def test_projects_fields_get_unknown_field_exits_3(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "get", "0-6", "Nope")
    assert result.returncode == 3
    assert "Priority" in result.stderr


def test_projects_fields_list_requests_project_field_fields(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
    path = [r[1] for r in ProjectFieldsHandler.requests if r[0] == "GET"][0]
    assert "aggregatedUsers" in path
    assert "canBeEmpty" in path
    assert "isMultiValue" in path
    assert "bundle(" in path or "bundle%28" in path


NAMELESS_BUNDLE_ENTRIES_PAYLOAD = [
    {"id": "1", "canBeEmpty": False, "$type": "EnumProjectCustomField",
     "field": {"name": "Priority", "fieldType": {"id": "enum[1]", "isMultiValue": False}},
     "bundle": {"id": "b1", "values": [{"id": "nameless"}, {"name": "Critical"}]}},
    {"id": "2", "canBeEmpty": True, "$type": "UserProjectCustomField",
     "field": {"name": "Assignee", "fieldType": {"id": "user[1]", "isMultiValue": False}},
     "bundle": {"id": "b2",
                "aggregatedUsers": [{"id": "nameless-user"}, {"login": "s.royz"}]}},
]


class NamelessBundleHandler(Handler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if "/customFields" in self.path:
            self._reply(NAMELESS_BUNDLE_ENTRIES_PAYLOAD)
        else:
            self._reply({"error": "missing"}, 404)


def test_projects_fields_list_drops_nameless_bundle_entries(tmp_path):
    NamelessBundleHandler.requests = []
    with serve(NamelessBundleHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)["items"]
    assert parsed[0]["values"] == ["Critical"]
    assert None not in parsed[0]["values"]
    assert parsed[1]["values"] == ["s.royz"]
    assert None not in parsed[1]["values"]


class MalformedProjectFieldsHandler(Handler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if "/customFields" in self.path:
            self._reply([
                {"id": "1", "canBeEmpty": False, "$type": "EnumProjectCustomField",
                 "field": {"name": "Priority",
                          "fieldType": {"id": "enum[1]", "isMultiValue": False}},
                 "bundle": {"id": "b1", "values": [{"name": "Critical"}]}},
                "not-a-dict",
                None,
            ])
        else:
            self._reply({"error": "missing"}, 404)


def test_projects_fields_list_skips_non_dict_entries(tmp_path):
    MalformedProjectFieldsHandler.requests = []
    with serve(MalformedProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)["items"]
    assert parsed == [
        {"name": "Priority", "type": "enum[1]", "multiValue": False,
         "canBeEmpty": False, "values": ["Critical"]},
    ]


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


def test_issues_get_smoke(tmp_path):
    class IssueGetHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps({"id": "1-1", "idReadable": "DEMO-1",
                               "summary": "First issue"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    IssueGetHandler.requests = []
    with serve(IssueGetHandler) as base_url:
        result = run_cli(tmp_path, base_url, "issues", "get", "DEMO-1")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["idReadable"] == "DEMO-1"
    assert IssueGetHandler.requests[0].startswith("/api/issues/DEMO-1?")


def test_issues_comments_list_smoke(tmp_path):
    class IssueCommentsHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps([{"id": "4-1", "text": "A note"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    IssueCommentsHandler.requests = []
    with serve(IssueCommentsHandler) as base_url:
        result = run_cli(tmp_path, base_url, "issues", "comments", "list", "DEMO-1", "--limit", "5")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"][0]["text"] == "A note"
    assert IssueCommentsHandler.requests[0].startswith("/api/issues/DEMO-1/comments?")
    # $top is limit + 1 (6), not limit (5): the extra row makes truncation detectable.
    assert ("$top=6" in IssueCommentsHandler.requests[0]
            or "%24top=6" in IssueCommentsHandler.requests[0])


def test_create_and_comment_payloads(tmp_path):
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        created = run_cli(tmp_path, base_url, "issues", "create", "--project", "0-0",
                          "--summary", "First issue", "--description", "Body")
        commented = run_cli(tmp_path, base_url, "issues", "comments", "add", "DEMO-1",
                            "--text", "A note")
    finally:
        server.shutdown()
        thread.join()

    assert created.returncode == 0, created.stderr
    assert commented.returncode == 0, commented.stderr
    posts = [row for row in Handler.requests if row[0] == "POST"]
    assert posts[0][3] == {
        "project": {"id": "0-0"}, "summary": "First issue", "description": "Body"
    }
    assert posts[1][3] == {"text": "A note"}
    assert posts[0][2]["Authorization"] == "Bearer perm:test"


class CreateWithFieldsHandler(Handler):
    requests = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.__class__.requests.append(("POST", self.path, self.headers, payload))
        if self.path.startswith("/api/issues"):
            self._reply({"id": "2-1", "idReadable": "DEMO-1", **payload,
                        "customFields": [
                            {"name": "State", "$type": "StateIssueCustomField",
                             "value": {"name": "Open"}}]})
        else:
            self._reply({"error": "missing"}, 404)


def test_issues_create_flattens_custom_fields(tmp_path):
    CreateWithFieldsHandler.requests = []
    with serve(CreateWithFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "create", "--project", "0-0",
                         "--summary", "First issue")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "customFields" not in body
    assert body["fields"] == {"State": "Open"}


def test_read_only_connection_refuses_create_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "create",
                     "--project", "0-0", "--summary", "blocked")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_issues_requires_query(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "search")
    assert result.returncode == 2
    assert "required: query" in result.stderr.lower()


def test_issues_limit_must_be_positive(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "search", "state:Open", "--limit", "0")
    assert result.returncode == 6
    assert "positive" in result.stderr


def test_issues_http_request_and_parsing(tmp_path):
    class IssuesHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(("GET", self.path, self.headers))
            if self.path.startswith("/api/issues?"):
                body = json.dumps([
                    {"id": "1-1", "idReadable": "DEMO-1", "summary": "First",
                     "description": "Body", "customFields": [
                        {"name": "State", "value": {"name": "Open"}}]},
                    {"id": "1-2", "idReadable": "DEMO-2", "summary": "Second",
                     "description": None, "customFields": [
                        {"name": "State", "value": {"name": "In Progress"}}]},
                ]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    IssuesHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), IssuesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "issues", "search", "state:Open", "--limit", "10")
        default_result = run_cli(tmp_path, base_url, "issues", "search", "state:Open")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert default_result.returncode == 0, default_result.stderr
    parsed = json.loads(result.stdout)["items"]
    assert len(parsed) == 2
    assert parsed[0]["idReadable"] == "DEMO-1"
    assert parsed[0]["fields"]["State"] == "Open"
    assert "customFields" not in parsed[0]
    assert parsed[1]["idReadable"] == "DEMO-2"
    assert parsed[1]["fields"]["State"] == "In Progress"
    assert "query=state%3AOpen" in IssuesHandler.requests[0][1]
    # $top is limit + 1 (11), not limit (10): the extra row makes truncation detectable.
    assert "$top=11" in IssuesHandler.requests[0][1] or "%24top=11" in IssuesHandler.requests[0][1]
    assert "customFields=State" not in IssuesHandler.requests[0][1]
    # default --limit is 100, so $top is 101 for the same reason.
    assert "$top=101" in IssuesHandler.requests[1][1] or "%24top=101" in IssuesHandler.requests[1][1]


ISSUE_WITH_FIELDS = {
    "id": "2-1", "idReadable": "DEMO-1", "summary": "s", "description": "d",
    "customFields": [
        {"name": "Assignee", "$type": "SingleUserIssueCustomField",
         "value": {"login": "s.royz", "name": "Sergey Royz", "id": "1-1"}},
        {"name": "State", "$type": "StateIssueCustomField",
         "value": {"name": "Done", "id": "126-37"}},
        {"name": "Points", "$type": "SimpleIssueCustomField", "value": 2},
        {"name": "Original Estimate", "$type": "PeriodIssueCustomField",
         "value": {"presentation": "1d", "minutes": 480, "id": "P1D"}},
        {"name": "Acceptance Criteria", "$type": "TextIssueCustomField",
         "value": {"id": "text", "text": "- one\n- two"}},
        {"name": "Work Category", "$type": "MultiEnumIssueCustomField",
         "value": [{"name": "Infrastructure", "id": "124-49"},
                   {"name": "Technical Debt", "id": "124-50"}]},
        {"name": "Requestor", "$type": "MultiUserIssueCustomField",
         "value": [{"login": "k.shmidt", "name": "Kirill Shmidt", "id": "1-9"}]},
        {"name": "Due Date", "$type": "DateIssueCustomField", "value": 1781534028493},
        {"name": "Incident Start Time", "$type": "SimpleIssueCustomField",
         "value": 1781534028493},
        {"name": "Blocked Reason", "$type": "SingleEnumIssueCustomField", "value": None},
    ],
}


class CustomFieldsHandler(Handler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if self.path.startswith("/api/issues/"):
            self._reply(ISSUE_WITH_FIELDS)
        elif self.path.startswith("/api/issues"):
            self._reply([ISSUE_WITH_FIELDS])
        else:
            self._reply({"error": "missing"}, 404)


def test_issues_get_flattens_custom_fields(tmp_path):
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "customFields" not in body
    assert body["fields"] == {
        "Assignee": "s.royz",
        "State": "Done",
        "Points": 2,
        "Original Estimate": "1d",
        "Acceptance Criteria": "- one\n- two",
        "Work Category": ["Infrastructure", "Technical Debt"],
        "Requestor": ["k.shmidt"],
        "Due Date": "2026-06-15",
        "Incident Start Time": 1781534028493,
        "Blocked Reason": None,
    }


def test_issues_get_renders_date_fields_as_calendar_dates(tmp_path):
    """A `date` field is a calendar date; epoch ms is not a usable interface,
    and the server snaps the value to noon UTC anyway."""
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["fields"]["Due Date"] == "2026-06-15"


def test_issues_get_keeps_date_and_time_as_epoch_ms(tmp_path):
    """`date and time` is SimpleIssueCustomField and is not normalized, so it
    keeps its precision rather than being truncated to a day."""
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["fields"]["Incident Start Time"] == 1781534028493


def test_issues_get_requests_custom_fields(tmp_path):
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        run_cli(tmp_path, base, "issues", "get", "DEMO-1")
    path = [r[1] for r in CustomFieldsHandler.requests if r[0] == "GET"][0]
    assert "customFields(" in path or "customFields%28" in path
    assert "presentation" in path and "login" in path and "text" in path
    assert "minutes" in path
    assert "name" in path


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


def test_self_link_is_refused_without_any_request(tmp_path, link_handler):
    # Measured: the server returns 200 for a self-link and silently creates
    # nothing. Asserting the exit code alone would pass for a client that sent
    # it, so assert that no write left the process. The refusal happens
    # before the direction phrase is even resolved, so no request of any
    # kind — not just no POST — should be issued; asserting only "no POST"
    # would still pass a refactor that issued a GET first.
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-1", "--type", "relates to")
    assert result.returncode == 6
    assert "itself" in result.stderr.lower()
    assert link_handler.requests == []


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


def test_links_remove_is_a_write_verb():
    # Mirrors test_links_add_is_a_write_verb. WRITE_VERBS <= COMMANDS only
    # catches a stray/typo'd entry; it cannot catch an omission, which would
    # silently disable the allow_write gate for this verb.
    assert "issues links remove" in _write_verbs()


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


def test_links_remove_translates_a_bad_target(tmp_path):
    # Mirrors test_links_add_translates_a_bad_target. Add's nonexistent-target
    # failure surfaces as a 400 on the write POST; remove's surfaces as a 404
    # on the target-resolution GET instead (measured asymmetry) — both must
    # produce the same specific "no issue named" message, not the generic
    # "resource not found".
    class BadTargetHandler(_LinkHandler):
        requests = []
        slots = None

        def do_GET(self):
            self.__class__.requests.append(("GET", self.path, None))
            route = urllib.parse.urlparse(self.path).path
            if route.endswith("/links"):
                self._reply(self.__class__.slots)
            else:
                self._reply({"error": "Not Found", "error_description":
                             "Entity with id DEMO-404 not found"}, 404)

    BadTargetHandler.requests = []
    BadTargetHandler.slots = _link_slots_with_ids()
    with serve(BadTargetHandler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "remove", "DEMO-1",
                         "--to", "DEMO-404", "--type", "relates to")
    assert result.returncode == 3
    assert "DEMO-404" in result.stderr


def test_links_remove_self_link_is_refused_without_any_request(tmp_path, link_handler):
    # Mirrors test_self_link_is_refused_without_any_request. `add` refuses a
    # self-link before any request; `remove` had no such guard and would spend
    # three HTTP calls (resolve direction, resolve target, DELETE) before
    # failing on the third. Assert it now fails before the first.
    with serve(link_handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "remove", "DEMO-1",
                         "--to", "DEMO-1", "--type", "relates to")
    assert result.returncode == 6
    assert "itself" in result.stderr.lower()
    assert link_handler.requests == []


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
    # F7: report by reading the issue back, not the raw POST echo — otherwise
    # add/remove is unobservable to the caller.
    assert json.loads(result.stdout)["idReadable"] == "DEMO-1"


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
    # F7: report by reading the issue back, not the empty DELETE reply.
    assert json.loads(result.stdout)["idReadable"] == "DEMO-1"


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


def test_bare_404_at_a_write_still_exits_3_via_the_generic_mapping(tmp_path):
    # Pins the property the `on_error`-before-hardcoded-404 reorder in
    # `_request` relies on: a verb that passes `on_error` must still exit 3 on
    # a plain 404 whose body matches none of that callback's conditions. Here
    # `issues links add`'s POST returns a 404 with a body `_link_target_error`
    # does not recognise (no "not found" text), so `on_error` returns without
    # dying and the hardcoded `if response.status_code == 404` a few lines
    # below must be the thing that produces exit 3.
    class Bare404Handler(_LinkHandler):
        requests = []
        slots = None

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(("POST", self.path, None))
            self._reply({"error": "Oops"}, 404)

    Bare404Handler.requests = []
    Bare404Handler.slots = _link_slots_with_ids()
    with serve(Bare404Handler) as base:
        result = run_cli(tmp_path, base, "issues", "links", "add", "DEMO-1",
                         "--to", "DEMO-9", "--type", "relates to")
    assert result.returncode == 3


def test_issues_search_emits_same_fields_shape(tmp_path):
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO")
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)["items"][0]
    assert row["fields"]["State"] == "Done"
    assert "customFields" not in row


ISSUE_WITH_MALFORMED_FIELDS = {
    "id": "2-2", "idReadable": "DEMO-2", "summary": "s", "description": "d",
    "customFields": [
        {"name": "State", "$type": "StateIssueCustomField",
         "value": {"name": "Open", "id": "126-1"}},
        {"$type": "SimpleIssueCustomField", "value": 2},
        "not-a-dict",
    ],
}


class MalformedFieldsHandler(Handler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, self.headers, None))
        if self.path.startswith("/api/issues/"):
            self._reply(ISSUE_WITH_MALFORMED_FIELDS)
        else:
            self._reply({"error": "missing"}, 404)


def test_issues_get_skips_nameless_custom_fields(tmp_path):
    MalformedFieldsHandler.requests = []
    with serve(MalformedFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "get", "DEMO-2")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "customFields" not in body
    assert body["fields"] == {"State": "Open"}


def test_update_with_nothing_to_change_exits_6_before_network(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "update", "DEMO-1")
    assert result.returncode == 6
    assert "nothing to update" in result.stderr.lower()


def test_update_posts_to_the_issue_endpoint_with_auth(tmp_path):
    class AuthCheckingHandler(UpdateFieldsHandler):
        requests = []

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.__class__.requests.append(
                ("POST", self.path, json.loads(raw), self.headers["Authorization"]))
            self._reply({"idReadable": "DEMO-1"})

    AuthCheckingHandler.requests = []
    server, thread, base_url = _serve(AuthCheckingHandler)
    try:
        result = run_cli(tmp_path, base_url, "issues", "update", "DEMO-1",
                         "--field", "State=In Progress")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    writes = [r for r in AuthCheckingHandler.requests if r[0] == "POST"]
    assert len(writes) == 1
    _method, path, payload, auth = writes[0]
    assert path.startswith("/api/issues/DEMO-1")
    assert "/fields/State" not in path, "must not use the old /fields/State endpoint"
    assert payload["customFields"] == [
        {"name": "State", "$type": "StateIssueCustomField",
         "value": {"name": "In Progress"}}]
    assert auth == "Bearer perm:test"


def test_read_only_connection_refuses_update_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "update",
                     "DEMO-1", "--field", "State=Done")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_read_only_connection_refuses_issue_comment_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "comments", "add",
                     "DEMO-1", "--text", "blocked")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_update_handles_api_errors(tmp_path):
    result, _ = run_update(tmp_path, "DEMO-1", "--field", "State=Invalid",
                           status=400, body={"error": "Invalid state"})

    assert result.returncode == 6
    assert "invalid" in result.stderr.lower()


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


# ── issues update: custom-field writes (M2) ──────────────────────────────
#
# $type per field mirrors what IONDEV returns on a real issue read, including
# the measured traps: `date` is DateIssueCustomField while `date and time` is
# SimpleIssueCustomField, indistinguishable from `integer` on the issue side.
UPDATE_FIELD_TYPES = {
    "State": "StateIssueCustomField",
    "Priority": "SingleEnumIssueCustomField",
    "Work Category": "MultiEnumIssueCustomField",
    "Subsystem": "SingleOwnedIssueCustomField",
    "Assignee": "SingleUserIssueCustomField",
    "Requestor": "MultiUserIssueCustomField",
    "Sprints": "MultiVersionIssueCustomField",
    "Original Estimate": "PeriodIssueCustomField",
    "Points": "SimpleIssueCustomField",
    "Due Date": "DateIssueCustomField",
    "Acceptance Criteria": "TextIssueCustomField",
}


class UpdateFieldsHandler(BaseHTTPRequestHandler):
    """Serves the issue read that supplies $type, and records the write."""

    requests = []
    write_status = 200
    write_body = {"idReadable": "DEMO-1"}

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
        if self.path.startswith("/api/issues/DEMO-1"):
            self._reply({
                "idReadable": "DEMO-1",
                "customFields": [{"name": n, "$type": t, "value": None}
                                 for n, t in UPDATE_FIELD_TYPES.items()],
            })
        else:
            self._reply({"error": "missing"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.__class__.requests.append(("POST", self.path, payload))
        self._reply(self.__class__.write_body, self.__class__.write_status)


def run_update(tmp_path, *args, handler=UpdateFieldsHandler,
               status=200, body=None):
    handler.requests = []
    handler.write_status = status
    handler.write_body = body if body is not None else {"idReadable": "DEMO-1"}
    server, thread, base_url = _serve(handler)
    try:
        result = run_cli(tmp_path, base_url, "issues", "update", *args)
    finally:
        server.shutdown()
        thread.join()
    writes = [r for r in handler.requests if r[0] == "POST"]
    return result, writes


def sent_fields(writes):
    assert len(writes) == 1, f"expected exactly one write, got {len(writes)}"
    return {f["name"]: f for f in writes[0][2]["customFields"]}


def test_update_field_marshals_single_enum(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Priority=High")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Priority"] == {
        "name": "Priority",
        "$type": "SingleEnumIssueCustomField",
        "value": {"name": "High"},
    }


def test_update_marshals_state_by_name(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "State=In Progress")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["State"]["value"] == {"name": "In Progress"}


def test_update_marshals_owned_field_by_name(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Subsystem=Ingestion")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Subsystem"]["value"] == {"name": "Ingestion"}


def test_update_marshals_user_field_by_login(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Assignee=s.royz")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Assignee"]["value"] == {"login": "s.royz"}


def test_update_marshals_multi_user_as_login_list(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Requestor=s.royz")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Requestor"]["value"] == [{"login": "s.royz"}]


def test_update_marshals_multi_enum_as_name_list(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                               "--field", "Work Category=Infrastructure")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Work Category"]["value"] == [
        {"name": "Infrastructure"}]


def test_update_marshals_multi_version_as_name_list(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                               "--field", "Sprints=2026-26 Sprint")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Sprints"]["value"] == [{"name": "2026-26 Sprint"}]


def test_update_marshals_period_as_presentation(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                               "--field", "Original Estimate=1d 4h")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Original Estimate"]["value"] == {
        "presentation": "1d 4h"}


def test_update_marshals_text_field_as_text(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                               "--field", "Acceptance Criteria=- one\n- two")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Acceptance Criteria"]["value"] == {
        "text": "- one\n- two"}


def test_update_marshals_integer_as_json_number(tmp_path):
    """A quoted "3" from the shell must reach YouTrack as 3; it rejects "3"."""
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Points=3")

    assert result.returncode == 0, result.stderr
    value = sent_fields(writes)["Points"]["value"]
    assert value == 3 and isinstance(value, int)


def test_update_converts_calendar_date_to_noon_utc(tmp_path):
    """Measured: YouTrack snaps `date` to 12:00 UTC of the same UTC day."""
    result, writes = run_update(tmp_path, "DEMO-1",
                                "--field", "Due Date=2026-08-15")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Due Date"]["value"] == 1786795200000


def test_update_empty_value_clears_field_to_null(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Assignee=")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Assignee"]["value"] is None


def test_update_field_splits_on_first_equals_only(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                                "--field", "Acceptance Criteria=a=b")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Acceptance Criteria"]["value"] == {"text": "a=b"}


def test_update_field_without_equals_exits_6(tmp_path):
    result, _ = run_update(tmp_path, "DEMO-1", "--field", "Priority")

    assert result.returncode == 6
    assert "name=value" in result.stderr.lower()


def test_update_matches_field_names_case_insensitively(tmp_path):
    # Deliberately neither the canonical casing nor its casefold, so a lookup
    # that skips casefolding the caller's input cannot pass by accident.
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "PRIORITY=High")

    assert result.returncode == 0, result.stderr
    # The canonical name from the schema is sent, not the caller's casing.
    assert sent_fields(writes)["Priority"]["name"] == "Priority"


def test_update_repeated_field_accumulates_for_multi_value(tmp_path):
    result, writes = run_update(
        tmp_path, "DEMO-1",
        "--field", "Work Category=Infrastructure",
        "--field", "Work Category=Technical Debt")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Work Category"]["value"] == [
        {"name": "Infrastructure"}, {"name": "Technical Debt"}]


def test_update_clearing_multi_value_field_sends_empty_list(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Work Category=")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Work Category"]["value"] == []


def test_update_fields_accepts_inline_json_object(tmp_path):
    result, writes = run_update(
        tmp_path, "DEMO-1",
        "--fields", json.dumps({"Priority": "High", "Points": 3}))

    assert result.returncode == 0, result.stderr
    sent = sent_fields(writes)
    assert sent["Priority"]["value"] == {"name": "High"}
    assert sent["Points"]["value"] == 3


def test_update_fields_json_preserves_declared_types(tmp_path):
    """JSON carries real types, so a numeric string stays a string."""
    result, writes = run_update(
        tmp_path, "DEMO-1",
        "--fields", json.dumps({"Acceptance Criteria": "42"}))

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Acceptance Criteria"]["value"] == {"text": "42"}


def test_update_fields_accepts_a_json_list_for_multi_value(tmp_path):
    result, writes = run_update(
        tmp_path, "DEMO-1",
        "--fields", json.dumps({"Work Category": ["Infrastructure", "Technical Debt"]}))

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Work Category"]["value"] == [
        {"name": "Infrastructure"}, {"name": "Technical Debt"}]


def test_update_field_overrides_fields_document(tmp_path):
    """--fields applies first; an explicit --field patches one value."""
    result, writes = run_update(
        tmp_path, "DEMO-1",
        "--fields", json.dumps({"Priority": "Low", "Points": 1}),
        "--field", "Priority=Critical")

    assert result.returncode == 0, result.stderr
    sent = sent_fields(writes)
    assert sent["Priority"]["value"] == {"name": "Critical"}
    assert sent["Points"]["value"] == 1


def test_update_fields_rejects_non_object_json(tmp_path):
    result, _ = run_update(tmp_path, "DEMO-1", "--fields", json.dumps(["Priority"]))

    assert result.returncode == 6
    assert "json object" in result.stderr.lower()


def test_update_fields_rejects_malformed_json(tmp_path):
    result, _ = run_update(tmp_path, "DEMO-1", "--fields", "{not json")

    assert result.returncode == 6
    assert "not valid json" in result.stderr.lower()


def test_update_sends_summary_and_description(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--summary", "new title",
                                "--description", "new body")

    assert result.returncode == 0, result.stderr
    payload = writes[0][2]
    assert payload["summary"] == "new title"
    assert payload["description"] == "new body"
    assert "customFields" not in payload


def test_update_summary_only_skips_the_issue_read(tmp_path):
    """No custom fields named means no $type is needed, so no extra GET."""
    result, _ = run_update(tmp_path, "DEMO-1", "--summary", "just a rename")

    assert result.returncode == 0, result.stderr
    reads = [r for r in UpdateFieldsHandler.requests if r[0] == "GET"]
    assert reads == [], f"expected no issue read, got {reads}"


def test_update_bad_date_format_exits_6_before_writing(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1",
                                "--field", "Due Date=15/08/2026")

    assert result.returncode == 6
    assert "yyyy-mm-dd" in result.stderr.lower()
    assert writes == [], "must not write when a value cannot be marshalled"


# ── issues update: error translation (option C) ──────────────────────────
#
# The server validates and rolls a bad batch back on its own (measured), so the
# project schema is fetched only on the failure path, to turn YouTrack's
# unusable messages into named fields and allowed values.
UPDATE_PROJECT_SCHEMA = [
    {"id": "1", "canBeEmpty": True, "$type": "EnumProjectCustomField",
     "field": {"name": "Priority",
               "fieldType": {"id": "enum[1]", "isMultiValue": False}},
     "bundle": {"id": "b1", "values": [{"name": "Critical"}, {"name": "High"},
                                       {"name": "Normal"}, {"name": "Low"}]}},
    {"id": "2", "canBeEmpty": True, "$type": "EnumProjectCustomField",
     "field": {"name": "Severity",
               "fieldType": {"id": "enum[1]", "isMultiValue": False}},
     "bundle": {"id": "b2", "values": [{"name": "Severity 1"}]}},
]


class UpdateErrorHandler(UpdateFieldsHandler):
    """Serves the issue read, the project schema, and a failing write."""

    requests = []
    write_status = 400
    write_body = {}

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, None))
        if "/admin/projects/" in self.path and "/customFields" in self.path:
            # Honors $top/$skip like a real backend would, so a mutation that
            # pages this error-translation re-read is observable.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            top = int(query["$top"][0]) if "$top" in query else len(UPDATE_PROJECT_SCHEMA)
            skip = int(query.get("$skip", ["0"])[0])
            self._reply(UPDATE_PROJECT_SCHEMA[skip:skip + top])
        elif self.path.startswith("/api/issues/DEMO-1"):
            self._reply({
                "idReadable": "DEMO-1",
                "project": {"id": "0-6", "shortName": "DEMO"},
                "customFields": [{"name": n, "$type": t, "value": None}
                                 for n, t in UPDATE_FIELD_TYPES.items()],
            })
        else:
            self._reply({"error": "missing"}, 404)


def schema_reads(handler):
    return [r for r in handler.requests
            if r[0] == "GET" and "/admin/projects/" in r[1]]


def test_update_workflow_rejection_exits_7(tmp_path):
    """A rule said no. The input was valid and the server is healthy, so
    neither 6 nor 5 is honest, and retrying is pointless."""
    result, _ = run_update(
        tmp_path, "DEMO-1", "--field", "State=In Progress",
        handler=UpdateErrorHandler, status=400,
        body={"error": "Workflow runtime error",
              "error_description": "The require_attach_task_to_feature/rule rule "
                                   "threw an exception when processing DEMO-1",
              "error_rule_name": "require_attach_task_to_feature/rule",
              "error_workflow_type": "runtime",
              "error_type": "workflow"})

    assert result.returncode == 7, result.stderr
    assert "require_attach_task_to_feature/rule" in result.stderr


def test_update_workflow_rejection_does_not_fetch_schema(tmp_path):
    """A rule rejection is not a value problem; allowed values would mislead."""
    run_update(tmp_path, "DEMO-1", "--field", "State=In Progress",
               handler=UpdateErrorHandler, status=400,
               body={"error_type": "workflow",
                     "error_rule_name": "some/rule"})

    assert schema_reads(UpdateErrorHandler) == []


def test_update_bad_value_reports_allowed_values(tmp_path):
    """YouTrack's own message names no field and lists nothing."""
    result, _ = run_update(
        tmp_path, "DEMO-1", "--field", "Priority=Sideways",
        handler=UpdateErrorHandler, status=400,
        body={"error": "Bad Request",
              "error_description": "An Sideways-type entity with the specified "
                                   "name ({1}) was not found"})

    assert result.returncode == 6, result.stderr
    combined = result.stderr
    assert "Priority" in combined
    assert "Critical" in combined and "Low" in combined, "must list allowed values"
    assert "{1}" not in combined, "must not leak the unsubstituted placeholder"


def test_update_server_500_for_unknown_field_maps_to_exit_6(tmp_path):
    """A typo is input, not a server fault, whatever status YouTrack picks."""
    result, _ = run_update(
        tmp_path, "DEMO-1", "--field", "Priority=High",
        handler=UpdateErrorHandler, status=500,
        body={"error": "Internal Server Error",
              "error_description": "incompatible-issue-custom-field-name-Priority"})

    assert result.returncode == 6, result.stderr


def test_update_error_schema_read_is_not_paged(tmp_path):
    """F14: the error-translation re-read needs the COMPLETE field set for the
    same reason create's does — a truncated schema would falsely say a real
    field is unknown."""
    result, _ = run_update(
        tmp_path, "DEMO-1", "--field", "Priority=Sideways",
        handler=UpdateErrorHandler, status=400,
        body={"error": "Bad Request",
              "error_description": "An Sideways-type entity with the specified "
                                   "name ({1}) was not found"})

    assert result.returncode == 6, result.stderr
    path = schema_reads(UpdateErrorHandler)[0][1]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert query["$top"] == ["200"]
    assert "$skip" not in query


def test_update_happy_path_never_fetches_the_schema(tmp_path):
    result, _ = run_update(tmp_path, "DEMO-1", "--field", "Priority=High",
                           handler=UpdateErrorHandler, status=200,
                           body={"idReadable": "DEMO-1"})

    assert result.returncode == 0, result.stderr
    assert schema_reads(UpdateErrorHandler) == [], "option (C): no schema on success"


def test_update_unknown_field_suggests_a_near_miss(tmp_path):
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Prioritee=High",
                                handler=UpdateErrorHandler)

    assert result.returncode == 6
    assert "Priority" in result.stderr
    assert writes == [], "an unknown name is caught before the write"


def test_update_field_on_project_but_not_this_issue_is_distinguished(tmp_path):
    """Severity exists on the project but only on Bug-typed issues. That is a
    different mistake from a typo and must not be reported as one."""
    result, writes = run_update(tmp_path, "DEMO-1", "--field", "Severity=Severity 1",
                                handler=UpdateErrorHandler)

    assert result.returncode == 6
    assert writes == []
    lowered = result.stderr.lower()
    assert "did you mean" not in lowered, "not a typo — must not suggest a near-miss"
    assert "type" in lowered, "must explain the field is not carried by this issue"


def test_update_reads_issue_once_then_writes(tmp_path):
    """$type must come from one issue read, not one per field."""
    result, _ = run_update(tmp_path, "DEMO-1",
                           "--field", "Priority=High", "--field", "Points=3")

    assert result.returncode == 0, result.stderr
    reads = [r for r in UpdateFieldsHandler.requests if r[0] == "GET"]
    assert len(reads) == 1, f"expected one issue read, got {len(reads)}"
    assert "$type" in urllib.parse.unquote(reads[0][1]), "the read must project $type"


# ── issues create: custom fields in the create call (M2) ─────────────────
#
# Create cannot read $type off an issue that does not exist yet, so it resolves
# $type from the project schema — which reports `fieldType.id` and *not* the
# issue-side $type. That mapping is create's only genuinely new logic, so every
# fieldType.id gets its own assertion on the exact request body: a wrong constant
# produces a plausible-looking request that YouTrack rejects at the wire.
#
# One row per `fieldType.id`: (fieldType.id, field name, isMultiValue,
# bundle names, bundle logins, --field input, expected $type, expected value).
CREATE_TYPE_CASES = [
    ("state[1]", "State", False, ["Open", "In Progress"], None,
     "Open", "StateIssueCustomField", {"name": "Open"}),
    ("enum[1]", "Priority", False, ["Critical", "Normal"], None,
     "Normal", "SingleEnumIssueCustomField", {"name": "Normal"}),
    ("enum[*]", "Work Category", True, ["Infrastructure", "Technical Debt"], None,
     "Infrastructure", "MultiEnumIssueCustomField", [{"name": "Infrastructure"}]),
    ("ownedField[1]", "Subsystem", False, ["Ingestion"], None,
     "Ingestion", "SingleOwnedIssueCustomField", {"name": "Ingestion"}),
    ("user[1]", "Assignee", False, None, ["s.royz", "k.shmidt"],
     "s.royz", "SingleUserIssueCustomField", {"login": "s.royz"}),
    ("user[*]", "Requestor", True, None, ["s.royz", "k.shmidt"],
     "k.shmidt", "MultiUserIssueCustomField", [{"login": "k.shmidt"}]),
    ("version[1]", "Release Window", False, ["2026.1"], None,
     "2026.1", "SingleVersionIssueCustomField", {"name": "2026.1"}),
    ("version[*]", "Sprints", True, ["Sprint W13", "Sprint W14"], None,
     "Sprint W13", "MultiVersionIssueCustomField", [{"name": "Sprint W13"}]),
    ("build[1]", "Reported In", False, ["build-42"], None,
     "build-42", "SingleBuildIssueCustomField", {"name": "build-42"}),
    ("period", "Original Estimate", False, None, None,
     "1d 4h", "PeriodIssueCustomField", {"presentation": "1d 4h"}),
    ("integer", "Points", False, None, None, "3", "SimpleIssueCustomField", 3),
    ("float", "Story points", False, None, None, "3.5",
     "SimpleIssueCustomField", 3.5),
    ("date", "Due Date", False, None, None, "2026-08-15",
     "DateIssueCustomField", 1786795200000),
    ("date and time", "Incident Start Time", False, None, None, "1781534028493",
     "SimpleIssueCustomField", 1781534028493),
    ("text", "Acceptance Criteria", False, None, None, "- one\n- two",
     "TextIssueCustomField", {"text": "- one\n- two"}),
]


def _schema_row(idx, ftype, name, multi, names, logins, can_be_empty=True):
    bundle = None
    if names is not None or logins is not None:
        bundle = {"id": f"b{idx}"}
        if names is not None:
            bundle["values"] = [{"name": v} for v in names]
        if logins is not None:
            bundle["aggregatedUsers"] = [{"login": v} for v in logins]
    return {
        "id": str(idx),
        "canBeEmpty": can_be_empty,
        # Deliberately the *project*-side $type on every row, and deliberately
        # the wrong one for most of them. Measured: the schema reports
        # SimpleProjectCustomField even for a `date` field, so an implementation
        # that passed this value through would send a type mismatch. Every
        # expectation below therefore fails unless the fieldType.id map is used.
        "$type": "SimpleProjectCustomField",
        "field": {"name": name,
                  "fieldType": {"id": ftype, "isMultiValue": multi}},
        "bundle": bundle,
    }


CREATE_PROJECT_SCHEMA = [
    _schema_row(i, ftype, name, multi, names, logins,
                # State/Priority are canBeEmpty=false on every real project, and
                # that must not become a client-side required-field check.
                can_be_empty=name not in ("State", "Priority"))
    for i, (ftype, name, multi, names, logins, _v, _t, _e)
    in enumerate(CREATE_TYPE_CASES, start=1)
]


class CreateFieldsHandler(BaseHTTPRequestHandler):
    """Serves the project schema that supplies $type, and records the write."""

    requests = []
    write_status = 200
    write_body = None       # None → echo the created entity back

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
        if "/admin/projects/" in self.path and "/customFields" in self.path:
            # Honors $top/$skip like a real backend would, so a mutation that
            # pages this internal read is observable rather than silently
            # served the complete fixture regardless of the query.
            schema = self.schema()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            top = int(query["$top"][0]) if "$top" in query else len(schema)
            skip = int(query.get("$skip", ["0"])[0])
            self._reply(schema[skip:skip + top])
        else:
            self._reply({"error": "missing"}, 404)

    def schema(self):
        return CREATE_PROJECT_SCHEMA

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.__class__.requests.append(("POST", self.path, payload))
        status = self.__class__.write_status
        if self.__class__.write_body is not None:
            self._reply(self.__class__.write_body, status)
        elif self.path.startswith("/api/users/me/drafts"):
            self._reply({"id": "2-9", "idReadable": "Issue.Draft", **payload}, status)
        else:
            self._reply({"id": "2-9", "idReadable": "DEMO-9", **payload}, status)


def run_create(tmp_path, *args, handler=CreateFieldsHandler,
               status=200, body=None):
    handler.requests = []
    handler.write_status = status
    handler.write_body = body
    server, thread, base_url = _serve(handler)
    try:
        result = run_cli(tmp_path, base_url, "issues", "create",
                         "--project", "0-6", *args)
    finally:
        server.shutdown()
        thread.join()
    writes = [r for r in handler.requests if r[0] == "POST"]
    return result, writes


@pytest.mark.parametrize(
    "ftype,name,_multi,_names,_logins,given,dollar,expected",
    CREATE_TYPE_CASES, ids=[row[0] for row in CREATE_TYPE_CASES])
def test_create_marshals_each_project_field_type(
        tmp_path, ftype, name, _multi, _names, _logins, given, dollar, expected):
    """One row per fieldType.id, asserting the exact request entry."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", f"{name}={given}")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)[name] == {
        "name": name, "$type": dollar, "value": expected}, f"{ftype} marshalled wrong"


def test_create_sends_project_summary_and_every_field_in_one_call(tmp_path):
    """The whole body, so an extra or missing key is caught too."""
    flags = []
    for _ftype, name, _multi, _names, _logins, given, _t, _e in CREATE_TYPE_CASES:
        flags += ["--field", f"{name}={given}"]
    result, writes = run_create(tmp_path, "--summary", "Born ready",
                                "--description", "Body", *flags)

    assert result.returncode == 0, result.stderr
    assert len(writes) == 1
    _method, path, payload = writes[0]
    assert path.startswith("/api/issues")
    assert payload["project"] == {"id": "0-6"}
    assert payload["summary"] == "Born ready"
    assert payload["description"] == "Body"
    assert payload["customFields"] == [
        {"name": name, "$type": dollar, "value": expected}
        for _ftype, name, _m, _n, _l, _given, dollar, expected in CREATE_TYPE_CASES
    ]


class EveryMappedTypeHandler(CreateFieldsHandler):
    """A project carrying exactly one field per _ISSUE_TYPE_BY_FIELD_TYPE row."""

    requests = []

    def schema(self):
        return [_schema_row(i, ftype, f"F {ftype}",
                            ftype.endswith("[*]"), None, None)
                for i, ftype in enumerate(sorted(_issue_type_map()), start=1)]


def test_every_mapped_field_type_is_dispatched_by_marshal_one(tmp_path):
    """Coverage guard driven by the map itself, so a row added later cannot
    escape it. Fifteen rows have their exact body asserted individually above;
    this one only proves no row falls through _marshal_one's final branch to
    `unsupported_field_type`, which is how an unhandled $type would surface."""
    mapping = _issue_type_map()
    flags = []
    for ftype in sorted(mapping):
        given = "2026-08-15" if ftype == "date" else "x"
        flags += ["--field", f"F {ftype}={given}"]
    result, writes = run_create(tmp_path, "--summary", "s", *flags,
                                handler=EveryMappedTypeHandler)

    assert result.returncode == 0, result.stderr
    sent = sent_fields(writes)
    assert set(sent) == {f"F {ftype}" for ftype in mapping}
    for ftype, dollar in mapping.items():
        entry = sent[f"F {ftype}"]
        assert entry["$type"] == dollar, ftype
        assert entry["value"] is not None, f"{ftype} marshalled to nothing"


def create_schema_reads(handler=CreateFieldsHandler):
    return [r for r in handler.requests
            if r[0] == "GET" and "/admin/projects/" in r[1]]


def test_create_happy_path_fetches_the_schema_exactly_once(tmp_path):
    """$type is mandatory and no issue exists to read it off, so create must
    fetch the schema — once. Zero would mean it guessed; two would mean it
    re-read what it already had."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Priority=Normal",
                                "--field", "Points=3")

    assert result.returncode == 0, result.stderr
    assert len(create_schema_reads()) == 1, create_schema_reads()
    assert len(writes) == 1


def test_create_schema_read_is_not_paged(tmp_path):
    """F14: create's schema read is the internal-caller path, which needs the
    COMPLETE field set — a truncated schema turns a legal field name into a
    false 'unknown field'. Only `projects fields list` may page."""
    result, _writes = run_create(tmp_path, "--summary", "s",
                                 "--field", "Priority=Normal")

    assert result.returncode == 0, result.stderr
    path = create_schema_reads()[0][1]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert query["$top"] == ["200"]
    assert "$skip" not in query


def test_create_without_fields_never_fetches_the_schema(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "plain")

    assert result.returncode == 0, result.stderr
    assert create_schema_reads() == [], "no fields named means no $type needed"
    assert "customFields" not in writes[0][2]


def test_create_converts_calendar_date_to_noon_utc(tmp_path):
    """Measured live: 2026-08-15 is stored as 1786795200000, i.e. 12:00 UTC of
    the same UTC day. An ISO string is rejected at the wire, so the conversion
    has to happen here."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Due Date=2026-08-15")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Due Date"]["value"] == 1786795200000


def test_create_date_field_reads_back_as_a_calendar_date(tmp_path):
    """Never assert epoch equality across a `date` round trip — compare the
    calendar day, which is what survives the server's noon-UTC snap."""
    result, _writes = run_create(tmp_path, "--summary", "s",
                                 "--field", "Due Date=2026-08-15")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["fields"]["Due Date"] == "2026-08-15"


def test_create_date_and_time_keeps_epoch_ms_exactly(tmp_path):
    """`date and time` is SimpleIssueCustomField and is not normalized, so here
    epoch equality does hold, on the way out and back."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Incident Start Time=1781534028493")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Incident Start Time"] == {
        "name": "Incident Start Time", "$type": "SimpleIssueCustomField",
        "value": 1781534028493}
    assert json.loads(result.stdout)["fields"]["Incident Start Time"] == 1781534028493


def test_create_draft_posts_to_the_drafts_endpoint(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "draft me", "--draft")

    assert result.returncode == 0, result.stderr
    assert len(writes) == 1
    assert writes[0][1].startswith("/api/users/me/drafts")
    assert not writes[0][1].startswith("/api/issues")
    assert json.loads(result.stdout)["idReadable"] == "Issue.Draft"


def test_create_draft_carries_the_full_field_set(tmp_path):
    """Measured: POST /api/users/me/drafts applies a whole custom-field set in
    the create call, so a draft can be born ready."""
    result, writes = run_create(
        tmp_path, "--summary", "draft me", "--draft",
        "--field", "Priority=Normal", "--field", "Points=3",
        "--field", "Sprints=Sprint W13")

    assert result.returncode == 0, result.stderr
    assert writes[0][1].startswith("/api/users/me/drafts")
    sent = sent_fields(writes)
    assert sent["Priority"]["value"] == {"name": "Normal"}
    assert sent["Points"]["value"] == 3
    assert sent["Sprints"]["value"] == [{"name": "Sprint W13"}]


def test_create_draft_may_set_state(tmp_path):
    """State on a draft is settable (measured on ION). Where a project's
    workflow rejects it that is exit 7, not a refusal the CLI invents."""
    result, writes = run_create(tmp_path, "--summary", "draft me", "--draft",
                                "--field", "State=Open")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["State"] == {
        "name": "State", "$type": "StateIssueCustomField",
        "value": {"name": "Open"}}


def test_read_only_connection_refuses_draft_create_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "create",
                     "--project", "0-6", "--summary", "blocked", "--draft",
                     "--field", "Priority=Normal")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_create_omitting_can_be_empty_false_fields_succeeds(tmp_path):
    """Guards a measured fact against a well-meant regression: canBeEmpty=false
    means "may not be emptied", NOT "required at create". A create omitting
    State and Priority returns 200 and the server defaults them, so a
    client-side required-field check would reject creates YouTrack accepts."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Points=3")

    assert result.returncode == 0, result.stderr
    assert len(writes) == 1, "must send, not refuse"
    names = {f["name"] for f in writes[0][2]["customFields"]}
    assert names == {"Points"}
    assert "required" not in result.stderr.lower()


def test_create_with_no_fields_at_all_is_not_blocked_by_required_fields(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "bare")

    assert result.returncode == 0, result.stderr
    assert writes[0][2] == {"project": {"id": "0-6"}, "summary": "bare"}


def test_create_unknown_field_suggests_a_near_miss_and_does_not_write(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Prioritee=Normal")

    assert result.returncode == 6
    assert "Priority" in result.stderr
    assert writes == [], "an unknown name is caught before the write"


def test_create_unknown_field_message_names_the_project_not_the_issue(tmp_path):
    """There is no issue yet, so "no field named X on this issue" would be a
    lie about what was checked."""
    result, _writes = run_create(tmp_path, "--summary", "s",
                                 "--field", "Nonexistent Field=1")

    assert result.returncode == 6
    assert "0-6" in result.stderr
    assert "on this issue" not in result.stderr


def test_create_rejects_a_value_outside_its_bundle_before_writing(tmp_path):
    """The schema is already in hand, so full value validation is free here —
    unlike update, where it would cost an extra request."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Priority=Sideways")

    assert result.returncode == 6
    assert writes == [], "a bad value is caught before the write"
    assert "Priority" in result.stderr
    assert "Critical" in result.stderr and "Normal" in result.stderr
    assert len(create_schema_reads()) == 1


def test_create_does_not_pre_reject_an_unrecognised_login(tmp_path):
    """A user field's legal set is bundle.aggregatedUsers, which is
    permission-scoped — measured: the server itself assigns logins absent from
    the list a token can see. So an unknown login is a soft signal and must
    reach the server rather than be refused locally."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Assignee=c.wootson")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Assignee"]["value"] == {"login": "c.wootson"}


def test_create_bad_date_exits_6_before_writing(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Due Date=15/08/2026")

    assert result.returncode == 6
    assert "yyyy-mm-dd" in result.stderr.lower()
    assert writes == []


def test_create_repeated_field_accumulates_for_multi_value(tmp_path):
    result, writes = run_create(
        tmp_path, "--summary", "s",
        "--field", "Work Category=Infrastructure",
        "--field", "Work Category=Technical Debt")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Work Category"]["value"] == [
        {"name": "Infrastructure"}, {"name": "Technical Debt"}]


def test_create_field_overrides_the_fields_document(tmp_path):
    """Same merge order as update: --fields first, then --field patches it."""
    result, writes = run_create(
        tmp_path, "--summary", "s",
        "--fields", json.dumps({"Priority": "Critical", "Points": 1}),
        "--field", "Priority=Normal")

    assert result.returncode == 0, result.stderr
    sent = sent_fields(writes)
    assert sent["Priority"]["value"] == {"name": "Normal"}
    assert sent["Points"]["value"] == 1


def test_create_matches_field_names_case_insensitively(tmp_path):
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "PRIORITY=Normal")

    assert result.returncode == 0, result.stderr
    assert sent_fields(writes)["Priority"]["name"] == "Priority"


class UnmappedTypeHandler(CreateFieldsHandler):
    requests = []

    def schema(self):
        return [_schema_row(1, "quantum[1]", "Spin", False, None, None)]


def test_create_unmapped_field_type_exits_6_before_writing(tmp_path):
    """No mapping means no $type, and YouTrack would answer "$type is required".
    Say so by name instead of sending an entry that cannot succeed."""
    result, writes = run_create(tmp_path, "--summary", "s",
                                "--field", "Spin=up",
                                handler=UnmappedTypeHandler)

    assert result.returncode == 6
    assert writes == []
    assert "quantum[1]" in result.stderr


def test_create_workflow_rejection_exits_7_without_a_second_schema_read(tmp_path):
    result, _writes = run_create(
        tmp_path, "--summary", "s", "--field", "State=Open",
        status=400,
        body={"error": "Workflow runtime error",
              "error_description": "The require_attach_task_to_feature/rule rule "
                                   "threw an exception",
              "error_rule_name": "require_attach_task_to_feature/rule",
              "error_type": "workflow"})

    assert result.returncode == 7, result.stderr
    assert "require_attach_task_to_feature/rule" in result.stderr
    assert len(create_schema_reads()) == 1, "the schema is read once, not again"


def test_create_type_scoped_rejection_surfaces_the_server_message(tmp_path):
    """The Type-scoped subset does not exist until the issue does, so this one
    cannot be pre-flighted; the server's 400 must come through as input error."""
    result, _writes = run_create(
        tmp_path, "--summary", "s", "--field", "Reported In=build-42",
        status=400,
        body={"error": "Bad Request",
              "error_description": "You can only update the value for the "
                                   "Reported In field when the value for the "
                                   "Type field is Bug"})

    assert result.returncode == 6, result.stderr
    assert "Reported In" in result.stderr
    assert "Type field" in result.stderr


def test_create_server_500_for_unknown_field_maps_to_exit_6(tmp_path):
    result, _writes = run_create(
        tmp_path, "--summary", "s", "--field", "Priority=Normal",
        status=500,
        body={"error": "Internal Server Error",
              "error_description": "incompatible-issue-custom-field-name-Priority"})

    assert result.returncode == 6, result.stderr


def test_create_flattens_the_created_issue_fields(tmp_path):
    result, _writes = run_create(tmp_path, "--summary", "s",
                                 "--field", "Priority=Normal")

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "customFields" not in body
    assert body["fields"]["Priority"] == "Normal"


def test_articles_list_all_and_by_project(tmp_path):
    requests = []

    class ArticlesHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = json.dumps([
                {"id": "150-1", "idReadable": "KB-A-1", "summary": "Onboarding"},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server, thread, base_url = _serve(ArticlesHandler)
    try:
        all_res = run_cli(tmp_path, base_url, "articles", "list", "--limit", "10")
        proj_res = run_cli(tmp_path, base_url, "articles", "list", "--project", "0-6")
    finally:
        server.shutdown()
        thread.join()

    assert all_res.returncode == 0, all_res.stderr
    assert proj_res.returncode == 0, proj_res.stderr
    assert json.loads(all_res.stdout)["items"][0]["idReadable"] == "KB-A-1"
    assert requests[0].startswith("/api/articles?")
    # $top is limit + 1 (11), not limit (10): the extra row makes truncation detectable.
    assert "$top=11" in requests[0] or "%24top=11" in requests[0]
    assert requests[1].startswith("/api/admin/projects/0-6/articles?")


def test_articles_limit_must_be_positive(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "list", "--limit", "0")
    assert result.returncode == 6
    assert "positive" in result.stderr


def test_article_read_and_comments(tmp_path):
    requests = []

    class OneHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if "/comments" in self.path:
                payload = [{"id": "160-1", "text": "First note"}]
            else:
                payload = {"id": "150-1", "idReadable": "KB-A-1",
                           "summary": "Onboarding", "content": "# Body"}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server, thread, base_url = _serve(OneHandler)
    try:
        art = run_cli(tmp_path, base_url, "articles", "get", "KB-A-1")
        # ID extraction from a pasted article URL
        art_url = run_cli(tmp_path, base_url, "articles", "get",
                          f"{base_url}/articles/KB-A-1")
        coms = run_cli(tmp_path, base_url, "articles", "comments", "list", "KB-A-1", "--limit", "5")
    finally:
        server.shutdown()
        thread.join()

    assert art.returncode == 0, art.stderr
    assert json.loads(art.stdout)["content"] == "# Body"
    assert art_url.returncode == 0, art_url.stderr
    assert coms.returncode == 0, coms.stderr
    assert json.loads(coms.stdout)["items"][0]["text"] == "First note"
    assert requests[0].startswith("/api/articles/KB-A-1?")
    assert requests[1].startswith("/api/articles/KB-A-1?")
    assert requests[2].startswith("/api/articles/KB-A-1/comments?")


article_write_requests = []

class ArticleWriteHandler(BaseHTTPRequestHandler):
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
        article_write_requests.append(("GET", self.path, None))
        if self.path.startswith("/api/articles/KB-A-1?"):
            self._reply({
                "id": "150-1",
                "idReadable": "KB-A-1",
                "project": {"id": "0-6"},
            })
        else:
            self._reply({"error": "missing"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        article_write_requests.append(("POST", self.path, payload))
        if self.path.startswith("/api/articles?") and "project" not in payload:
            self._reply({"error": "project is required"}, 400)
            return
        self._reply({"id": "150-9", "idReadable": "KB-A-9", **payload})


def test_article_create_update_comment_payloads(tmp_path):
    article_write_requests.clear()
    server, thread, base_url = _serve(ArticleWriteHandler)
    try:
        created = run_cli(tmp_path, base_url, "articles", "create",
                          "--summary", "Guide", "--content", "# Hi", "--project", "0-6")
        sub = run_cli(tmp_path, base_url, "articles", "create",
                      "--summary", "Child", "--project", "0-6",
                      "--parent", f"{base_url}/articles/KB-A-1")
        updated = run_cli(tmp_path, base_url, "articles", "update", "KB-A-9",
                          "--summary", "Renamed")
        commented = run_cli(tmp_path, base_url, "articles", "comments", "add", "KB-A-9",
                            "--text", "Nice")
    finally:
        server.shutdown()
        thread.join()

    assert created.returncode == 0, created.stderr
    assert sub.returncode == 0, sub.stderr
    assert updated.returncode == 0, updated.stderr
    assert commented.returncode == 0, commented.stderr
    posts = [(path, body) for method, path, body in article_write_requests
             if method == "POST"]
    # create with project
    assert ("/api/articles", {"summary": "Guide", "content": "# Hi",
                              "project": {"id": "0-6"}}) in \
        [(p.split("?")[0], b) for p, b in posts]
    # sub-article resolves its parent and carries both required project and parent.
    parent_reads = [(p, b) for method, p, b in article_write_requests
                    if method == "GET" and p.startswith("/api/articles/KB-A-1?")]
    assert parent_reads
    sub_bodies = [b for p, b in posts
                  if p.split("?")[0] == "/api/articles" and "parentArticle" in b]
    assert sub_bodies and sub_bodies[0]["parentArticle"] == {"id": "150-1"}
    assert sub_bodies[0]["project"] == {"id": "0-6"}
    # update targets the article
    upd = [(p, b) for p, b in posts
           if p.split("?")[0] == "/api/articles/KB-A-9" and "/comments" not in p]
    assert upd and upd[0][1] == {"summary": "Renamed"}
    # comment
    com = [(p, b) for p, b in posts
           if p.split("?")[0] == "/api/articles/KB-A-9/comments"]
    assert com and com[0][1] == {"text": "Nice"}


def test_article_create_requires_project(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "create", "--summary", "Orphan")
    assert result.returncode == 2
    assert "--project" in result.stderr


def test_article_update_requires_a_field(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "update", "KB-A-9")
    assert result.returncode == 6
    assert "--summary, --content or --parent" in result.stderr


def test_read_only_connection_refuses_article_create(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "create",
                     "--summary", "blocked", "--project", "0-6")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_read_only_connection_refuses_article_update_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "update",
                     "KB-A-9", "--summary", "blocked")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_read_only_connection_refuses_article_comment_before_network(tmp_path):
    envelope = tmp_path / "capabilities" / "youtrack"
    envelope.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (envelope / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {
            "secret_env": "YOUTRACK_TOKEN",
            "base_url": "http://127.0.0.1:1",
            "allow_write": False,
        }},
    }))
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "comments", "add",
                     "KB-A-9", "--text", "blocked")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_article_create_rejects_parent_from_another_project(tmp_path):
    article_write_requests.clear()

    class OtherProjectHandler(ArticleWriteHandler):
        def do_GET(self):
            article_write_requests.append(("GET", self.path, None))
            self._reply({
                "id": "150-1",
                "idReadable": "KB-A-1",
                "project": {"id": "0-7"},
            })

    server, thread, base_url = _serve(OtherProjectHandler)
    try:
        result = run_cli(tmp_path, base_url, "articles", "create",
                         "--summary", "X", "--project", "0-6",
                         "--parent", "KB-A-1")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 6
    assert "not '0-6'" in result.stderr
    assert not [row for row in article_write_requests if row[0] == "POST"]


def test_article_update_rejects_empty_summary(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "update",
                     "KB-A-9", "--summary", "")
    assert result.returncode == 6
    assert ("articles update needs a non-empty --summary, --content or --parent"
            in result.stderr)


def test_article_comment_rejects_empty_text(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "comments", "add",
                     "KB-A-9", "--text", "   ")
    assert result.returncode == 6
    assert "empty" in result.stderr


def test_article_not_found_exits_3(tmp_path):
    class NotFoundHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

    server, thread, base_url = _serve(NotFoundHandler)
    try:
        result = run_cli(tmp_path, base_url, "articles", "get", "KB-NOPE")
    finally:
        server.shutdown()
        thread.join()
    assert result.returncode == 3


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


def _reparent_handler():
    """Serves A-1/5-1 and A-2/5-2 in the same project; records the parent POST."""
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
    return ReparentHandler


def test_articles_update_reparents(tmp_path):
    handler = _reparent_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "articles", "update", "A-1",
                         "--parent", "A-2")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
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


def test_ambiguous_group_name_exits_6_with_no_write(tmp_path):
    # Two groups sharing a name: resolving must refuse rather than pick a
    # winner, mirroring _resolve_link's refusal of an ambiguous link phrase.
    dup_groups = [{"id": "3-4", "name": "Incidents"},
                  {"id": "3-9", "name": "Incidents"}]
    handler = _visibility_handler(dup_groups)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-groups", "Incidents")
    assert result.returncode == 6
    assert "ambiguous" in result.stderr
    assert not any(r[0] == "POST" for r in handler.requests)


def test_comment_permitted_users_rejects_empty_login(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-users", "")
    assert result.returncode == 6
    assert not any(r[0] == "POST" for r in handler.requests)


def test_comment_permitted_groups_rejects_empty_name(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi", "--permitted-groups", "   ")
    assert result.returncode == 6
    assert not any(r[0] == "POST" for r in handler.requests)


def test_comment_without_visibility_flags_sends_no_visibility_key(tmp_path):
    handler = _visibility_handler(_GROUPS)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "comments", "add", "DEMO-1",
                         "--text", "hi")
    assert result.returncode == 0, result.stderr
    post = [r for r in handler.requests if r[0] == "POST"][0]
    assert "visibility" not in post[2]


_WORK_ITEM = {"id": "162-1", "date": 1784505600000,
              "duration": {"minutes": 90, "presentation": "1h 30m",
                           "$type": "DurationValue"},
              "text": None, "type": None,
              "author": {"login": "s.royz"}, "created": 1785273759478}

_PROJECT_WORK_TYPES = [{"id": "139-0", "name": "Development"},
                       {"id": "139-1", "name": "Testing"},
                       {"id": "139-2", "name": "Documentation"},
                       {"id": "139-3", "name": "Meeting"},
                       {"id": "139-4", "name": "Support"}]

# Measured 2026-07-28: the instance-wide endpoint lists 6 work-item types on
# ION, project 0-1 lists 5 — `Review` exists instance-wide and is refused by
# the project. The fixture keeps that asymmetry so a test can tell "resolved
# against the project's set" apart from "resolved against the instance's".
_INSTANCE_WORK_TYPES = _PROJECT_WORK_TYPES + [{"id": "139-5", "name": "Review"}]


def _work_handler(items=None, types=None, *, post_status=200, post_body=None,
                  instance_types=None):
    """Serves the issue read (for its project), the project's work-item types
    (project-scoped) and the instance-wide types (distinct set, see above),
    the work-item POST, and the work-item list."""
    items = [_WORK_ITEM] if items is None else items
    types = _PROJECT_WORK_TYPES if types is None else types
    instance_types = _INSTANCE_WORK_TYPES if instance_types is None else instance_types

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
            if route == "/api/admin/timeTrackingSettings/workItemTypes":
                self._reply(instance_types)
            elif route.endswith("/timeTrackingSettings/workItemTypes"):
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


def test_work_log_refuses_an_empty_duration_before_any_request(tmp_path):
    handler = _work_handler()
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "")
    assert result.returncode == 6
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


def test_work_log_ambiguous_type_is_refused_not_guessed(tmp_path):
    """I-5: nothing in the measured record says work-item-type names are
    unique on a project, so a collision must be refused (exit 6), mirroring
    _resolve_tag_id / _resolve_group_ids / _resolve_link, not resolved to
    whichever row happened to match first."""
    collide = [{"id": "139-1", "name": "Testing"}, {"id": "139-9", "name": "Testing"}]
    handler = _work_handler(types=collide)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "30m", "--type", "Testing")
    assert result.returncode == 6
    assert "ambiguous" in result.stderr.lower()
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


def test_work_log_flattens_type_and_author(tmp_path):
    """_shape_work_item must flatten `type` to its name and `author` to its
    login, never emit the whole object (which would leak $type)."""
    item = dict(_WORK_ITEM,
               type={"id": "139-1", "name": "Testing", "$type": "WorkItemType"},
               author={"login": "s.royz", "$type": "User"})
    handler = _work_handler(post_body=item)
    with serve(handler) as base:
        result = run_cli(tmp_path, base, "issues", "work", "log", "DEMO-1",
                         "--duration", "1h 30m")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "Testing"
    assert payload["author"] == "s.royz"
    assert "$type" not in json.dumps(payload)


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

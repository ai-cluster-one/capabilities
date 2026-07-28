import ast
import contextlib
import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CLI = Path(__file__).parents[1] / "bin" / "youtrack"


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
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
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
    assert parsed[0]["shortName"] == "DEMO"
    assert ProjectsHandler.requests[0].startswith("/api/admin/projects?")
    assert "query=Demo" in ProjectsHandler.requests[0]


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
            self._reply(PROJECT_FIELDS_PAYLOAD)
        else:
            self._reply({"error": "missing"}, 404)


def test_projects_fields_list_shapes_schema(tmp_path):
    ProjectFieldsHandler.requests = []
    with serve(ProjectFieldsHandler) as base:
        result = run_cli(tmp_path, base, "projects", "fields", "list", "0-6")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"name": "Priority", "type": "enum[1]", "multiValue": False,
         "required": True, "values": ["Critical", "Normal"]},
        {"name": "Assignee", "type": "user[1]", "multiValue": False,
         "required": False, "values": ["s.royz", "j.howell"]},
        {"name": "Points", "type": "integer", "multiValue": False,
         "required": False, "values": None},
    ]


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
    parsed = json.loads(result.stdout)
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
    parsed = json.loads(result.stdout)
    assert parsed == [
        {"name": "Priority", "type": "enum[1]", "multiValue": False,
         "required": True, "values": ["Critical"]},
    ]


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
    assert json.loads(result.stdout)[0]["text"] == "A note"
    assert IssueCommentsHandler.requests[0].startswith("/api/issues/DEMO-1/comments?")
    assert ("$top=5" in IssueCommentsHandler.requests[0]
            or "%24top=5" in IssueCommentsHandler.requests[0])


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
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    assert parsed[0]["idReadable"] == "DEMO-1"
    assert parsed[0]["fields"]["State"] == "Open"
    assert "customFields" not in parsed[0]
    assert parsed[1]["idReadable"] == "DEMO-2"
    assert parsed[1]["fields"]["State"] == "In Progress"
    assert "query=state%3AOpen" in IssuesHandler.requests[0][1]
    assert "$top=10" in IssuesHandler.requests[0][1] or "%24top=10" in IssuesHandler.requests[0][1]
    assert "customFields=State" not in IssuesHandler.requests[0][1]
    assert "$top=100" in IssuesHandler.requests[1][1] or "%24top=100" in IssuesHandler.requests[1][1]


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


def test_issues_search_emits_same_fields_shape(tmp_path):
    CustomFieldsHandler.requests = []
    with serve(CustomFieldsHandler) as base:
        result = run_cli(tmp_path, base, "issues", "search", "project: DEMO")
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)[0]
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
            self._reply(UPDATE_PROJECT_SCHEMA)
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
    assert json.loads(all_res.stdout)[0]["idReadable"] == "KB-A-1"
    assert requests[0].startswith("/api/articles?")
    assert "$top=10" in requests[0] or "%24top=10" in requests[0]
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
    assert json.loads(coms.stdout)[0]["text"] == "First note"
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
    assert "--summary or --content" in result.stderr


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
    assert "articles update needs a non-empty --summary or --content" in result.stderr


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

import ast
import contextlib
import json
import os
import subprocess
import threading
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), UsersMeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "users", "me")
    finally:
        server.shutdown()
        thread.join()

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
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProjectsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "projects", "find", "Demo")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed[0]["shortName"] == "DEMO"
    assert ProjectsHandler.requests[0].startswith("/api/admin/projects?")
    assert "query=Demo" in ProjectsHandler.requests[0]


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
    server = ThreadingHTTPServer(("127.0.0.1", 0), IssueGetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "issues", "get", "DEMO-1")
    finally:
        server.shutdown()
        thread.join()

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
    server = ThreadingHTTPServer(("127.0.0.1", 0), IssueCommentsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "issues", "comments", "list", "DEMO-1", "--limit", "5")
    finally:
        server.shutdown()
        thread.join()

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
        "Due Date": 1781534028493,
        "Blocked Reason": None,
    }


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


def test_update_requires_state(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "update", "DEMO-1")
    assert result.returncode == 2
    assert "required: --state" in result.stderr.lower()


def test_update_http_request_shape(tmp_path):
    class UpdateHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            # Reject old invalid endpoint
            if "/fields/State" in self.path:
                self.send_response(404)
                self.end_headers()
                return

            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw)
            self.__class__.requests.append(("POST", self.path, self.headers, payload))

            # Validate correct payload structure
            if "customFields" in payload and len(payload["customFields"]) > 0:
                field = payload["customFields"][0]
                body = json.dumps({"customFields": [field]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(400)
                self.end_headers()

    UpdateHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), UpdateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "issues", "update", "DEMO-1", "--state", "In Progress")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert len(UpdateHandler.requests) == 1
    method, path, headers, payload = UpdateHandler.requests[0]
    assert method == "POST"
    assert path.startswith("/api/issues/DEMO-1")
    assert "/fields/State" not in path, "Must not use old invalid /fields/State endpoint"
    assert "customFields" in payload
    assert len(payload["customFields"]) == 1
    field = payload["customFields"][0]
    assert field["name"] == "State"
    assert field["$type"] == "StateIssueCustomField"
    assert field["value"] == {"name": "In Progress"}
    assert headers["Authorization"] == "Bearer perm:test"


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
                     "DEMO-1", "--state", "Done")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_update_handles_api_errors(tmp_path):
    class ErrorHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path.startswith("/api/issues/DEMO-1"):
                self.send_response(400)
                body = json.dumps({"error": "Invalid state"}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "issues", "update", "DEMO-1", "--state", "Invalid")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 6
    assert "invalid_request" in result.stderr.lower()


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


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

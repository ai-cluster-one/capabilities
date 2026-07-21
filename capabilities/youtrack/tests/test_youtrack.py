import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CLI = Path(__file__).parents[1] / "bin" / "youtrack"


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


def test_create_and_comment_payloads(tmp_path):
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        created = run_cli(tmp_path, base_url, "create", "--project", "0-0",
                          "--summary", "First issue", "--description", "Body")
        commented = run_cli(tmp_path, base_url, "comment", "DEMO-1",
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
    result = run_cli(tmp_path, "http://127.0.0.1:1", "create",
                     "--project", "0-0", "--summary", "blocked")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_issues_requires_query(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues")
    assert result.returncode == 2
    assert "required: query" in result.stderr.lower()


def test_issues_limit_must_be_positive(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "issues", "state:Open", "--limit", "0")
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
        result = run_cli(tmp_path, base_url, "issues", "state:Open", "--limit", "10")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    assert parsed[0]["idReadable"] == "DEMO-1"
    assert parsed[0]["State"] == "Open"
    assert parsed[1]["idReadable"] == "DEMO-2"
    assert parsed[1]["State"] == "In Progress"
    assert "query=state%3AOpen" in IssuesHandler.requests[0][1]
    assert "$top=10" in IssuesHandler.requests[0][1] or "%24top=10" in IssuesHandler.requests[0][1]


def test_update_requires_state(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "update", "DEMO-1")
    assert result.returncode == 2
    assert "required: --state" in result.stderr.lower()


def test_update_http_request_shape(tmp_path):
    class UpdateHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw)
            self.__class__.requests.append(("POST", self.path, self.headers, payload))
            body = json.dumps({"name": "State", "value": {"name": payload["value"]["name"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    UpdateHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), UpdateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = run_cli(tmp_path, base_url, "update", "DEMO-1", "--state", "In Progress")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert len(UpdateHandler.requests) == 1
    method, path, headers, payload = UpdateHandler.requests[0]
    assert method == "POST"
    assert path.startswith("/api/issues/DEMO-1/fields/State")
    assert payload == {"value": {"name": "In Progress"}}
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
    result = run_cli(tmp_path, "http://127.0.0.1:1", "update",
                     "DEMO-1", "--state", "Done")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_update_handles_api_errors(tmp_path):
    class ErrorHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path.startswith("/api/issues/DEMO-1/fields/State"):
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
        result = run_cli(tmp_path, base_url, "update", "DEMO-1", "--state", "Invalid")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 6
    assert "invalid_request" in result.stderr.lower()

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
        default_result = run_cli(tmp_path, base_url, "issues", "state:Open")
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert default_result.returncode == 0, default_result.stderr
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    assert parsed[0]["idReadable"] == "DEMO-1"
    assert parsed[0]["State"] == "Open"
    assert parsed[1]["idReadable"] == "DEMO-2"
    assert parsed[1]["State"] == "In Progress"
    assert "query=state%3AOpen" in IssuesHandler.requests[0][1]
    assert "$top=10" in IssuesHandler.requests[0][1] or "%24top=10" in IssuesHandler.requests[0][1]
    assert "customFields=State" in IssuesHandler.requests[0][1]
    assert "$top=100" in IssuesHandler.requests[1][1] or "%24top=100" in IssuesHandler.requests[1][1]


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
        result = run_cli(tmp_path, base_url, "update", "DEMO-1", "--state", "In Progress")
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
    result = run_cli(tmp_path, "http://127.0.0.1:1", "update",
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
        result = run_cli(tmp_path, base_url, "update", "DEMO-1", "--state", "Invalid")
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
    class ArticlesHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
            body = json.dumps([
                {"id": "150-1", "idReadable": "KB-A-1", "summary": "Onboarding"},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ArticlesHandler.requests = []
    server, thread, base_url = _serve(ArticlesHandler)
    try:
        all_res = run_cli(tmp_path, base_url, "articles", "--limit", "10")
        proj_res = run_cli(tmp_path, base_url, "articles", "--project", "0-6")
    finally:
        server.shutdown()
        thread.join()

    assert all_res.returncode == 0, all_res.stderr
    assert proj_res.returncode == 0, proj_res.stderr
    assert json.loads(all_res.stdout)[0]["idReadable"] == "KB-A-1"
    assert ArticlesHandler.requests[0].startswith("/api/articles?")
    assert "$top=10" in ArticlesHandler.requests[0] or "%24top=10" in ArticlesHandler.requests[0]
    assert ArticlesHandler.requests[1].startswith("/api/admin/projects/0-6/articles?")


def test_articles_limit_must_be_positive(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "articles", "--limit", "0")
    assert result.returncode == 6
    assert "positive" in result.stderr


def test_article_read_and_comments(tmp_path):
    class OneHandler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.__class__.requests.append(self.path)
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

    OneHandler.requests = []
    server, thread, base_url = _serve(OneHandler)
    try:
        art = run_cli(tmp_path, base_url, "article", "KB-A-1")
        # ID extraction from a pasted article URL
        art_url = run_cli(tmp_path, base_url, "article",
                          f"{base_url}/articles/KB-A-1")
        coms = run_cli(tmp_path, base_url, "article-comments", "KB-A-1", "--limit", "5")
    finally:
        server.shutdown()
        thread.join()

    assert art.returncode == 0, art.stderr
    assert json.loads(art.stdout)["content"] == "# Body"
    assert art_url.returncode == 0, art_url.stderr
    assert coms.returncode == 0, coms.stderr
    assert json.loads(coms.stdout)[0]["text"] == "First note"
    assert OneHandler.requests[0].startswith("/api/articles/KB-A-1?")
    assert OneHandler.requests[1].startswith("/api/articles/KB-A-1?")
    assert OneHandler.requests[2].startswith("/api/articles/KB-A-1/comments?")


class ArticleWriteHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.__class__.requests.append((self.path, payload))
        body = json.dumps({"id": "150-9", "idReadable": "KB-A-9", **payload}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_article_create_update_comment_payloads(tmp_path):
    ArticleWriteHandler.requests = []
    server, thread, base_url = _serve(ArticleWriteHandler)
    try:
        created = run_cli(tmp_path, base_url, "article-create",
                          "--summary", "Guide", "--content", "# Hi", "--project", "0-6")
        sub = run_cli(tmp_path, base_url, "article-create",
                      "--summary", "Child", "--parent", "KB-A-1")
        updated = run_cli(tmp_path, base_url, "article-update", "KB-A-9",
                          "--summary", "Renamed")
        commented = run_cli(tmp_path, base_url, "article-comment", "KB-A-9",
                            "--text", "Nice")
    finally:
        server.shutdown()
        thread.join()

    assert created.returncode == 0, created.stderr
    assert sub.returncode == 0, sub.stderr
    assert updated.returncode == 0, updated.stderr
    assert commented.returncode == 0, commented.stderr
    reqs = dict((p, body) for p, body in ArticleWriteHandler.requests)
    # create with project
    assert ("/api/articles", {"summary": "Guide", "content": "# Hi",
                              "project": {"id": "0-6"}}) in \
        [(p.split("?")[0], b) for p, b in ArticleWriteHandler.requests]
    # sub-article carries parentArticle, no project
    sub_bodies = [b for p, b in ArticleWriteHandler.requests
                  if p.split("?")[0] == "/api/articles" and "parentArticle" in b]
    assert sub_bodies and sub_bodies[0]["parentArticle"] == {"id": "KB-A-1"}
    assert "project" not in sub_bodies[0]
    # update targets the article
    upd = [(p, b) for p, b in ArticleWriteHandler.requests
           if p.split("?")[0] == "/api/articles/KB-A-9" and "/comments" not in p]
    assert upd and upd[0][1] == {"summary": "Renamed"}
    # comment
    com = [(p, b) for p, b in ArticleWriteHandler.requests
           if p.split("?")[0] == "/api/articles/KB-A-9/comments"]
    assert com and com[0][1] == {"text": "Nice"}


def test_article_create_requires_project_or_parent(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-create", "--summary", "Orphan")
    assert result.returncode == 6
    assert "--project or --parent" in result.stderr


def test_article_update_requires_a_field(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-update", "KB-A-9")
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
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-create",
                     "--summary", "blocked", "--project", "0-6")
    assert result.returncode == 4
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "read_only"


def test_article_create_rejects_both_project_and_parent(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-create",
                     "--summary", "X", "--project", "0-6", "--parent", "KB-A-1")
    assert result.returncode == 6
    assert "not both" in result.stderr


def test_article_update_rejects_empty_summary(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-update",
                     "KB-A-9", "--summary", "")
    assert result.returncode == 6
    assert "non-empty" in result.stderr


def test_article_comment_rejects_empty_text(tmp_path):
    result = run_cli(tmp_path, "http://127.0.0.1:1", "article-comment",
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
        result = run_cli(tmp_path, base_url, "article", "KB-NOPE")
    finally:
        server.shutdown()
        thread.join()
    assert result.returncode == 3

#!/usr/bin/env python3
"""Regression tests for vapic's narrow Vapi CLI decode fallback.

Run with: python3 capabilities/vapic/tests/test_resilience.py
"""

from __future__ import annotations

from contextlib import contextmanager
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterator


REPO = Path(__file__).resolve().parents[3]
VAPIC = REPO / "capabilities" / "vapic" / "bin" / "vapic"
SECRET = "vapic-test-secret-token"


def _write_fake_vapi(bin_dir: Path) -> Path:
    path = bin_dir / "vapi"
    path.write_text("""#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("FAKE_VAPI_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "argv": sys.argv[1:],
            "argv_text": " ".join(sys.argv[1:]),
            "has_env_token": bool(os.environ.get("VAPI_API_KEY")),
        }) + "\\n")

mode = os.environ.get("FAKE_VAPI_MODE", "success")
if mode == "success":
    sys.stdout.write(os.environ.get("FAKE_VAPI_STDOUT", "native ok\\n"))
    sys.stderr.write(os.environ.get("FAKE_VAPI_STDERR", ""))
    sys.exit(int(os.environ.get("FAKE_VAPI_RC", "0")))
if mode == "assistant_unmarshal":
    sys.stderr.write(
        "Error: failed to list assistants: json: cannot unmarshal array into "
        "Go struct field .embed.keypadInputPlan.delimiters of type "
        "api.KeypadInputPlanDelimiters\\n")
    sys.exit(1)
if mode == "call_unmarshal":
    sys.stderr.write(
        "Error: failed to list calls: json: cannot unmarshal string into "
        "Go struct field embed.phoneNumber of type api.unmarshaler\\n")
    sys.exit(1)
if mode == "unrelated":
    sys.stderr.write("Error: ordinary upstream failure\\n")
    sys.exit(42)

sys.stderr.write(f"unknown fake mode: {mode}\\n")
sys.exit(99)
""")
    path.chmod(0o755)
    return path


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.seen.append({  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        status, body = self.server.routes.get(  # type: ignore[attr-defined]
            self.path, (404, {"error": "not found"}))
        payload = body if isinstance(body, str) else json.dumps(body)
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _fmt: str, *_args: object) -> None:
        return


@contextmanager
def _server(routes: dict[str, tuple[int, object]]) -> Iterator[tuple[str, list[dict]]]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.routes = routes  # type: ignore[attr-defined]
    srv.seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}", srv.seen  # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _env(tmp: Path, bin_dir: Path, log: Path, **extra: str) -> tuple[dict[str, str], Path]:
    home = tmp / "home"
    project = tmp / "project"
    (project / ".git").mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        "CLAUDE_PROJECT_DIR": str(project),
        "VAPI_API_KEY": SECRET,
        "FAKE_VAPI_LOG": str(log),
    })
    env.update(extra)
    return env, project


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VAPIC), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _fake_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _assert_no_secret(proc: subprocess.CompletedProcess) -> None:
    assert SECRET not in proc.stdout
    assert SECRET not in proc.stderr
    assert SECRET not in " ".join(proc.args)


def test_native_success_stays_native() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (200, [{"id": "rest"}])}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="success",
                FAKE_VAPI_STDOUT="native assistants\n",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["assistant", "list"], env, project)
        assert proc.returncode == 0
        assert proc.stdout == "native assistants\n"
        assert proc.stderr == ""
        assert seen == []
        rows = _fake_log(log)
        assert rows == [{"argv": ["assistant", "list"],
                         "argv_text": "assistant list",
                         "has_env_token": True}]
        _assert_no_secret(proc)


def test_assistant_unmarshal_falls_back_to_rest() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (200, [{"id": "from-rest"}])}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="assistant_unmarshal",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["assistant", "list"], env, project)
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == [{"id": "from-rest"}]
        assert proc.stderr == ""
        assert seen == [{"path": "/assistant",
                         "authorization": f"Bearer {SECRET}"}]
        assert _fake_log(log)[0]["argv"] == ["assistant", "list"]
        assert SECRET not in _fake_log(log)[0]["argv_text"]
        _assert_no_secret(proc)


def test_call_unmarshal_falls_back_to_rest() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/call": (200, [{"id": "call-from-rest"}])}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="call_unmarshal",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["call", "list"], env, project)
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == [{"id": "call-from-rest"}]
        assert proc.stderr == ""
        assert seen == [{"path": "/call",
                         "authorization": f"Bearer {SECRET}"}]
        _assert_no_secret(proc)


def test_unrelated_error_is_passthrough() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (200, [{"id": "rest"}])}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="unrelated",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["assistant", "list"], env, project)
        assert proc.returncode == 42
        assert proc.stdout == ""
        assert proc.stderr == "Error: ordinary upstream failure\n"
        assert seen == []
        _assert_no_secret(proc)


def test_http_failure_is_environment_category_without_secret() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (500, {"echo": SECRET})}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="assistant_unmarshal",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["assistant", "list"], env, project)
        assert proc.returncode == 5
        err = json.loads(proc.stderr)
        assert err["error"]["code"] == "rest_http_error"
        assert seen == [{"path": "/assistant",
                         "authorization": f"Bearer {SECRET}"}]
        _assert_no_secret(proc)


def test_auth_failure_is_auth_category_without_secret() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (401, {"echo": SECRET})}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="assistant_unmarshal",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["assistant", "list"], env, project)
        assert proc.returncode == 2
        err = json.loads(proc.stderr)
        assert err["error"]["code"] == "auth_failed"
        assert seen == [{"path": "/assistant",
                         "authorization": f"Bearer {SECRET}"}]
        _assert_no_secret(proc)


def test_doctor_uses_resilient_probe() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_fake_vapi(bin_dir)
        log = tmp / "fake.log"
        with _server({"/assistant": (200, [{"id": "from-rest"}])}) as (base, seen):
            env, project = _env(
                tmp, bin_dir, log,
                FAKE_VAPI_MODE="assistant_unmarshal",
                VAPI_API_BASE_URL=base,
            )
            proc = _run(["doctor"], env, project)
        assert proc.returncode == 0
        body = json.loads(proc.stdout)
        assert body["ok"] is True
        assert body["connections"]["default"]["ok"] is True
        assert "GET /assistant" in body["connections"]["default"]["probe"]
        assert seen == [{"path": "/assistant",
                         "authorization": f"Bearer {SECRET}"}]
        _assert_no_secret(proc)


if __name__ == "__main__":
    import traceback

    tests = [
        test_native_success_stays_native,
        test_assistant_unmarshal_falls_back_to_rest,
        test_call_unmarshal_falls_back_to_rest,
        test_unrelated_error_is_passthrough,
        test_http_failure_is_environment_category_without_secret,
        test_auth_failure_is_auth_category_without_secret,
        test_doctor_uses_resilient_probe,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)

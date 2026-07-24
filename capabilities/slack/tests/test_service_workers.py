import os
import stat
import pytest
from workers import worker_stub, run_worker_proc, WorkerTimeout, WORKERS


def test_stub_echoes_last_prompt_line():
    out = worker_stub("ctx\n--- Conversation ---\nAlice: hello there",
                      cwd=".", env={}, timeout=5)
    assert out["reply"].startswith("[stub]")
    assert "hello there" in out["reply"]
    assert out["meta"]["harness"] == "stub"


def test_workers_registry_has_all_three():
    assert set(WORKERS) == {"stub", "claude", "codex"}


def test_run_worker_proc_success():
    import os
    rc, out, err = run_worker_proc(["/bin/echo", "hi"], cwd=".", env=dict(os.environ),
                                   timeout=10)
    assert rc == 0
    assert out.strip() == "hi"


def test_run_worker_proc_times_out_and_raises():
    import os
    with pytest.raises(WorkerTimeout):
        run_worker_proc(["/bin/sleep", "5"], cwd=".", env=dict(os.environ), timeout=1)


def _fake_bin(tmp_path, name, script):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def test_worker_claude_raises_on_nonzero_exit(tmp_path, monkeypatch):
    from workers import worker_claude
    d = _fake_bin(tmp_path, "claude", "#!/bin/sh\necho boom >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_claude_raises_on_is_error(tmp_path, monkeypatch):
    from workers import worker_claude
    d = _fake_bin(tmp_path, "claude", '#!/bin/sh\necho \'{"is_error": true, "result": ""}\'\n')
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_claude_success_parses_result(tmp_path, monkeypatch):
    from workers import worker_claude
    d = _fake_bin(tmp_path, "claude",
                  '#!/bin/sh\necho \'{"result": "hi there", "total_cost_usd": 0.01}\'\n')
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    out = worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)
    assert out["reply"] == "hi there"
    assert out["meta"]["harness"] == "claude"


def test_worker_codex_raises_on_nonzero_exit(tmp_path, monkeypatch):
    from workers import worker_codex
    d = _fake_bin(tmp_path, "codex", "#!/bin/sh\necho boom >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_codex("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_codex_raises_on_empty_output(tmp_path, monkeypatch):
    from workers import worker_codex
    d = _fake_bin(tmp_path, "codex", "#!/bin/sh\nexit 0\n")   # exits 0 but writes nothing to -o
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_codex("p", cwd=".", env=dict(os.environ), timeout=10)

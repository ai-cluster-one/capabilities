from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source-repo"
    _init_repo(source)
    (source / "bin").mkdir()
    shutil.copy2(MANAGER, source / "bin" / "capabilities")
    shutil.copy2(REPO / "capabilities.repo.json", source / "capabilities.repo.json")
    for name in ("SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md",
                 "ROUTINES.md"):
        shutil.copy2(REPO / name, source / name)
    shutil.copytree(REPO / "contract", source / "contract")
    shutil.copytree(
        REPO / "capabilities" / "deployment",
        source / "capabilities" / "deployment",
    )
    _commit_all(source, "Fixture source")
    return source


def _consumer_repo(tmp_path: Path) -> Path:
    consumer = tmp_path / "consumer-repo"
    _init_repo(consumer)
    settings = consumer / "capabilities" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "capabilities": {"deployment": {"enabled": True}},
            }
        )
        + "\n"
    )
    (consumer / "README.md").write_text("consumer\n")
    _commit_all(consumer, "Fixture consumer")
    (consumer / ".env").write_text("REAL_SECRET=stays-in-live-checkout\n")
    return consumer


def _release_source_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "release-source"
    _init_repo(source)
    (source / "bin").mkdir()
    shutil.copy2(MANAGER, source / "bin" / "capabilities")
    shutil.copy2(REPO / "capabilities.repo.json", source / "capabilities.repo.json")
    for name in ("SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md",
                 "ROUTINES.md"):
        shutil.copy2(REPO / name, source / name)
    shutil.copytree(REPO / "contract", source / "contract")
    shutil.copytree(
        REPO / "capabilities" / "askproject",
        source / "capabilities" / "askproject",
    )
    (source / ".capability-source").mkdir()
    (source / ".capability-source" / "catalog.json").write_text(
        json.dumps({"capabilities": {}}) + "\n"
    )
    (source / "README.md").write_text("release fixture\n")
    index_env = _env(tmp_path / "release-index")
    indexed = subprocess.run(
        [str(source / "bin" / "capabilities"), "source", "index", "official"],
        cwd=source, env=index_env, text=True, capture_output=True, timeout=300,
        check=False,
    )
    assert indexed.returncode == 0, indexed.stderr
    _commit_all(source, "Initial releasable source")
    remote = tmp_path / "release-remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch", "main")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    return source, remote


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "outer-home"),
            "CAPABILITIES_HOME": str(tmp_path / "outer-registry"),
            "CAPABILITIES_BIN": str(tmp_path / "outer-bin"),
            "XDG_CONFIG_HOME": str(tmp_path / "outer-config"),
            "XDG_STATE_HOME": str(tmp_path / "outer-state"),
            "XDG_DATA_HOME": str(tmp_path / "outer-data"),
            "XDG_CACHE_HOME": str(tmp_path / "outer-cache"),
        }
    )
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _run(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(MANAGER), *args],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"capabilities {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _error(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stderr.splitlines()[-1])["error"]


def _start(
    env: dict[str, str], source: Path, consumer: Path, session: str = "deployment-test"
) -> dict:
    return json.loads(
        _run(
            env,
            "dev",
            "start",
            "deployment",
            "--source",
            str(source),
            "--consumer",
            str(consumer),
            "--session",
            session,
        ).stdout
    )


def test_dev_session_isolates_worktrees_environment_and_untracked_files(tmp_path):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    source_head = _git(source, "rev-parse", "HEAD").stdout.strip()
    consumer_head = _git(consumer, "rev-parse", "HEAD").stdout.strip()

    started = _start(env, source, consumer)
    source_worktree = Path(started["source_worktree"])
    consumer_worktree = Path(started["consumer_worktree"])
    assert source_worktree != source
    assert consumer_worktree != consumer
    assert not (consumer_worktree / ".env").exists()
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert _git(consumer, "rev-parse", "HEAD").stdout.strip() == consumer_head

    env["DEV_TEST_SECRET"] = "only-when-named"
    probe = (
        "import json,os,sys; "
        "print(json.dumps({'cwd':os.getcwd(),'home':os.environ.get('HOME'),"
        "'cache':os.environ.get('XDG_CACHE_HOME'),"
        "'uv_cache':os.environ.get('UV_CACHE_DIR'),"
        "'project_dir':os.environ.get('CLAUDE_PROJECT_DIR'),"
        "'secret':os.environ.get('DEV_TEST_SECRET'),'argv':sys.argv[1:]}))"
    )
    isolated = json.loads(
        _run(
            env,
            "dev",
            "exec",
            "deployment-test",
            "--cwd",
            "source",
            "--",
            "python3",
            "-c",
            probe,
            "--cwd",
            "consumer",
        ).stdout
    )
    assert isolated["cwd"] == str(source_worktree)
    assert isolated["home"] != env["HOME"]
    assert isolated["cache"] == started["isolated"]["cache"]
    assert isolated["uv_cache"] == str(
        Path(started["isolated"]["cache"]) / "uv"
    )
    assert isolated["project_dir"] is None
    assert isolated["secret"] is None
    assert isolated["argv"] == ["--cwd", "consumer"]

    inherited = json.loads(
        _run(
            env,
            "dev",
            "exec",
            "deployment-test",
            "--inherit-env",
            "DEV_TEST_SECRET",
            "--",
            "python3",
            "-c",
            probe,
        ).stdout
    )
    assert inherited["cwd"] == str(consumer_worktree)
    assert inherited["project_dir"] == str(consumer_worktree)
    assert inherited["secret"] == "only-when-named"

    managed = _run(
        env,
        "dev",
        "exec",
        "deployment-test",
        "--inherit-env",
        "HOME",
        "--",
        "python3",
        "-c",
        probe,
        check=False,
    )
    assert managed.returncode == 6
    assert _error(managed)["code"] == "managed_env_key"

    doctor = json.loads(_run(env, "dev", "doctor", "deployment-test").stdout)
    assert doctor["ok"] is True
    assert doctor["cleanup_ready"] is True
    assert doctor["source"]["dirty"] is False
    assert doctor["environment"]["cache"] == started["isolated"]["cache"]
    assert doctor["environment"]["logs"] == started["isolated"]["logs"]

    stopped = json.loads(_run(env, "dev", "stop", "deployment-test").stdout)
    assert stopped == {"removed": True, "session": "deployment-test"}
    assert not source_worktree.exists()
    assert not consumer_worktree.exists()
    assert not _git(
        source,
        "show-ref",
        "--verify",
        "refs/heads/dev/deployment-test",
        check=False,
    ).stdout


def test_dev_install_uses_session_registry_and_refuses_canonical_fallback(tmp_path):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    canonical_mailbox = Path(env["CAPABILITIES_HOME"]) / "mailbox"
    canonical_mailbox.mkdir(parents=True)
    (canonical_mailbox / "mailbox").write_text("#!/bin/sh\nexit 0\n")

    started = _start(env, source, consumer)
    installed = json.loads(
        _run(
            env,
            "dev",
            "install",
            "deployment-test",
            "deployment",
        ).stdout
    )
    assert installed["ok"] is True
    dev_registry = Path(started["isolated"]["registry"])
    dev_bin = Path(started["isolated"]["bin"])
    assert (dev_registry / "deployment" / "deployment").is_file()
    assert (dev_bin / "deployment").resolve() == (
        dev_registry / "deployment" / "deployment"
    ).resolve()
    assert not (Path(env["CAPABILITIES_HOME"]) / "deployment").exists()

    help_result = _run(
        env,
        "dev",
        "exec",
        "deployment-test",
        "--",
        "deployment",
        "help",
    )
    assert "deployment" in help_result.stdout.lower()

    refused = _run(
        env,
        "dev",
        "exec",
        "deployment-test",
        "--",
        "mailbox",
        "help",
        check=False,
    )
    assert refused.returncode == 6
    assert _error(refused)["code"] == "dev_capability_not_installed"

    _run(env, "dev", "stop", "deployment-test")


def test_dev_stop_preserves_dirty_and_unmerged_work_then_allows_merged_cleanup(
    tmp_path,
):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = _start(env, source, consumer)
    source_worktree = Path(started["source_worktree"])

    marker = source_worktree / "development.txt"
    marker.write_text("uncommitted\n")
    dirty = _run(env, "dev", "stop", "deployment-test", check=False)
    assert dirty.returncode == 6
    assert "dirty worktree" in dirty.stderr
    assert source_worktree.is_dir()

    _commit_all(source_worktree, "Prepared capability change")
    unmerged = _run(env, "dev", "stop", "deployment-test", check=False)
    assert unmerged.returncode == 6
    assert "unpublished commits" in unmerged.stderr
    assert source_worktree.is_dir()

    prepared = _git(source_worktree, "rev-parse", "HEAD").stdout.strip()
    (source / "main-only.txt").write_text("main advanced\n")
    _commit_all(source, "Advance main independently")
    _git(source, "cherry-pick", prepared)
    stopped = json.loads(_run(env, "dev", "stop", "deployment-test").stdout)
    assert stopped["removed"] is True
    assert (source / "development.txt").read_text() == "uncommitted\n"


def test_dev_start_preflights_both_repositories_before_creating_worktrees(tmp_path):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    _git(consumer, "branch", "dev/deployment-test-consumer")

    refused = _run(
        env,
        "dev",
        "start",
        "deployment",
        "--source",
        str(source),
        "--consumer",
        str(consumer),
        "--session",
        "deployment-test",
        check=False,
    )
    assert refused.returncode == 6
    assert _error(refused)["code"] == "branch_exists"
    assert (
        _git(
            source,
            "show-ref",
            "--verify",
            "refs/heads/dev/deployment-test",
            check=False,
        ).returncode
        != 0
    )
    assert not (
        tmp_path / "outer-state" / "capabilities" / "dev" / "deployment-test"
    ).exists()
    assert not (
        tmp_path / "outer-data" / "capabilities" / "dev" / "deployment-test"
    ).exists()


def test_dev_gc_removes_old_safe_sessions_and_retains_dirty_work(tmp_path):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    safe = _start(env, source, consumer, "safe-session")
    dirty = _start(env, source, consumer, "dirty-session")
    dirty_marker = Path(dirty["source_worktree"]) / "unfinished.txt"
    dirty_marker.write_text("keep me\n")

    session_root = Path(env["XDG_STATE_HOME"]) / "capabilities" / "dev"
    for session_id in ("safe-session", "dirty-session"):
        state_file = session_root / session_id / "session.json"
        state = json.loads(state_file.read_text())
        state["created_at"] = 0
        state_file.write_text(json.dumps(state) + "\n")

    collected = json.loads(
        _run(
            env,
            "dev",
            "gc",
            "--older-than",
            "1s",
        ).stdout
    )
    assert collected["removed"] == [
        {
            "removed": True,
            "session": "safe-session",
        }
    ]
    assert collected["retained"] == [
        {
            "reason": f"dirty worktree: {dirty['source_worktree']}",
            "session": "dirty-session",
        }
    ]
    assert not Path(safe["source_worktree"]).exists()
    assert Path(dirty["source_worktree"]).is_dir()

    dirty_marker.unlink()
    _run(env, "dev", "stop", "dirty-session")


def test_dev_stop_refuses_recorded_worktree_outside_session_data(tmp_path):
    source = _source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = _start(env, source, consumer)
    session_file = (
        Path(env["XDG_STATE_HOME"])
        / "capabilities"
        / "dev"
        / "deployment-test"
        / "session.json"
    )
    session = json.loads(session_file.read_text())
    session["source"]["worktree"] = str(source)
    session_file.write_text(json.dumps(session) + "\n")

    refused = _run(env, "dev", "stop", "deployment-test", check=False)
    assert refused.returncode == 6
    assert _error(refused)["code"] == "unsafe_dev_path"
    assert source.is_dir()
    assert Path(started["source_worktree"]).is_dir()


def test_dev_start_refuses_dirty_integration_checkout(tmp_path):
    source, _remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    (source / "unfinished.txt").write_text("must move to a dev worktree\n")

    refused = _run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "dirty-source",
        check=False,
    )
    assert refused.returncode == 6
    assert _error(refused)["code"] == "source_checkout_dirty"


def test_dev_finish_releases_commit_syncs_checkout_and_removes_session(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "finish-release",
    ).stdout)
    worktree = Path(started["source_worktree"])
    (worktree / "README.md").write_text("released from the dev worktree\n")
    candidate = _commit_all(worktree, "Prepare release candidate")

    finished = json.loads(_run(
        env, "dev", "finish", "finish-release").stdout)
    assert finished["ok"] is True
    assert finished["release"]["commit"] == candidate
    assert finished["release"]["published"] is True
    assert finished["release"]["checkout_synced"] is True
    assert finished["release"]["verification_profile"] == "editorial"
    assert finished["cleanup"]["removed"] is True
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == candidate
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == candidate
    assert _git(source, "status", "--porcelain").stdout == ""
    assert not worktree.exists()
    listed = json.loads(_run(env, "dev", "list").stdout)
    assert listed["sessions"] == []


def test_dev_finish_refuses_dirty_main_before_publication(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "blocked-release",
    ).stdout)
    worktree = Path(started["source_worktree"])
    (worktree / "README.md").write_text("prepared candidate\n")
    _commit_all(worktree, "Prepare blocked candidate")
    before = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    (source / "unexpected.txt").write_text("dirty integration checkout\n")

    refused = _run(
        env, "dev", "finish", "blocked-release", check=False)
    assert refused.returncode == 6
    assert _error(refused)["code"] == "release_checkout_dirty"
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == before
    assert worktree.is_dir()


def test_dev_finish_reconciles_changed_installed_capability(tmp_path):
    source, _remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    installed = json.loads(_run(
        env, "install", "askproject", "--from", str(source)).stdout)
    registry = Path(installed["registry"])
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "payload-release",
    ).stdout)
    worktree = Path(started["source_worktree"])
    guide = worktree / "capabilities" / "askproject" / "guides" / "capability-changes.md"
    marker = "\nRelease reconciliation fixture.\n"
    guide.write_text(guide.read_text() + marker)
    _git(worktree, "add", str(guide.relative_to(worktree)))
    indexed = subprocess.run(
        [str(worktree / "bin" / "capabilities"),
         "source", "index", "official", "--staged"],
        cwd=worktree, env=env, text=True, capture_output=True, timeout=300,
        check=False,
    )
    assert indexed.returncode == 0, indexed.stderr
    candidate = _commit_all(worktree, "Change installed capability payload")

    finished = json.loads(_run(
        env, "dev", "finish", "payload-release").stdout)
    assert finished["release"]["changed_capabilities"] == ["askproject"]
    assert finished["release"]["verification_profile"] == "capability"
    assert finished["release"]["installed_updates"][0]["name"] == "askproject"
    assert marker.strip() in (
        registry / "guides" / "capability-changes.md"
    ).read_text()
    meta = json.loads((registry / "meta.json").read_text())
    assert meta["source_commit"] == candidate
    assert meta["source_dirty"] is False


def test_dev_finish_removes_deleted_installed_capability_and_resumes(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    installed = json.loads(_run(
        env, "install", "askproject", "--from", str(source)).stdout)
    registry = Path(installed["registry"])
    link = Path(installed["symlink"])
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "payload-removal",
    ).stdout)
    worktree = Path(started["source_worktree"])
    _git(worktree, "rm", "-r", "capabilities/askproject")
    (worktree / "capabilities").mkdir()
    (worktree / "capabilities" / ".gitkeep").write_text("")
    _git(worktree, "add", "capabilities/.gitkeep")
    indexed = subprocess.run(
        [str(worktree / "bin" / "capabilities"),
         "source", "index", "official", "--staged"],
        cwd=worktree, env=env, text=True, capture_output=True, timeout=300,
        check=False,
    )
    assert indexed.returncode == 0, indexed.stderr
    candidate = _commit_all(worktree, "Remove installed capability payload")

    agents_dir = Path(env["HOME"]) / ".agents"
    agents_dir.mkdir(parents=True)
    blocked_skills = agents_dir / "skills"
    blocked_skills.write_text("force one post-removal interruption\n")
    interrupted = _run(
        env, "dev", "finish", "payload-removal", check=False)
    assert interrupted.returncode != 0
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == candidate
    assert not registry.exists()
    assert not link.exists()
    assert worktree.is_dir()

    blocked_skills.unlink()
    finished = json.loads(_run(
        env, "dev", "finish", "payload-removal").stdout)
    release = finished["release"]
    assert release["commit"] == candidate
    assert release["changed_capabilities"] == ["askproject"]
    assert release["installed_updates"] == []
    assert release["installed_removals"] == [{
        "name": "askproject",
        "action": "remove",
        "outcome": "applied",
        "removed": True,
        "registry_removed": True,
        "symlink_removed": True,
    }]
    assert release["installed_skipped"] == []
    assert not registry.exists()
    assert not link.exists()


def test_dev_finish_refuses_remote_race_without_touching_session(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "remote-race",
    ).stdout)
    worktree = Path(started["source_worktree"])
    (worktree / "README.md").write_text("candidate from original base\n")
    candidate = _commit_all(worktree, "Prepare candidate before remote race")

    peer = tmp_path / "peer"
    _git(tmp_path, "clone", str(remote), str(peer))
    (peer / "peer.txt").write_text("remote advanced independently\n")
    _commit_all(peer, "Advance remote independently")
    _git(peer, "push", "origin", "main")
    remote_head = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    assert remote_head != candidate

    refused = _run(env, "dev", "finish", "remote-race", check=False)
    assert refused.returncode == 6
    assert _error(refused)["code"] == "release_remote_diverged"
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == remote_head
    assert worktree.is_dir()


def test_dev_finish_resumes_after_remote_integrity_gate(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "pending-gate",
    ).stdout)
    worktree = Path(started["source_worktree"])
    (worktree / "README.md").write_text("candidate waits for integrity status\n")
    candidate = _commit_all(worktree, "Prepare gated candidate")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  if [ \"$ref\" = refs/heads/main ]; then\n"
        "    echo 'required status pending' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    pending = _run(env, "dev", "finish", "pending-gate", check=False)
    assert pending.returncode == 5
    assert _error(pending)["code"] == "release_pending"
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() != candidate
    assert _git(
        remote, "rev-parse", f"refs/heads/capabilities-release/{candidate[:12]}"
    ).stdout.strip() == candidate
    assert worktree.is_dir()

    hook.unlink()
    finished = json.loads(_run(
        env, "dev", "finish", "pending-gate").stdout)
    assert finished["ok"] is True
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == candidate
    assert _git(
        remote,
        "show-ref",
        "--verify",
        f"refs/heads/capabilities-release/{candidate[:12]}",
        check=False,
    ).returncode != 0
    assert not worktree.exists()


def test_pending_release_preserves_newer_local_installation(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    installed = json.loads(_run(
        env, "install", "askproject", "--from", str(source)).stdout)
    registry = Path(installed["registry"])
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "superseded-removal",
    ).stdout)
    worktree = Path(started["source_worktree"])
    _git(worktree, "rm", "-r", "capabilities/askproject")
    (worktree / "capabilities").mkdir()
    (worktree / "capabilities" / ".gitkeep").write_text("")
    _git(worktree, "add", "capabilities/.gitkeep")
    indexed = subprocess.run(
        [str(worktree / "bin" / "capabilities"),
         "source", "index", "official", "--staged"],
        cwd=worktree, env=env, text=True, capture_output=True, timeout=300,
        check=False,
    )
    assert indexed.returncode == 0, indexed.stderr
    candidate = _commit_all(worktree, "Remove capability after integrity gate")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  if [ \"$ref\" = refs/heads/main ]; then exit 1; fi\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    pending = _run(
        env, "dev", "finish", "superseded-removal", check=False)
    assert pending.returncode == 5
    assert _error(pending)["code"] == "release_pending"

    replacement = tmp_path / "replacement-askproject"
    shutil.copytree(source / "capabilities" / "askproject", replacement)
    guide = replacement / "guides" / "capability-changes.md"
    marker = "\nNewer local installation wins over pending release.\n"
    guide.write_text(guide.read_text() + marker)
    _run(env, "install", "askproject", "--from", str(replacement))
    hook.unlink()

    finished = json.loads(_run(
        env, "dev", "finish", "superseded-removal").stdout)
    release = finished["release"]
    assert release["commit"] == candidate
    assert release["installed_removals"] == []
    assert release["installed_skipped"] == [{
        "name": "askproject",
        "action": "remove",
        "outcome": "superseded",
        "reason": "local_installation_changed",
    }]
    assert registry.is_dir()
    assert marker.strip() in (registry / "guides" / "capability-changes.md").read_text()


def test_pending_release_does_not_reinstall_explicitly_removed_payload(tmp_path):
    source, remote = _release_source_repo(tmp_path)
    consumer = _consumer_repo(tmp_path)
    env = _env(tmp_path)
    installed = json.loads(_run(
        env, "install", "askproject", "--from", str(source)).stdout)
    registry = Path(installed["registry"])
    link = Path(installed["symlink"])
    started = json.loads(_run(
        env,
        "dev", "start", "askproject",
        "--source", str(source),
        "--consumer", str(consumer),
        "--session", "superseded-update",
    ).stdout)
    worktree = Path(started["source_worktree"])
    guide = worktree / "capabilities" / "askproject" / "guides" / "capability-changes.md"
    guide.write_text(guide.read_text() + "\nPending update fixture.\n")
    _git(worktree, "add", str(guide.relative_to(worktree)))
    indexed = subprocess.run(
        [str(worktree / "bin" / "capabilities"),
         "source", "index", "official", "--staged"],
        cwd=worktree, env=env, text=True, capture_output=True, timeout=300,
        check=False,
    )
    assert indexed.returncode == 0, indexed.stderr
    candidate = _commit_all(worktree, "Update capability before integrity gate")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  if [ \"$ref\" = refs/heads/main ]; then exit 1; fi\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    pending = _run(
        env, "dev", "finish", "superseded-update", check=False)
    assert pending.returncode == 5
    assert _error(pending)["code"] == "release_pending"
    _run(env, "uninstall", "askproject")
    hook.unlink()

    finished = json.loads(_run(
        env, "dev", "finish", "superseded-update").stdout)
    release = finished["release"]
    assert release["commit"] == candidate
    assert release["installed_updates"] == []
    assert release["installed_skipped"] == [{
        "name": "askproject",
        "action": "update",
        "outcome": "superseded",
        "reason": "capability_uninstalled",
    }]
    assert not registry.exists()
    assert not link.exists()

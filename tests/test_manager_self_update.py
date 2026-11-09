#!/usr/bin/env python3
"""Manager releases are described by the release being installed."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
MANIFEST_REL = Path(".capability-source") / "manager-release.json"
SCHEMA = "capabilities.manager-release.v1"


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CAPABILITIES_BIN": str(tmp_path / "bin"),
    })
    return env


def _write_release(tmp_path: Path) -> tuple[Path, dict]:
    release = tmp_path / "incoming"
    manager = release / "bin" / "capabilities"
    manager.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, manager)
    assets = {
        "SHEBANG.md": (REPO / "SHEBANG.md").read_bytes(),
        "contract/CHANGELOG.md": (REPO / "contract" / "CHANGELOG.md").read_bytes(),
        # Deliberately absent from the outgoing manager constant. This is the
        # next new asset that used to require two self-update calls.
        "runtime/future-required.txt": b"future runtime dependency\n",
    }
    for rel, body in assets.items():
        target = release / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    manifest = {
        "schema": SCHEMA,
        "manager": {
            "path": "bin/capabilities",
            "sha256": hashlib.sha256(manager.read_bytes()).hexdigest(),
        },
        "assets": {
            rel: hashlib.sha256(body).hexdigest()
            for rel, body in assets.items()
        },
    }
    manifest_path = release / MANIFEST_REL
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return release, manifest


def _legacy_install(tmp_path: Path, env: dict[str, str]) -> tuple[Path, Path]:
    legacy = Path(env["CAPABILITIES_HOME"]) / ".manager" / "capabilities"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(MANAGER.read_bytes() + b"\n# outgoing manager\n")
    legacy.chmod(0o755)
    link = Path(env["CAPABILITIES_BIN"]) / "capabilities"
    link.parent.mkdir(parents=True)
    link.symlink_to(legacy)
    return legacy, link


def _run(env: dict[str, str], *args: str, check: bool = True):
    result = subprocess.run(
        [str(MANAGER), *args], cwd=REPO, env=env,
        text=True, capture_output=True, timeout=120,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"capabilities {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _error(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


def test_incoming_manifest_can_add_an_asset_the_outgoing_manager_does_not_know(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    legacy, link = _legacy_install(tmp_path, env)
    release, manifest = _write_release(tmp_path)

    updated = json.loads(_run(
        env, "self-update", "--from", str(release)).stdout)

    active = link.resolve()
    release_root = active.parents[1]
    assert updated["self_update"] == "updated"
    assert active == Path(updated["manager"])
    assert active != legacy
    assert (release_root / "runtime" / "future-required.txt").read_text() == \
        "future runtime dependency\n"
    assert json.loads((release_root / MANIFEST_REL).read_text()) == manifest
    assert updated["manager_assets"] == sorted(manifest["assets"])
    listed = _run(env, "list")
    assert json.loads(listed.stdout)["installed"] == []

    current = json.loads(_run(
        env, "self-update", "--from", str(release)).stdout)
    assert current["self_update"] == "already current"
    assert link.resolve() == active


def test_current_release_is_a_true_noop_and_repairs_executable_mode(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    _legacy, link = _legacy_install(tmp_path, env)
    release, _manifest = _write_release(tmp_path)
    _run(env, "self-update", "--from", str(release))
    active = link.resolve()
    inode = link.lstat().st_ino

    unchanged = json.loads(_run(
        env, "self-update", "--from", str(release)).stdout)

    assert unchanged["self_update"] == "already current"
    assert link.lstat().st_ino == inode
    active.chmod(0o644)

    repaired = json.loads(_run(
        env, "self-update", "--from", str(release)).stdout)

    assert repaired["self_update"] == "updated"
    assert link.lstat().st_ino == inode
    assert active.stat().st_mode & 0o111
    assert _run(env, "list").returncode == 0


def test_bad_incoming_hash_cannot_switch_the_active_manager(tmp_path: Path) -> None:
    env = _env(tmp_path)
    legacy, link = _legacy_install(tmp_path, env)
    before = legacy.read_bytes()
    release, _manifest = _write_release(tmp_path)
    (release / "runtime" / "future-required.txt").write_text("tampered\n")

    refused = _run(
        env, "self-update", "--from", str(release), check=False)

    assert refused.returncode == 5
    error = _error(refused)
    assert error["code"] == "manager_release_hash_mismatch"
    assert link.resolve() == legacy
    assert legacy.read_bytes() == before
    assert not (Path(env["CAPABILITIES_HOME"]) / ".manager" / "releases").exists()


def test_remote_update_fetches_the_incoming_manifest_before_its_files(
    tmp_path: Path,
) -> None:
    release, manifest = _write_release(tmp_path)
    manager = runpy.run_path(str(MANAGER), run_name="manager_remote_release_test")
    base = "https://release.invalid/current"
    requested: list[str] = []

    class Response:
        status_code = 200

        def __init__(self, content: bytes):
            self.content = content

    class FakeHttpx:
        class HTTPError(Exception):
            pass

        @staticmethod
        def get(url: str, **_kwargs) -> Response:
            requested.append(url)
            rel = url.removeprefix(base + "/")
            return Response((release / rel).read_bytes())

    loader = manager["_manager_release_from_remote"]
    loader.__globals__["SOURCE"] = base
    content, assets, loaded, source = loader(FakeHttpx)

    assert requested[0] == f"{base}/{MANIFEST_REL.as_posix()}"
    assert requested[1] == f"{base}/bin/capabilities"
    assert set(requested[2:]) == {
        f"{base}/{rel}" for rel in manifest["assets"]}
    assert hashlib.sha256(content).hexdigest() == \
        manifest["manager"]["sha256"]
    assert set(assets) == set(manifest["assets"])
    assert loaded == manifest
    assert source == requested[0]


RAW = "https://raw.githubusercontent.com/owner/repo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REFS = "https://github.com/owner/repo.git/info/refs?service=git-upload-pack"


def _pkt(line: str) -> bytes:
    body = line.encode()
    return f"{len(body) + 4:04x}".encode() + body


@pytest.mark.parametrize("path", ["self-update", "store upgrade"])
@pytest.mark.parametrize("world", ["stale branch asset", "true mismatch", "unresolved"])
def test_a_github_release_is_read_at_one_commit(
    tmp_path: Path, monkeypatch, path: str, world: str,
) -> None:
    """raw.githubusercontent.com refreshes each file of a branch on its own,
    so right after a release the branch can serve the new manifest beside an
    old asset; the release is read at the commit the branch names instead."""
    release, manifest = _write_release(tmp_path)
    stale = "contract/CHANGELOG.md"
    at_branch = {stale: b"the release before\n"}
    at_commit = {stale: b"tampered\n"} if world == "true mismatch" else {}
    advertised = (_pkt("# service=git-upload-pack\n") + b"0000"
                  + _pkt(f"{'f' * 40} HEAD\0multi_ack symref=HEAD:refs/heads/main\n")
                  + _pkt(f"{'e' * 40} refs/heads/dev/other\n")
                  + _pkt(f"{COMMIT} refs/heads/main\n") + b"0000")
    requested: list[str] = []

    class Response:
        def __init__(self, content: bytes | None):
            self.status_code = 404 if content is None else 200
            self.content = content or b""

    class FakeHttpx:
        class HTTPError(Exception):
            pass

        @staticmethod
        def get(url: str, **_kwargs) -> Response:
            requested.append(url)
            if url == REFS:
                return Response(None if world == "unresolved" else advertised)
            for ref, override in (("main", at_branch), (COMMIT, at_commit)):
                rel = url.removeprefix(f"{RAW}/{ref}/")
                if rel != url:
                    body = override.get(rel)
                    return Response(body if body is not None else (release / rel).read_bytes())
            return Response(None)

    m = _manager_module()
    monkeypatch.setattr(m, "SOURCE", f"{RAW}/main")
    monkeypatch.setattr(m, "_httpx", lambda: FakeHttpx)
    monkeypatch.setattr(m, "_store_major_here", lambda: {"store": None, "reason": "none"})
    activated: list[tuple] = []
    if path == "self-update":
        import contextlib
        monkeypatch.setattr(m, "_manager_mutation_guard", contextlib.nullcontext)
        monkeypatch.setattr(m, "_marked_checkout", lambda _path: None)
        monkeypatch.setattr(m, "_install_carried_capabilities", lambda _manager: [])
        monkeypatch.setattr(m, "_emit", lambda _answer: None)
        monkeypatch.setattr(m, "_activate_manager_release", lambda *args: (
            activated.append(args) or {"manager": "x", "release": "x"}))
        fetched, error = m._captured(m.cmd_self_update, None)
    else:
        fetched, error = m._captured(m._manager_release_from_remote, FakeHttpx)
        if fetched is not None:
            content, assets, loaded, source = fetched
            activated.append((content, assets, source, loaded))

    if world == "true mismatch":
        assert error == {
            "code": "manager_release_hash_mismatch",
            "message": f"incoming manager asset failed its release hash: {stale}",
            "hint": f"{RAW}/main/{MANIFEST_REL.as_posix()}: expected "
                    f"{manifest['assets'][stale]}, got "
                    f"{hashlib.sha256(at_commit[stale]).hexdigest()}"}
        return
    if world == "unresolved":
        # Without the commit the branch is read as given, as before, and the
        # release hashes still guard it.
        assert error is not None and error["code"] == "manager_release_hash_mismatch"
        assert not any(url.startswith(f"{RAW}/{COMMIT}/") for url in requested)
        return
    assert error is None, error
    content, assets, loaded = activated[0][0], activated[0][1], activated[0][-1]
    assert loaded == manifest
    assert hashlib.sha256(content).hexdigest() == manifest["manager"]["sha256"]
    assert assets[stale] == (release / stale).read_bytes()
    files = [url for url in requested if url != REFS]
    assert files and all(url.startswith(f"{RAW}/{COMMIT}/") for url in files)
    assert requested.count(REFS) == 1


def test_manifest_rejects_conflicting_release_paths() -> None:
    manager = runpy.run_path(str(MANAGER), run_name="manager_release_paths_test")
    validate = manager["_validate_manager_release_manifest"]
    manifest = {
        "schema": SCHEMA,
        "manager": {"path": "bin/capabilities", "sha256": "0" * 64},
        "assets": {
            "bin": "1" * 64,
            "runtime/one": "2" * 64,
            "runtime/one/child": "3" * 64,
            "bin/Capabilities": "4" * 64,
        },
    }

    problems = validate(manifest)

    assert "release manifest paths conflict: 'bin/capabilities' and 'bin'" \
        in problems
    assert "release manifest paths conflict: 'runtime/one' and " \
        "'runtime/one/child'" in problems
    assert "release manifest paths conflict: 'bin/capabilities' and " \
        "'bin/Capabilities'" in problems


def test_repository_manager_release_manifest_matches_the_source_tree() -> None:
    manager = runpy.run_path(str(MANAGER), run_name="manager_release_test")
    expected, problems = manager["_manager_release_payload"](REPO)

    assert problems == []
    assert json.loads((REPO / MANIFEST_REL).read_text()) == expected


def _manager_module():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_self_update", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_old_manager_releases_are_pruned_and_the_ones_a_running_manager_may_use_are_kept(
    tmp_path: Path, monkeypatch,
) -> None:
    m = _manager_module()
    releases = tmp_path / "releases"
    staged = {}
    for index, at in enumerate((100, 200, 300, 400, 500)):
        release = releases / (f"{index:x}" * 64)
        release.mkdir(parents=True)
        os.utime(release, (at, at))
        staged[at] = release
    staging = releases / ".manager-release-x"
    staging.mkdir()

    # A manager process started at 350 may run the release staged at 300.
    monkeypatch.setattr(m, "_manager_process_start", lambda: 350)
    removed = m._prune_manager_releases(releases, staged[500], staged[400])
    assert removed == [staged[100].name, staged[200].name]
    assert sorted(p.name for p in releases.iterdir()) == sorted(
        [staged[300].name, staged[400].name, staged[500].name, staging.name])

    # With no manager running, only the active release and the one it replaced stay.
    monkeypatch.setattr(m, "_manager_process_start", lambda: None)
    assert m._prune_manager_releases(releases, staged[500], staged[400]) == [staged[300].name]
    assert sorted(p.name for p in releases.iterdir()) == sorted(
        [staged[400].name, staged[500].name, staging.name])

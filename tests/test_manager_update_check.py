#!/usr/bin/env python3
"""Check-only update and self-update answer what would change, changing nothing."""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
BUNDLE = REPO / "capabilities" / "resend"
MANIFEST_REL = Path(".capability-source") / "manager-release.json"
SCHEMA = "capabilities.manager-release.v1"
TITLE = "# resend — change log\n"
OLDER = "## 2026-01-01 — Older change\n\nWhat the installed copy already has.\n"
NEWER = "## 2026-02-01 — Newer change\n\nWhat arrived after the install.\n"


def _env(tmp: Path) -> dict[str, str]:
    home = tmp / "home"
    cap_home = home / ".capabilities"
    bin_dir = tmp / "bin"
    for path in (home, cap_home, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CAPABILITIES_HOME": str(cap_home),
        "CAPABILITIES_BIN": str(bin_dir),
        "CAPABILITIES_SOURCE": "http://127.0.0.1:9",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def _ok(args: list[str], env: dict[str, str], cwd: Path) -> dict:
    proc = _run(args, env, cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _error(proc: subprocess.CompletedProcess) -> dict:
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return json.loads(lines[-1])["error"]


def _tree(root: Path) -> dict[str, str]:
    """Every file under root with its hash, and every symlink with its target."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "-> " + os.readlink(path)
        elif path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@contextmanager
def _serve(root: Path):
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _install_from_copy(tmp: Path, env: dict[str, str]) -> Path:
    source = tmp / "src" / "resend"
    shutil.copytree(BUNDLE, source)
    (source / "CHANGELOG.md").write_text(TITLE + "\n" + OLDER)
    _ok(["install", "resend", "--from", str(source)], env, tmp)
    return source


def _row(result: dict, name: str) -> dict:
    return next(row for row in result["checked"] if row["name"] == name)


def test_a_local_source_answers_current_then_behind_and_nothing_moves(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    source = _install_from_copy(tmp_path, env)

    current = _ok(["update", "--check", "resend", "--json"], env, tmp_path)
    row = _row(current, "resend")
    assert row["source"] == str(source)
    assert row["update_available"] is False
    assert current["update_available"] is False
    assert row["available"]["catalog_sha256"] == row["installed"]["catalog_sha256"]
    assert row["installed"]["version"] == "2026-01-01 — Older change"
    assert row["available"]["version"] == row["installed"]["version"]
    assert "error" not in row

    (source / "CHANGELOG.md").write_text(TITLE + "\n" + NEWER + "\n" + OLDER)
    before = _tree(cap_home)
    links = _tree(Path(env["CAPABILITIES_BIN"]))

    behind = _ok(["update", "--check"], env, tmp_path)
    row = _row(behind, "resend")
    assert row["update_available"] is True
    assert behind["update_available"] is True
    assert row["available"]["catalog_sha256"] != row["installed"]["catalog_sha256"]
    assert row["installed"]["version"] == "2026-01-01 — Older change"
    assert row["available"]["version"] == "2026-02-01 — Newer change"
    assert _tree(cap_home) == before
    assert _tree(Path(env["CAPABILITIES_BIN"])) == links


def test_the_installed_copy_answers_in_the_published_catalogues_terms(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    _ok(["install", "resend", "--from", str(BUNDLE)], env, tmp_path)
    meta_path = cap_home / "resend" / "meta.json"
    published = json.loads(
        (REPO / ".capability-source" / "catalog.json").read_text()
    )["capabilities"]["resend"]["payload_sha256"]

    with _serve(REPO) as base:
        meta = json.loads(meta_path.read_text())
        meta["source"] = f"{base}/capabilities/resend/bin/resend"
        meta_path.write_text(json.dumps(meta))
        before = _tree(cap_home)
        result = _ok(["update", "--check", "resend"], env, tmp_path)

    row = _row(result, "resend")
    assert row["installed"]["catalog_sha256"] == published
    assert row["available"]["catalog_sha256"] == published
    assert row["update_available"] is False
    assert _tree(cap_home) == before


def test_an_unreachable_source_is_one_rows_error_and_exit_5(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    _install_from_copy(tmp_path, env)
    _ok(["install", "calcomc", "--from", str(REPO / "capabilities" / "calcomc")],
        env, tmp_path)
    meta_path = cap_home / "calcomc" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["source"] = "http://127.0.0.1:9/capabilities/calcomc/bin/calcomc"
    meta_path.write_text(json.dumps(meta))

    proc = _run(["update", "--check"], env, tmp_path)

    assert proc.returncode == 5, proc.stderr
    result = json.loads(proc.stdout)
    broken = _row(result, "calcomc")
    assert broken["available"] is None
    assert broken["update_available"] is False
    assert broken["error"]["code"] == "network_error"
    answered = _row(result, "resend")
    assert answered["update_available"] is False
    assert "error" not in answered

    missing = _run(["update", "--check", "ghost"], env, tmp_path)
    assert missing.returncode == 3
    assert _error(missing)["code"] == "not_found"


def _uv_holding_an_older_contract_library(tmp: Path, env: dict[str, str]) -> Path:
    """A uv whose script environments hold capabilities-contract 0.48.0: each
    script path is answered with an environment of its own, made on first ask,
    and a contract-library sync moves that path's environment to the release
    it names, logging the call; every other call goes to the real uv."""
    real = shutil.which("uv", path=env["PATH"])
    root = tmp / "fake-uv"
    (root / "cache" / "environments-v2").mkdir(parents=True)
    uv = root / "uv"
    uv.write_text(f"""#!/bin/sh
envs="{root}/cache/environments-v2"
for last; do :; done
folder() {{ echo "$envs/$(basename "$1")-$(printf %s "$1" | sha256sum | cut -c1-16)"; }}
case " $* " in
  " cache dir ") echo "{root}/cache"; exit 0;;
  *" python find "*"--script "*)
    f=$(folder "$last")
    if [ ! -d "$f" ]; then
      mkdir -p "$f/bin" "$f/lib/python3.12/site-packages/capabilities_contract-0.48.0.dist-info"
      ln -s "$(command -v python3)" "$f/bin/python"
    fi
    echo "$f/bin/python"; exit 0;;
  *" sync "*"--upgrade-package capabilities-contract"*)
    echo "$*" >> "{root}/log"
    script= to= prev=
    for arg; do
      [ "$prev" = "--script" ] && script=$arg
      case "$arg" in capabilities-contract==*) to=${{arg#capabilities-contract==}};; esac
      prev=$arg
    done
    f=$(folder "$script")
    rm -rf "$f"/lib/python3.12/site-packages/capabilities_contract-*.dist-info
    mkdir -p "$f/lib/python3.12/site-packages/capabilities_contract-$to.dist-info"
    exit 0;;
esac
exec "{real}" "$@"
""")
    uv.chmod(0o755)
    env["PATH"] = str(root) + os.pathsep + env["PATH"]
    return root


def _official_catalogue(root: Path, release: str | None) -> None:
    catalog: dict = {"capabilities": {}}
    if release is not None:
        catalog["contract_library"] = release
    path = root / ".capability-source" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog))


def test_a_contract_library_release_is_an_update_only_once_the_catalogue_names_it(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    _install_from_copy(tmp_path, env)
    fake = _uv_holding_an_older_contract_library(tmp_path, env)
    official = tmp_path / "official"
    _official_catalogue(official, None)
    log = fake / "log"

    with _serve(official) as base:
        env["CAPABILITIES_SOURCE"] = base
        # A library release the catalogue does not name marks nothing behind,
        # and update at an equal hash moves nothing.
        named_none = _ok(["update", "--check", "resend"], env, tmp_path)
        assert "contract_library" not in _row(named_none, "resend")
        assert named_none["update_available"] is False
        assert _ok(["update", "resend"], env, tmp_path)["updated"] == [
            {"name": "resend", "changed": False}]
        assert not log.exists()

        _official_catalogue(official, "0.49.0")
        before = _tree(cap_home)
        behind = _ok(["update", "--check", "resend"], env, tmp_path)
        row = _row(behind, "resend")
        assert row["available"]["catalog_sha256"] == row["installed"]["catalog_sha256"]
        assert row["contract_library"] == {"installed": "0.48.0", "available": "0.49.0"}
        assert row["update_available"] is True
        assert behind["update_available"] is True
        assert _tree(cap_home) == before
        assert not log.exists()

        updated = _ok(["update", "resend"], env, tmp_path)
        assert updated["updated"] == [{
            "name": "resend", "changed": False,
            "contract_library": {"installed": "0.48.0", "available": "0.49.0"}}]
        synced = log.read_text().splitlines()
        assert len(synced) == 2
        assert all("--upgrade-package capabilities-contract==0.49.0" in line
                   for line in synced)
        assert _tree(cap_home) == before

        current = _ok(["update", "--check", "resend"], env, tmp_path)
        row = _row(current, "resend")
        assert "contract_library" not in row
        assert row["update_available"] is False
        assert current["update_available"] is False
        _ok(["update", "resend"], env, tmp_path)
        assert log.read_text().splitlines() == synced

        # A release outside the declared range is never offered.
        _official_catalogue(official, "1.0.0")
        outside = _ok(["update", "--check", "resend"], env, tmp_path)
        assert "contract_library" not in _row(outside, "resend")


class _LoggingHandler(_QuietHandler):
    requested: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - the handler's own name
        type(self).requested.append(self.path)
        super().do_GET()


def _publish_resend(root: Path, edit=None) -> None:
    """resend's bundle under `root`, as a catalogue URL serves it, with a
    catalogue entry the manager generates for it."""
    import runpy
    target = root / "capabilities" / "resend"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(BUNDLE, target)
    if edit is not None:
        edit(target)
    manager = runpy.run_path(str(MANAGER), run_name="manager_update_check_test")
    entry = manager["_catalog_entry_of"](target, "resend")
    path = root / ".capability-source" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capabilities": {"resend": entry}}))


def _uv_logging_contract_syncs(tmp: Path, env: dict[str, str]) -> Path:
    """A uv that logs each contract-library sync and answers it without
    syncing; every other call goes to the real uv."""
    real = shutil.which("uv", path=env["PATH"])
    root = tmp / "logging-uv"
    root.mkdir()
    uv = root / "uv"
    uv.write_text(f"""#!/bin/sh
case " $* " in
  *" sync "*"--upgrade-package capabilities-contract"*) echo "$*" >> "{root}/log"; exit 0;;
esac
exec "{real}" "$@"
""")
    uv.chmod(0o755)
    env["PATH"] = str(root) + os.pathsep + env["PATH"]
    return root / "log"


def test_update_does_nothing_at_an_equal_hash_and_otherwise_fetches_the_capability_alone(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    served = tmp_path / "served"
    _publish_resend(served)
    _ok(["install", "resend", "--from", str(BUNDLE)], env, tmp_path)
    log = _uv_logging_contract_syncs(tmp_path, env)
    handler = functools.partial(_LoggingHandler, directory=str(served))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        meta_path = cap_home / "resend" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["source"] = f"{base}/capabilities/resend/bin/resend"
        meta_path.write_text(json.dumps(meta))
        before = _tree(cap_home)

        equal = _ok(["update", "resend"], env, tmp_path)
        assert equal["updated"] == [{"name": "resend", "changed": False}]
        assert _LoggingHandler.requested == ["/.capability-source/catalog.json"]
        assert _tree(cap_home) == before
        assert not log.exists()

        # A change outside the script's dependency block: the capability's own
        # files alone are fetched, and no dependency is synced.
        _publish_resend(served, lambda bundle: (bundle / "CHANGELOG.md").write_text(
            TITLE + "\n" + NEWER + "\n" + OLDER))
        _LoggingHandler.requested.clear()
        changed = _ok(["update", "resend"], env, tmp_path)
        assert changed["updated"][0]["changed"] is True
        files = json.loads((served / ".capability-source" / "catalog.json").read_text())[
            "capabilities"]["resend"]["files"]
        assert sorted(_LoggingHandler.requested) == sorted(
            ["/.capability-source/catalog.json"]
            + [f"/capabilities/resend/{rel}" for rel in files])
        assert (cap_home / "resend" / "CHANGELOG.md").read_text().startswith(TITLE + "\n" + NEWER)
        assert os.access(cap_home / "resend" / "resend", os.X_OK)
        assert not log.exists()
        assert _ok(["update", "--check", "resend"], env, tmp_path)["update_available"] is False

        # A change to the dependency block syncs the dependencies.
        def requires(bundle: Path) -> None:
            script = bundle / "bin" / "resend"
            script.write_text(script.read_text().replace(
                '# requires-python = ">=3.11"', '# requires-python = ">=3.11.0"', 1))
        _publish_resend(served, requires)
        locked = _ok(["update", "resend"], env, tmp_path)
        assert locked["updated"][0]["changed"] is True
        assert len(log.read_text().splitlines()) == 2
    finally:
        server.shutdown()
        server.server_close()


def _write_release(tmp: Path) -> tuple[Path, dict]:
    release = tmp / "incoming"
    manager = release / "bin" / "capabilities"
    manager.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, manager)
    assets = {
        "SHEBANG.md": (REPO / "SHEBANG.md").read_bytes(),
        "contract/CHANGELOG.md": (REPO / "contract" / "CHANGELOG.md").read_bytes(),
        "manager/CHANGELOG.md": b"# capabilities - change log\n\n## 2026-03-01 - Incoming\n",
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
        "assets": {rel: hashlib.sha256(body).hexdigest() for rel, body in assets.items()},
    }
    manifest_path = release / MANIFEST_REL
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return release, manifest


def _legacy_install(env: dict[str, str]) -> Path:
    legacy = Path(env["CAPABILITIES_HOME"]) / ".manager" / "capabilities"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(MANAGER.read_bytes() + b"\n# outgoing manager\n")
    legacy.chmod(0o755)
    link = Path(env["CAPABILITIES_BIN"]) / "capabilities"
    link.symlink_to(legacy)
    return link


def test_self_update_check_names_both_releases_and_switches_nothing(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    cap_home = Path(env["CAPABILITIES_HOME"])
    link = _legacy_install(env)
    release, manifest = _write_release(tmp_path)
    before = _tree(cap_home)

    behind = _ok(["self-update", "--check", "--from", str(release), "--json"],
                 env, tmp_path)

    assert behind["installed"] is None
    assert behind["update_available"] is True
    assert behind["available"]["manager_sha256"] == manifest["manager"]["sha256"]
    assert behind["available"]["version"] == "2026-03-01 - Incoming"
    assert _tree(cap_home) == before
    assert not (cap_home / ".manager" / "releases").exists()

    with _serve(release) as base:
        remote = _ok(["self-update", "--check"],
                     {**env, "CAPABILITIES_SOURCE": base}, tmp_path)
    assert remote["update_available"] is True
    assert remote["available"]["release"] == behind["available"]["release"]
    assert remote["available"]["version"] == "2026-03-01 - Incoming"
    assert _tree(cap_home) == before

    _ok(["self-update", "--from", str(release)], env, tmp_path)
    active = link.resolve()
    current = _ok(["self-update", "--check", "--from", str(release)], env, tmp_path)

    assert current["update_available"] is False
    assert current["installed"]["release"] == current["available"]["release"]
    assert current["installed"]["release"] == active.parents[1].name
    assert current["installed"]["manager_sha256"] == manifest["manager"]["sha256"]
    assert current["installed"]["version"] == "2026-03-01 - Incoming"
    assert link.resolve() == active


def test_self_update_check_on_an_unreachable_source_exits_5(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _legacy_install(env)

    proc = _run(["self-update", "--check"], env, tmp_path)

    assert proc.returncode == 5
    assert _error(proc)["code"] == "network_error"
    assert not (Path(env["CAPABILITIES_HOME"]) / ".manager" / "releases").exists()

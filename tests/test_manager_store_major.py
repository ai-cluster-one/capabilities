#!/usr/bin/env python3
"""`self-update` and `update` take only a release that reads the store's major.

Every manager release and capability names the store schema major it reads
(`store_major` in the release manifest and the catalogue, 1 where a release
names none). Against the store this folder resolves, a release of the store's
major is taken; from the canonical source a release of another major gives way
to that major's line (`store-major-<n>`, the source's trailing `/main`
replaced), and where no such line answers, or the source is not canonical,
what is installed is kept. Either way the answer names `capabilities store
upgrade`. With no store, or an empty one, the newest is taken. A line naming
`library` keeps each capability environment's capabilities-contract inside it.

The stores are databases of a throwaway PostgreSQL on a loopback port, started
when `initdb` and `pg_ctl` are on PATH and psycopg is importable, and skipped
otherwise; the sources are trees served over HTTP from the test.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import runpy
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
MANIFEST_REL = ".capability-source/manager-release.json"
CATALOG_REL = ".capability-source/catalog.json"
BUNDLE = REPO / "capabilities" / "resend"
ADMIN = "pgadmin"
SCHEMA = "lab"
UPGRADE = "capabilities store upgrade"
M = runpy.run_path(str(MANAGER), run_name="manager_store_major_test")


# --- a throwaway PostgreSQL ------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.port = _free_port()
        self.env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(self, *argv: str) -> None:
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True,
                                timeout=120)
        assert result.returncode == 0, (argv, result.stdout + result.stderr)

    def start(self) -> None:
        self.run("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                 "--locale=en_US.UTF-8", "--auth=trust")
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       "unix_socket_directories = ''\n")
        self.run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "pg.log"),
                 "-w", "-t", "60", "start")

    def stop(self) -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "stop"],
                       env=self.env, capture_output=True, timeout=60)

    def store(self, versions: dict[str, int] | None) -> str:
        """A database of its own holding `versions` {owner: major} in its
        schema_version, or nothing at all for None; its URL."""
        import psycopg
        name = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                             dbname="postgres", autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
        if versions is not None:
            with psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                                 dbname=name, autocommit=True) as conn:
                conn.execute(f"CREATE SCHEMA {SCHEMA}")
                conn.execute(f"CREATE TABLE {SCHEMA}.schema_ledger (owner text NOT NULL, "
                             "step text NOT NULL, checksum text NOT NULL, applied_at "
                             "timestamptz NOT NULL DEFAULT now(), library_version text, "
                             "PRIMARY KEY (owner, step))")
                conn.execute(f"CREATE TABLE {SCHEMA}.schema_version (owner text PRIMARY KEY, "
                             "major integer NOT NULL, minor integer NOT NULL, "
                             "updated_at timestamptz NOT NULL DEFAULT now())")
                for owner, major in versions.items():
                    conn.execute(f"INSERT INTO {SCHEMA}.schema_version (owner, major, minor) "
                                 "VALUES (%s, %s, 0)", (owner, major))
        return f"postgresql://{ADMIN}@127.0.0.1:{self.port}/{name}?sslmode=disable"


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl", "uv"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-store-major-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


# --- the machine, the sources and the manager ------------------------------------

def _env(tmp: Path, url: str | None = None) -> dict[str, str]:
    home = tmp / "home"
    bin_dir = tmp / "bin"
    for path in (home, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTKIT_DB_")}
    env.update({
        "HOME": str(home),
        "CAPABILITIES_HOME": str(home / ".capabilities"),
        "CAPABILITIES_BIN": str(bin_dir),
        "CAPABILITIES_SOURCE": "http://127.0.0.1:9/main",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    if url is not None:
        env.update({"AGENTKIT_DB_URL": url, "AGENTKIT_DB_SCHEMA": SCHEMA})
    return env


def _run(env: dict[str, str], cwd: Path, *args: str) -> dict:
    proc = subprocess.run([str(MANAGER), *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


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


def _release(root: Path, tag: str, store_major: int | None) -> dict:
    """A manager release in `root` reading `store_major` (naming none for None)."""
    manager = MANAGER.read_bytes() + f"\n# the {tag} manager\n".encode()
    files = {"bin/capabilities": manager,
             "SHEBANG.md": (REPO / "SHEBANG.md").read_bytes(),
             "manager/CHANGELOG.md": f"# capabilities - change log\n\n## {tag}\n".encode()}
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    manifest = {"schema": "capabilities.manager-release.v1",
                "manager": {"path": "bin/capabilities",
                            "sha256": hashlib.sha256(manager).hexdigest()},
                "assets": {rel: hashlib.sha256(body).hexdigest()
                           for rel, body in files.items() if rel != "bin/capabilities"}}
    if store_major is not None:
        manifest["store_major"] = store_major
    (root / MANIFEST_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_REL).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _legacy(env: dict[str, str]) -> Path:
    legacy = Path(env["CAPABILITIES_HOME"]) / ".manager" / "capabilities"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(MANAGER.read_bytes() + b"\n# outgoing manager\n")
    legacy.chmod(0o755)
    link = Path(env["CAPABILITIES_BIN"]) / "capabilities"
    link.symlink_to(legacy)
    return link


def _tag(link: Path) -> str:
    return link.resolve().read_text().rstrip().splitlines()[-1]


# --- the release manifest and the catalogue name the major -----------------------

def test_the_manifest_and_the_catalogue_carry_the_store_major() -> None:
    manifest = json.loads((REPO / MANIFEST_REL).read_text())
    catalog = json.loads((REPO / CATALOG_REL).read_text())
    assert manifest["store_major"] == M["STORE_MAJOR"]
    assert catalog["store_major"] == M["STORE_MAJOR"]
    assert catalog["capabilities"]
    assert {entry["store_major"] for entry in catalog["capabilities"].values()} \
        == {M["STORE_MAJOR"]}
    assert "library" not in manifest and "library" not in catalog


def test_a_kept_line_names_its_library_and_an_older_tree_names_nothing(tmp_path: Path) -> None:
    line = tmp_path / "line"
    (line / "bin").mkdir(parents=True)
    (line / "bin" / "capabilities").write_text(
        'STORE_MAJOR = 1\nSTORE_LIBRARY: str | None = ">=0.42,<0.50"\n')
    assert M["_tree_store_line"](line) == {"store_major": 1, "library": ">=0.42,<0.50"}
    assert M["_stamp_catalog"](line, {"x": {"path": "capabilities/x"}}) == {
        "capabilities": {"x": {"path": "capabilities/x", "tier": "standard",
                               "store_major": 1}},
        "store_major": 1, "library": ">=0.42,<0.50"}
    older = tmp_path / "older"
    (older / "bin").mkdir(parents=True)
    (older / "bin" / "capabilities").write_text('CORE_CAPABILITIES = ("tasks",)\n')
    assert M["_tree_store_line"](older) == {}
    assert M["_stamp_catalog"](older, {}) == {"capabilities": {}}
    assert M["_tree_store_line"](tmp_path / "no-manager") == {"store_major": M["STORE_MAJOR"]}
    assert M["_catalogue_release"]({"capabilities": {"x": {}}}, "x") == (1, None)


# --- self-update -----------------------------------------------------------------

def test_a_release_of_the_stores_major_is_taken(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 1, "tasks": 1}))
    link = _legacy(env)
    _release(tmp_path / "src" / "main", "main", 1)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        checked = _run(env, tmp_path, "self-update", "--check")
        assert checked["update_available"] is True
        assert checked["store_major"] == {"store": 1, "release": 1, "taken": "release",
                                          "upgrade_needed": False}
        result = _run(env, tmp_path, "self-update")
    assert result["self_update"] == "updated"
    assert result["store_major"]["taken"] == "release"
    assert _tag(link) == "# the main manager"


def test_a_newer_major_gives_way_to_the_stores_line(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 1}))
    link = _legacy(env)
    _release(tmp_path / "src" / "main", "main", 2)
    line = _release(tmp_path / "src" / "store-major-1", "line", 1)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        checked = _run(env, tmp_path, "self-update", "--check")
        result = _run(env, tmp_path, "self-update")
    field = {"store": 1, "release": 2, "taken": "line",
             "line_source": f"{base}/store-major-1", "upgrade_needed": True,
             "command": UPGRADE}
    assert {k: v for k, v in checked["store_major"].items() if k != "message"} == field
    assert UPGRADE in checked["store_major"]["message"]
    assert checked["update_available"] is True
    assert checked["available"]["manager_sha256"] == line["manager"]["sha256"]
    assert checked["available"]["source"] == f"{base}/store-major-1/{MANIFEST_REL}"
    assert result["self_update"] == "updated"
    assert {k: v for k, v in result["store_major"].items() if k != "message"} == field
    assert _tag(link) == "# the line manager"


def test_without_the_line_what_is_installed_is_kept(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 1}))
    link = _legacy(env)
    before = link.resolve()
    _release(tmp_path / "src" / "main", "main", 2)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        checked = _run(env, tmp_path, "self-update", "--check")
        result = _run(env, tmp_path, "self-update")
    assert checked["update_available"] is False
    assert checked["store_major"]["taken"] == "installed"
    assert checked["store_major"]["upgrade_needed"] is True
    assert checked["store_major"]["command"] == UPGRADE
    assert result["self_update"] == "kept"
    assert result["store_major"]["command"] == UPGRADE
    assert link.resolve() == before
    assert not (Path(env["CAPABILITIES_HOME"]) / ".manager" / "releases").exists()


def test_from_a_checkout_another_major_keeps_what_is_installed(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"capabilities": 1}))
    link = _legacy(env)
    before = link.resolve()
    _release(tmp_path / "checkout", "checkout", 2)
    checked = _run(env, tmp_path, "self-update", "--check", "--from",
                   str(tmp_path / "checkout"))
    result = _run(env, tmp_path, "self-update", "--from", str(tmp_path / "checkout"))
    assert checked["update_available"] is False
    for answer in (checked, result):
        assert answer["store_major"]["store"] == 1
        assert answer["store_major"]["taken"] == "installed"
        assert "line_source" not in answer["store_major"]
        assert answer["store_major"]["command"] == UPGRADE
    assert result["self_update"] == "kept"
    assert link.resolve() == before


@pytest.mark.parametrize("store", ["none", "empty"])
def test_with_no_store_or_an_empty_one_the_newest_is_taken(tmp_path: Path, cluster,
                                                           store: str) -> None:
    env = _env(tmp_path, None if store == "none" else cluster.store(None))
    link = _legacy(env)
    _release(tmp_path / "checkout", "checkout", 2)
    result = _run(env, tmp_path, "self-update", "--from", str(tmp_path / "checkout"))
    assert result["self_update"] == "updated"
    assert result["store_major"]["store"] is None
    assert result["store_major"]["taken"] == "release"
    assert result["store_major"]["upgrade_needed"] is False
    assert ("no store is configured" if store == "none" else "holds no schema") \
        in result["store_major"]["reason"]
    assert _tag(link) == "# the checkout manager"


def test_a_store_newer_than_the_installed_release_takes_main(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 2, "tasks": 1}))
    link = _legacy(env)
    _release(tmp_path / "src" / "main", "main", 2)
    _release(tmp_path / "src" / "store-major-1", "line", 1)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        result = _run(env, tmp_path, "self-update")
    assert result["store_major"] == {"store": 2, "release": 2, "taken": "release",
                                     "upgrade_needed": False}
    assert _tag(link) == "# the main manager"


# --- update ----------------------------------------------------------------------

def _publish(root: Path, tag: str, store_major: int, library: str | None = None) -> None:
    """A catalogue tree in `root` carrying resend marked `tag`."""
    script = BUNDLE / "bin" / "resend"
    target = root / "capabilities" / "resend" / "bin" / "resend"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(script.read_bytes() + f"\n# {tag}\n".encode())
    catalog = {"capabilities": {"resend": {"path": "capabilities/resend",
                                           "payload_sha256": "0" * 64,
                                           "store_major": store_major}},
               "store_major": store_major}
    if library:
        catalog["library"] = library
    (root / CATALOG_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / CATALOG_REL).write_text(json.dumps(catalog))


def _install_canonical(tmp: Path, env: dict[str, str], base: str) -> Path:
    """resend installed as from the canonical source at `base`/main."""
    _run(env, tmp, "install", "resend", "--from", str(BUNDLE))
    meta_path = Path(env["CAPABILITIES_HOME"]) / "resend" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["source"] = f"{base}/main/capabilities/resend/bin/resend"
    meta_path.write_text(json.dumps(meta))
    return meta_path


def _installed_tag(env: dict[str, str]) -> str:
    script = Path(env["CAPABILITIES_HOME"]) / "resend" / "resend"
    return script.read_text().rstrip().splitlines()[-1]


def _uv_logging_contract_syncs(tmp: Path, env: dict[str, str]) -> Path:
    """A uv that logs every contract-library sync and passes every call on."""
    real = shutil.which("uv", path=env["PATH"])
    root = tmp / "logging-uv"
    root.mkdir()
    uv = root / "uv"
    uv.write_text(f"""#!/bin/sh
case " $* " in
  *" sync "*"--upgrade-package capabilities-contract"*) echo "$*" >> "{root}/log";;
esac
exec "{real}" "$@"
""")
    uv.chmod(0o755)
    env["PATH"] = str(root) + os.pathsep + env["PATH"]
    return root / "log"


def test_update_takes_the_stores_line_and_keeps_its_library(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 1}))
    _publish(tmp_path / "src" / "main", "main", 2)
    _publish(tmp_path / "src" / "store-major-1", "line", 1, ">=0.42,<0.50")
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        meta_path = _install_canonical(tmp_path, env, base)
        log = _uv_logging_contract_syncs(tmp_path, env)
        checked = _run(env, tmp_path, "update", "--check", "resend")
        row = checked["checked"][0]
        assert row["update_available"] is True
        assert row["source"] == f"{base}/store-major-1/capabilities/resend/bin/resend"
        assert row["store_major"]["taken"] == "line"
        assert row["store_major"]["command"] == UPGRADE
        assert checked["store_major"] == {"store": 1}
        asked = log.read_text().splitlines() if log.exists() else []
        assert asked and all("capabilities-contract>=0.42,<0.50" in line for line in asked)
        updated = _run(env, tmp_path, "update", "resend")
    entry = updated["updated"][0]
    assert entry["changed"] is True
    assert entry["store_major"]["taken"] == "line"
    assert entry["store_major"]["line_source"] == f"{base}/store-major-1"
    assert entry["store_major"]["upgrade_needed"] is True
    assert entry["store_major"]["command"] == UPGRADE
    assert _installed_tag(env) == "# line"
    # Recorded against main, so the next update starts there again.
    assert json.loads(meta_path.read_text())["source"] == \
        f"{base}/main/capabilities/resend/bin/resend"
    synced = log.read_text().splitlines()[len(asked):]
    assert synced and all("--upgrade-package capabilities-contract>=0.42,<0.50" in line
                          and "--dry-run" not in line for line in synced)


def test_update_takes_a_release_of_the_stores_major(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 2}))
    _publish(tmp_path / "src" / "main", "main", 2)
    _publish(tmp_path / "src" / "store-major-1", "line", 1, ">=0.42,<0.50")
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base)
        log = _uv_logging_contract_syncs(tmp_path, env)
        updated = _run(env, tmp_path, "update", "resend")
    entry = updated["updated"][0]
    assert "store_major" not in entry
    assert updated["store_major"] == {"store": 2}
    assert _installed_tag(env) == "# main"
    assert all(">=0.42,<0.50" not in line for line in log.read_text().splitlines())


def test_update_keeps_what_is_installed_without_the_line(tmp_path: Path, cluster) -> None:
    env = _env(tmp_path, cluster.store({"core": 1}))
    _publish(tmp_path / "src" / "main", "main", 2)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base)
        before = (Path(env["CAPABILITIES_HOME"]) / "resend" / "resend").read_bytes()
        checked = _run(env, tmp_path, "update", "--check", "resend")
        updated = _run(env, tmp_path, "update", "resend")
    row = checked["checked"][0]
    assert row["update_available"] is False and "contract_library" not in row
    assert row["store_major"]["taken"] == "installed"
    entry = updated["updated"][0]
    assert entry["changed"] is False
    assert entry["store_major"]["taken"] == "installed"
    assert entry["store_major"]["command"] == UPGRADE
    assert (Path(env["CAPABILITIES_HOME"]) / "resend" / "resend").read_bytes() == before


def test_update_from_a_checkout_of_another_major_keeps_what_is_installed(
    tmp_path: Path, cluster,
) -> None:
    env = _env(tmp_path, cluster.store({"core": 1}))
    checkout = tmp_path / "checkout"
    _publish(checkout, "checkout", 2)
    _run(env, tmp_path, "install", "resend", "--from",
         str(checkout / "capabilities" / "resend"))
    _publish(checkout, "checkout-next", 2)
    updated = _run(env, tmp_path, "update", "resend")
    entry = updated["updated"][0]
    assert entry["changed"] is False
    assert entry["store_major"]["taken"] == "installed"
    assert "line_source" not in entry["store_major"]
    assert _installed_tag(env) == "# checkout"


def test_with_no_store_update_takes_the_newest(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _publish(tmp_path / "src" / "main", "main", 2)
    with _serve(tmp_path / "src") as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base)
        updated = _run(env, tmp_path, "update", "resend")
    assert "store_major" not in updated["updated"][0]
    assert updated["store_major"]["store"] is None
    assert "no store is configured" in updated["store_major"]["reason"]
    assert _installed_tag(env) == "# main"


def test_a_line_library_range_constrains_the_sync_uv_runs(tmp_path: Path) -> None:
    """uv takes a requirement given to --upgrade-package as a constraint, so a
    sync stays inside the line's range even where the script admits newer."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    script = tmp_path / "probe"
    script.write_text("# /// script\n# requires-python = \">=3.11\"\n# dependencies = [\n"
                      "#     \"capabilities-contract>=0.38,<1\",\n# ]\n# ///\n")

    def resolved(library: str | None) -> str | None:
        proc = subprocess.run(
            [*M["_contract_library_sync"](uv, script, library), "--dry-run",
             "--output-format", "json"], capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr
        changes = json.loads(proc.stdout)["sync"]["changes"]
        return next((c["version"] for c in changes
                     if c["name"] == "capabilities-contract" and c["action"] == "installed"),
                    None)

    from packaging.version import Version
    assert Version(resolved(">=0.38,<0.40")) < Version("0.40")
    assert Version(resolved(None)) >= Version("0.40")

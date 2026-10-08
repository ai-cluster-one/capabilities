"""The machine's store setting: `capabilities store set|show|doctor|unset`.

The setting is the one store pointer, the family's file
`$XDG_CONFIG_HOME/agentkit/store.json` with the password inside it at mode
0600. The manager writes it; its own records in database mode resolve their
store from it, `AGENTKIT_STORE_URL` then `CAPABILITIES_STORE_URL` overriding
it; a capability reads it through the store tier and writes nothing. While the
file is absent the legacy pair under `$XDG_CONFIG_HOME/capabilities/` is read.

Everything runs against a scratch HOME. The doctor and the records tests build
a throwaway PostgreSQL with TLS on a loopback port when `initdb`, `pg_ctl` and
`openssl` are on PATH and psycopg2 is importable, and skip otherwise.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "storefix"
# Distinctive, and carrying every character a URL or an env file could mangle,
# so its absence from output is meaningful and its round trip is proven.
PASSWORD = "pw-Zq7!x@%3A/#?&= 'q\"\\end"
ADMIN = "pgadmin"

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_store_setting_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR",
                "CAPABILITIES_AUTH_CONTEXT", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_STORE_URL",
                "AGENTKIT_STORE_URL",
                "CAPABILITIES_STORE_MODE", "CAPABILITIES_DEV_SESSION",
                "CAPABILITIES_WORKSPACE", "STORE_FIX_PASSWORD"):
        env.pop(key, None)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CAPABILITIES_BIN": str(tmp_path / "bin"),
    })
    (tmp_path / "home" / "nowhere").mkdir(parents=True, exist_ok=True)
    return env


def _outside(env: dict) -> Path:
    return Path(env["HOME"]) / "nowhere"


def _manager(env: dict, *args: str, cwd: Path | None = None, stdin: str | None = None,
             extra: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], cwd=cwd or _outside(env),
                          env={**env, **(extra or {})}, input=stdin, text=True,
                          capture_output=True, timeout=180)


def _ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess) -> dict:
    for line in reversed(result.stderr.splitlines()):
        try:
            return json.loads(line)["error"]
        except (ValueError, KeyError, TypeError):
            continue
    raise AssertionError(result.stdout + result.stderr)


def _no_secret(result: subprocess.CompletedProcess, secret: str = PASSWORD) -> None:
    assert secret not in result.stdout and secret not in result.stderr


def _family(env: dict) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "agentkit" / "store.json"


def _written(env: dict) -> dict:
    return json.loads(_family(env).read_text())


def _files(env: dict) -> tuple[Path, Path]:
    """The legacy pair."""
    home = Path(env["XDG_CONFIG_HOME"]) / "capabilities"
    return home / "store.json", home / "credentials.env"


def _legacy(env: dict, document: dict | None = None, password: str | None = "old") -> None:
    setting_file, password_file = _files(env)
    setting_file.parent.mkdir(parents=True, exist_ok=True)
    setting_file.write_text(json.dumps(document or {
        "schema": "capabilities.store.v1", "host": "legacy.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require"}))
    if password is not None:
        password_file.write_text(f"OTHER_KEY=kept\nCAPABILITIES_STORE_PASSWORD={password}\n")


def _nothing_written(env: dict) -> bool:
    return not _family(env).exists() and not _files(env)[0].exists() \
        and not _files(env)[1].exists()


BASE = ("--host", "db.example.test", "--database", "app", "--user", "agent")


def _set(env: dict, *extra_args: str, password: str = PASSWORD,
         extra: dict | None = None) -> subprocess.CompletedProcess:
    return _manager(env, "store", "set", *BASE, *extra_args, "--password-stdin",
                    stdin=password + "\n", extra=extra)


# --- set ----------------------------------------------------------------------

def test_set_takes_the_password_from_stdin(tmp_path):
    env = _env(tmp_path)
    result = _set(env)
    _no_secret(result)
    payload = _ok(result)
    assert payload["changed"] is True and payload["configured"] is True
    assert payload["password"]["present"] is True
    assert _written(env)["password"] == PASSWORD
    assert not _files(env)[0].exists() and not _files(env)[1].exists()
    assert S.read_store_setting(env["XDG_CONFIG_HOME"])["password"] == PASSWORD


def test_set_takes_the_password_from_a_file(tmp_path):
    env = _env(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text(PASSWORD + "\n")
    result = _manager(env, "store", "set", *BASE, "--password-file", str(secret))
    _no_secret(result)
    _ok(result)
    assert S.read_store_setting(env["XDG_CONFIG_HOME"])["password"] == PASSWORD


def test_set_takes_the_password_from_a_named_environment_variable(tmp_path):
    env = _env(tmp_path)
    result = _manager(env, "store", "set", *BASE, "--password-env", "STORE_FIX_PASSWORD",
                      extra={"STORE_FIX_PASSWORD": PASSWORD})
    _no_secret(result)
    _ok(result)
    assert S.read_store_setting(env["XDG_CONFIG_HOME"])["password"] == PASSWORD
    unset = _manager(env, "store", "set", *BASE, "--password-env", "STORE_FIX_PASSWORD")
    assert unset.returncode == 6 and _error(unset)["code"] == "password_source"


@pytest.mark.parametrize("argv", [
    ("--password", PASSWORD), (f"--password={PASSWORD}",), ("--pass", PASSWORD),
    (PASSWORD,),
])
def test_set_never_takes_the_password_on_argv(tmp_path, argv):
    env = _env(tmp_path)
    result = _manager(env, "store", "set", *BASE, *argv)
    assert result.returncode == 6, result.stdout + result.stderr
    _no_secret(result)
    assert _nothing_written(env)


def test_set_needs_exactly_one_password_source(tmp_path):
    env = _env(tmp_path)
    none = _manager(env, "store", "set", *BASE)
    assert none.returncode == 6 and _error(none)["code"] == "password_source"
    both = _manager(env, "store", "set", *BASE, "--password-stdin", "--password-env", "X",
                    stdin=PASSWORD)
    assert both.returncode == 6 and _error(both)["code"] == "password_source"
    _no_secret(both)
    assert _nothing_written(env)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_set_refuses_an_sslmode_below_require(tmp_path, mode):
    env = _env(tmp_path)
    result = _set(env, "--sslmode", mode)
    assert result.returncode == 6
    assert _error(result)["code"] == "sslmode_too_weak"
    _no_secret(result)
    assert _nothing_written(env)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "/var/run/postgresql"])
def test_set_admits_disable_only_for_a_local_host(tmp_path, host):
    env = _env(tmp_path)
    payload = _ok(_manager(env, "store", "set", "--host", host, "--database", "app",
                           "--user", "agent", "--sslmode", "disable", "--password-stdin",
                           stdin=PASSWORD + "\n"))
    assert payload["setting"]["sslmode"]["value"] == "disable"
    assert _written(env)["host"] == host
    for mode in ("allow", "prefer"):
        refused = _manager(env, "store", "set", "--host", host, "--database", "app",
                           "--user", "agent", "--sslmode", mode, "--password-stdin",
                           stdin=PASSWORD + "\n")
        assert refused.returncode == 6 and _error(refused)["code"] == "sslmode_too_weak"


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_set_accepts_require_and_stronger(tmp_path, mode):
    env = _env(tmp_path)
    root = tmp_path / "root.crt"
    root.write_text("fixture certificate\n")
    extra = ("--sslrootcert", str(root)) if mode != "require" else ()
    payload = _ok(_set(env, "--sslmode", mode, *extra))
    assert payload["setting"]["sslmode"]["value"] == mode
    written = _written(env)
    assert written["sslmode"] == mode
    if extra:
        assert written["sslrootcert"] == str(root.resolve())


def test_set_defaults_the_port_and_the_sslmode(tmp_path):
    env = _env(tmp_path)
    payload = _ok(_set(env))
    assert payload["setting"]["port"]["value"] == 5432
    assert payload["setting"]["sslmode"]["value"] == "require"
    root = tmp_path / "root.crt"
    root.write_text("fixture certificate\n")
    payload = _ok(_set(env, "--sslrootcert", str(root), "--port", "6543"))
    assert payload["setting"]["sslmode"]["value"] == "verify-full"
    assert payload["setting"]["port"]["value"] == 6543


def test_set_writes_the_family_file_whole_at_0600_and_leaves_the_legacy_pair(tmp_path):
    env = _env(tmp_path)
    setting_file, password_file = _files(env)
    _legacy(env)
    legacy_before = (setting_file.read_text(), password_file.read_text())
    payload = _ok(_set(env))
    path = _family(env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _written(env) == {
        "schema": "agentkit.store.v1", "host": "db.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require", "password": PASSWORD}
    assert sorted(p.name for p in path.parent.iterdir()) == ["store.json"]
    assert (setting_file.read_text(), password_file.read_text()) == legacy_before
    assert payload["setting"]["host"] == {"value": "db.example.test", "source": str(path)}
    again = _ok(_set(env))
    assert again["changed"] is False
    changed = _ok(_set(env, password="another-" + PASSWORD))
    assert changed["changed"] is True
    assert _written(env)["password"] == "another-" + PASSWORD


def test_set_never_replaces_a_version_it_does_not_know(tmp_path):
    env = _env(tmp_path)
    path = _family(env)
    path.parent.mkdir(parents=True)
    newer = json.dumps({"schema": "agentkit.store.v2", "host": "h", "future": True})
    path.write_text(newer)
    for args in (("store", "set", *BASE, "--password-stdin"), ("store", "unset")):
        result = _manager(env, *args, stdin=PASSWORD + "\n")
        assert result.returncode == 6 and _error(result)["code"] == "store_setting_too_new"
        assert path.read_text() == newer
    shown = _manager(env, "store", "show")
    assert shown.returncode == 6 and _error(shown)["code"] == "store_setting_too_new"


def test_set_writes_under_the_manager_lock(tmp_path):
    env = _env(tmp_path)
    lock = Path(env["XDG_STATE_HOME"]) / "capabilities" / "manager-mutation.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        waiting = subprocess.Popen(
            [str(MANAGER), "store", "set", *BASE, "--password-stdin"],
            cwd=_outside(env), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        waiting.stdin.write(PASSWORD + "\n")
        waiting.stdin.close()
        time.sleep(2.0)
        assert waiting.poll() is None
        assert _nothing_written(env)
    out, err = waiting.stdout.read(), waiting.stderr.read()
    waiting.wait(timeout=60)
    assert waiting.returncode == 0, out + err
    assert PASSWORD not in out + err
    assert _written(env)["password"] == PASSWORD


def test_set_is_refused_under_the_read_only_switch(tmp_path):
    env = _env(tmp_path)
    result = _set(env, extra={"CAPABILITIES_READ_ONLY": "1"})
    assert result.returncode == 4
    assert _error(result)["code"] == "read_only_switch"
    _no_secret(result)
    assert _nothing_written(env)


def test_set_names_the_schema_only_when_one_is_given(tmp_path):
    env = _env(tmp_path)
    path = _family(env)
    _ok(_set(env))
    written = _written(env)
    assert written["schema"] == "agentkit.store.v1" and "db_schema" not in written
    payload = _ok(_set(env, "--schema", "agentkit"))
    assert payload["changed"] is True
    written = _written(env)
    assert written["schema"] == "agentkit.store.v1"
    assert written["db_schema"] == "agentkit"
    assert payload["setting"]["db_schema"] == {"value": "agentkit", "source": str(path)}
    assert _ok(_set(env, "--schema", "agentkit"))["changed"] is False
    assert _ok(_set(env, "--schema", "tools"))["changed"] is True
    assert _ok(_set(env))["setting"]["db_schema"] == {"value": "agentkit",
                                                      "source": "default"}
    assert "db_schema" not in _written(env)


@pytest.mark.parametrize("name", ["public", "information_schema", "pg_toast", "Tools",
                                  "1tools", "tools-x", ""])
def test_set_refuses_a_schema_that_is_reserved_or_not_an_identifier(tmp_path, name):
    env = _env(tmp_path)
    result = _set(env, "--schema", name)
    assert result.returncode == 6 and _error(result)["code"] == "bad_schema_name"
    assert _nothing_written(env)


def test_a_v1_setting_written_before_the_schema_is_read_as_agentkit(tmp_path):
    env = _env(tmp_path)
    setting_file = _files(env)[0]
    setting_file.parent.mkdir(parents=True)
    setting_file.write_text(json.dumps({
        "schema": "capabilities.store.v1", "host": "db.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require",
        "db_schema": "ignored_in_v1"}))
    assert S.read_store_setting(env["XDG_CONFIG_HOME"])["db_schema"] == "agentkit"
    payload = _ok(_manager(env, "store", "show"))
    assert payload["setting"]["db_schema"] == {"value": "agentkit", "source": "default"}
    assert payload["setting"]["host"] == {"value": "db.example.test", "source": "legacy"}
    assert payload["exists"] is False


def test_a_v2_setting_naming_a_bad_schema_is_refused_when_read(tmp_path):
    env = _env(tmp_path)
    setting_file = _files(env)[0]
    setting_file.parent.mkdir(parents=True)
    setting_file.write_text(json.dumps({
        "schema": "capabilities.store.v2", "host": "db.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require", "db_schema": "public"}))
    with pytest.raises(S.StoreError) as raised:
        S.read_store_setting(env["XDG_CONFIG_HOME"])
    assert raised.value.slug == "bad_schema_name"
    result = _manager(env, "store", "show")
    assert result.returncode == 6 and _error(result)["code"] == "bad_schema_name"


# --- unset --------------------------------------------------------------------

def test_unset_removes_the_file_and_the_legacy_pair_and_keeps_other_keys(tmp_path):
    env = _env(tmp_path)
    setting_file, password_file = _files(env)
    _legacy(env)
    _ok(_set(env, "--schema", "tools"))
    result = _manager(env, "store", "unset")
    _no_secret(result)
    payload = _ok(result)
    assert payload["changed"] is True and payload["configured"] is False
    assert payload["in_force"]["source"] == "default"
    assert not _family(env).exists() and not setting_file.exists()
    assert password_file.read_text() == "OTHER_KEY=kept\n"
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert S.read_store_setting(env["XDG_CONFIG_HOME"]) is None
    assert _ok(_manager(env, "store", "unset"))["changed"] is False


def test_unset_removes_a_legacy_password_file_it_leaves_empty(tmp_path):
    env = _env(tmp_path)
    _legacy(env, password=None)
    _files(env)[1].write_text("CAPABILITIES_STORE_PASSWORD=old\n")
    assert _ok(_manager(env, "store", "unset"))["changed"] is True
    assert sorted(p.name for p in _files(env)[0].parent.iterdir()) == []


def test_unset_removes_a_setting_that_no_longer_reads(tmp_path):
    env = _env(tmp_path)
    for path in (_family(env), _files(env)[0]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert _manager(env, "store", "show").returncode == 6
        assert _ok(_manager(env, "store", "unset"))["changed"] is True
        assert not path.exists()


def test_unset_is_refused_under_the_read_only_switch(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    result = _manager(env, "store", "unset", extra={"CAPABILITIES_READ_ONLY": "1"})
    assert result.returncode == 4 and _error(result)["code"] == "read_only_switch"
    assert _written(env)["password"] == PASSWORD


# --- show ---------------------------------------------------------------------

def test_show_without_a_setting_reports_the_default(tmp_path):
    env = _env(tmp_path)
    payload = _ok(_manager(env, "store", "show", "--json"))
    assert payload["configured"] is False and payload["setting"] is None
    assert payload["path"] == str(_family(env)) and payload["exists"] is False
    assert payload["password"] == {"present": False, "source": None}
    assert payload["in_force"] == {
        "store": str(Path(env["XDG_STATE_HOME"]) / "capabilities" / "store.db"),
        "source": "default"}
    assert payload["overridden_by"] is None


def test_show_reports_each_value_and_its_source_without_the_secret(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env, "--port", "6543"))
    result = _manager(env, "store", "show")
    _no_secret(result)
    payload = _ok(result)
    setting_file = _family(env)
    assert payload["path"] == str(setting_file) and payload["exists"] is True
    assert Path(payload["path"]).is_absolute()
    assert payload["setting"] == {
        field: {"value": value, "source": str(setting_file)}
        for field, value in (("host", "db.example.test"), ("port", 6543),
                             ("database", "app"), ("user", "agent"),
                             ("sslmode", "require"))} | {
        "db_schema": {"value": "agentkit", "source": "default"}}
    assert payload["password"] == {"present": True, "source": str(setting_file)}
    assert payload["in_force"] == {"store": "postgresql://db.example.test:6543/app",
                                   "source": "setting"}
    assert payload["overridden_by"] is None
    assert _ok(_manager(env, "path", "store")) == payload["in_force"]


def test_show_reports_the_override_while_it_is_set(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    override = f"postgresql://someone:{PASSWORD}@other.example.test:5432/elsewhere"
    result = _manager(env, "store", "show", extra={"CAPABILITIES_STORE_URL": override})
    _no_secret(result)
    payload = _ok(result)
    assert payload["in_force"] == {"store": "postgresql://other.example.test:5432/elsewhere",
                                   "source": "CAPABILITIES_STORE_URL"}
    assert payload["overridden_by"] == "CAPABILITIES_STORE_URL"
    assert payload["setting"]["host"]["value"] == "db.example.test"
    first = f"postgresql://someone:{PASSWORD}@first.example.test:5432/family"
    result = _manager(env, "store", "show", extra={"CAPABILITIES_STORE_URL": override,
                                                   "AGENTKIT_STORE_URL": first})
    _no_secret(result)
    payload = _ok(result)
    assert payload["in_force"] == {"store": "postgresql://first.example.test:5432/family",
                                   "source": "AGENTKIT_STORE_URL"}
    assert payload["overridden_by"] == "AGENTKIT_STORE_URL"


def test_show_reads_the_legacy_pair_while_the_file_is_absent_and_the_file_after(tmp_path):
    env = _env(tmp_path)
    _legacy(env, {"schema": "capabilities.store.v2", "host": "legacy.example.test",
                  "port": 5432, "database": "app", "user": "agent", "sslmode": "require",
                  "db_schema": "tools"})
    payload = _ok(_manager(env, "store", "show"))
    assert payload["configured"] is True and payload["exists"] is False
    assert payload["path"] == str(_family(env))
    assert payload["setting"]["host"] == {"value": "legacy.example.test", "source": "legacy"}
    assert payload["setting"]["db_schema"] == {"value": "tools", "source": "legacy"}
    assert payload["password"] == {"present": True, "source": "legacy"}
    _ok(_set(env))
    payload = _ok(_manager(env, "store", "show"))
    assert payload["exists"] is True
    assert payload["setting"]["host"] == {"value": "db.example.test",
                                          "source": str(_family(env))}
    assert payload["setting"]["db_schema"] == {"value": "agentkit", "source": "default"}


# --- the helper a capability reads it through -----------------------------------

def _fixture_capability(tmp_path: Path) -> Path:
    """The manager's own core-only scaffold, answering one verb with what the
    stamped store tier reads, before any gate, so the helper is all that runs."""
    text = M._capability_skeleton(NAME, True)
    marker = "\ndef main() -> None:\n    _gate()\n"
    assert marker in text
    text = text.replace(marker, (
        "\ndef main() -> None:\n"
        "    if sys.argv[1:] == [\"store-setting\"]:\n"
        "        print(json.dumps({\"setting\": read_store_setting()}))\n"
        "        return\n"
        "    _gate()\n"), 1)
    script = tmp_path / "bundle" / NAME
    script.parent.mkdir(parents=True)
    script.write_text(text)
    script.chmod(0o755)
    return script


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()} if root.exists() else {}


def test_a_capability_reads_the_setting_through_the_store_tier_and_writes_nothing(tmp_path):
    env = _env(tmp_path)
    script = _fixture_capability(tmp_path)
    roots = [tmp_path / name for name in ("config", "state", "cache", "data", "home")]

    def read() -> dict:
        before = [_tree(root) for root in roots]
        result = subprocess.run([str(script), "store-setting"], cwd=_outside(env), env=env,
                                text=True, capture_output=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        assert [_tree(root) for root in roots] == before
        return json.loads(result.stdout)["setting"]

    assert read() is None
    _ok(_set(env, "--port", "6543"))
    assert read() == {"host": "db.example.test", "port": 6543, "database": "app",
                      "user": "agent", "sslmode": "require", "db_schema": "agentkit",
                      "password": PASSWORD}
    _ok(_set(env, "--schema", "tools"))
    assert read()["db_schema"] == "tools"


def test_the_store_tier_builds_the_url_with_the_password_encoded():
    setting = {"host": "db.example.test", "port": 5432, "database": "app",
               "user": "agent", "sslmode": "verify-full",
               "sslrootcert": "/etc/ssl/root.crt", "password": PASSWORD}
    url = S.store_setting_url(setting)
    from urllib.parse import parse_qs, unquote, urlparse
    parsed = urlparse(url)
    assert parsed.scheme == "postgresql" and parsed.hostname == "db.example.test"
    assert unquote(parsed.password) == PASSWORD and unquote(parsed.username) == "agent"
    assert parse_qs(parsed.query) == {"sslmode": ["verify-full"],
                                      "sslrootcert": ["/etc/ssl/root.crt"]}
    assert "sslmode=disable" in S.store_setting_url(setting, sslmode="disable")
    assert PASSWORD not in S.store_setting_url(setting, with_password=False)
    socket_url = S.store_setting_url({**setting, "host": "/var/run/postgresql",
                                      "sslmode": "disable"})
    parsed = urlparse(socket_url)
    assert parsed.hostname is None and parsed.path == "/app"
    assert parse_qs(parsed.query)["host"] == ["/var/run/postgresql"]
    assert parse_qs(parsed.query)["port"] == ["5432"]


# --- a throwaway PostgreSQL with TLS ----------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


ENFORCED_HBA = (f"hostssl all {ADMIN} 127.0.0.1/32 trust\n"
                "hostssl all all 127.0.0.1/32 scram-sha-256\n"
                "hostnossl all all 127.0.0.1/32 reject\n")
PLAIN_HBA = (f"hostssl all {ADMIN} 127.0.0.1/32 trust\n"
             "host all all 127.0.0.1/32 scram-sha-256\n")


class Cluster:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.port = _free_port()
        self.cert = root / "server.crt"
        self.env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(self, *argv: str) -> None:
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True,
                                timeout=120)
        assert result.returncode == 0, (argv, result.stdout + result.stderr)

    def start(self) -> None:
        self.run("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                 "--locale=en_US.UTF-8", "--auth=trust")
        key = self.root / "server.key"
        self.run("openssl", "req", "-new", "-x509", "-days", "2", "-nodes",
                 "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                 "-keyout", str(key), "-out", str(self.cert))
        key.chmod(0o600)
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       "unix_socket_directories = ''\nssl = on\n"
                       f"ssl_cert_file = '{self.cert}'\nssl_key_file = '{key}'\n"
                       "password_encryption = 'scram-sha-256'\n")
        self.hba(ENFORCED_HBA, reload=False)
        self.run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "pg.log"),
                 "-w", "-t", "60", "start")
        admin = self.connect(ADMIN, "postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE ROLE agent LOGIN PASSWORD %s", (PASSWORD,))
            cur.execute("CREATE DATABASE app OWNER agent ENCODING 'UTF8' "
                        "TEMPLATE template0")
            cur.execute("CREATE DATABASE foreign_db ENCODING 'UTF8' TEMPLATE template0")
        admin.close()
        admin = self.connect(ADMIN, "app")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE SCHEMA locked")
        admin.close()

    def connect(self, user: str, database: str, password: str | None = None):
        import psycopg2
        return psycopg2.connect(host="127.0.0.1", port=self.port, user=user,
                                dbname=database, password=password,
                                sslmode="require", connect_timeout=10)

    def hba(self, body: str, reload: bool = True) -> None:
        (self.data / "pg_hba.conf").write_text(body)
        if reload:
            self.run("pg_ctl", "-D", str(self.data), "reload")
            time.sleep(1.0)

    def stop(self) -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "stop"],
                       env=self.env, capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg2")
    for tool in ("initdb", "pg_ctl", "openssl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-store-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def enforced(cluster):
    cluster.hba(ENFORCED_HBA)
    yield cluster
    cluster.hba(ENFORCED_HBA)


# A name for the loopback cluster that is not one the doctor takes for this
# machine, so the checks a store across a network gets are the ones that run.
REMOTE_NAME = "127.1"


def _set_cluster(env: dict, cluster: Cluster, *extra_args: str,
                 host: str = REMOTE_NAME) -> None:
    _ok(_manager(env, "store", "set", "--host", host, "--port", str(cluster.port),
                 "--database", "app", "--user", "agent", *extra_args,
                 "--password-stdin", stdin=PASSWORD + "\n"))


# --- doctor -----------------------------------------------------------------------

def test_doctor_passes_when_tls_works_and_plain_text_is_refused(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced)
    result = _manager(env, "store", "doctor", "--json")
    _no_secret(result)
    payload = _ok(result)
    assert payload["ok"] is True
    tls = payload["checks"]["tls"]
    assert tls["ok"] is True and tls["server_version"]
    assert tls["tls"]["version"].startswith("TLS") and tls["tls"]["cipher"]
    plain = payload["checks"]["plain_text_refused"]
    assert plain["ok"] is True and "no encryption" in plain["reason"]
    assert payload["store"] == f"postgresql://{REMOTE_NAME}:{enforced.port}/app"
    assert payload["db_schema"] == "agentkit"
    assert payload["checks"]["schema"]["ok"] is True
    assert payload["checks"]["schema"]["exists"] is False


def test_doctor_passes_for_a_named_schema_the_role_may_use(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced, "--schema", "tools")
    payload = _ok(_manager(env, "store", "doctor"))
    assert payload["ok"] is True and payload["db_schema"] == "tools"
    assert payload["checks"]["schema"]["name"] == "tools"
    admin = enforced.connect("agent", "app", PASSWORD)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS tools")
    admin.close()
    schema = _ok(_manager(env, "store", "doctor"))["checks"]["schema"]
    assert schema["ok"] is True and schema["exists"] is True


def test_doctor_fails_when_the_role_may_not_use_the_schema(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced, "--schema", "locked")
    result = _manager(env, "store", "doctor")
    _no_secret(result)
    assert result.returncode == 7, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["checks"]["tls"]["ok"] is True
    schema = payload["checks"]["schema"]
    assert schema["ok"] is False and schema["exists"] is True
    assert "lacks USAGE and CREATE on schema locked" in schema["reason"]


def test_doctor_fails_when_the_role_may_not_create_the_missing_schema(tmp_path, enforced):
    env = _env(tmp_path)
    _ok(_manager(env, "store", "set", "--host", REMOTE_NAME, "--port", str(enforced.port),
                 "--database", "foreign_db", "--user", "agent", "--schema", "tools",
                 "--password-stdin", stdin=PASSWORD + "\n"))
    result = _manager(env, "store", "doctor")
    assert result.returncode == 7, result.stdout + result.stderr
    schema = json.loads(result.stdout)["checks"]["schema"]
    assert schema["ok"] is False and schema["exists"] is False
    assert "may not create schemas in database foreign_db" in schema["reason"]


def test_doctor_verifies_the_server_certificate_with_verify_full(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced, "--sslmode", "verify-full",
                 "--sslrootcert", str(enforced.cert), host="127.0.0.1")
    payload = _ok(_manager(env, "store", "doctor"))
    assert payload["ok"] is True and payload["sslmode"] == "verify-full"


def test_doctor_skips_the_plain_text_check_for_a_local_store(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced, host="127.0.0.1")
    enforced.hba(PLAIN_HBA)
    result = _manager(env, "store", "doctor")
    _no_secret(result)
    payload = _ok(result)
    assert payload["ok"] is True and payload["checks"]["tls"]["ok"] is True
    plain = payload["checks"]["plain_text_refused"]
    assert plain["ok"] is True and plain["skipped"] is True
    assert "on this machine" in plain["reason"]


def test_doctor_passes_a_local_store_reached_without_tls(tmp_path, enforced):
    env = _env(tmp_path)
    enforced.hba(PLAIN_HBA)
    _set_cluster(env, enforced, "--sslmode", "disable", host="127.0.0.1")
    result = _manager(env, "store", "doctor")
    _no_secret(result)
    payload = _ok(result)
    assert payload["ok"] is True and payload["sslmode"] == "disable"
    tls = payload["checks"]["tls"]
    assert tls["ok"] is True and "without TLS" in tls["reason"]
    assert payload["checks"]["plain_text_refused"]["skipped"] is True
    assert payload["checks"]["schema"]["ok"] is True


def test_doctor_fails_when_the_server_accepts_plain_text(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced)
    enforced.hba(PLAIN_HBA)
    result = _manager(env, "store", "doctor")
    _no_secret(result)
    assert result.returncode == 7, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["checks"]["tls"]["ok"] is True
    plain = payload["checks"]["plain_text_refused"]
    assert plain["ok"] is False and "plain-text connection" in plain["reason"]


def test_doctor_without_a_setting_is_not_found(tmp_path):
    env = _env(tmp_path)
    result = _manager(env, "store", "doctor")
    assert result.returncode == 3 and _error(result)["code"] == "no_store_setting"


# --- the one pointer ----------------------------------------------------------------

def _db_project(tmp_path: Path, env: dict) -> tuple[Path, str, str]:
    root = tmp_path / "consumer"
    (root / ".git").mkdir(parents=True)
    envelope = root / "capabilities"
    envelope.mkdir()
    project_id = str(uuid.uuid4())
    slug = "fixture-" + project_id[:8]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id, "slug": slug,
        "store": "db"}))
    return root, project_id, slug


def _project_env(env: dict, root: Path) -> dict:
    return {**env, "CLAUDE_PROJECT_DIR": str(root)}


def _policy(store, slug: str, name: str):
    return store.config_get("capabilities", "policy", name,
                            S.Scopes(slug, include_global=False))


def test_database_mode_records_go_through_the_setting(tmp_path, enforced):
    env = _env(tmp_path)
    root, project_id, slug = _db_project(tmp_path, env)
    _set_cluster(env, enforced)
    url = S.store_setting_url(S.read_store_setting(env["XDG_CONFIG_HOME"]))
    with S.PostgresStore.open(url) as store:
        store.migrate()
        store.project_register(project_id, slug)
    result = _manager(_project_env(env, root), "enable", "slack", "--project", cwd=root)
    _no_secret(result)
    assert result.returncode == 0, result.stdout + result.stderr
    with S.PostgresStore.open(url) as store:
        assert _policy(store, slug, "slack") == {"enabled": True}
    assert not (Path(env["XDG_STATE_HOME"]) / "capabilities" / "store.db").exists()
    assert not (root / "capabilities" / "settings.json").exists()
    listed = _ok(_manager(_project_env(env, root), "list", cwd=root))
    assert "slack" in listed["enabled_not_installed"]


def test_the_override_wins_over_the_setting(tmp_path, enforced):
    env = _env(tmp_path)
    root, project_id, slug = _db_project(tmp_path, env)
    _set_cluster(env, enforced)
    url = S.store_setting_url(S.read_store_setting(env["XDG_CONFIG_HOME"]))
    with S.PostgresStore.open(url) as store:
        store.migrate()
        store.project_register(project_id, slug)
    override = tmp_path / "override.db"
    with S.SQLiteStore.open(str(override)) as store:
        store.migrate()
        store.project_register(project_id, slug)
    extra = {"CAPABILITIES_STORE_URL": str(override)}
    _ok(_manager(_project_env(env, root), "enable", "notion", "--project", cwd=root,
                 extra=extra))
    with S.SQLiteStore.open(str(override)) as store:
        assert _policy(store, slug, "notion") == {"enabled": True}
    with S.PostgresStore.open(url) as store:
        assert _policy(store, slug, "notion") is None
    assert _ok(_manager(env, "path", "store", extra=extra)) == {
        "store": str(override), "source": "CAPABILITIES_STORE_URL"}


def test_without_a_setting_records_resolve_as_before(tmp_path):
    env = _env(tmp_path)
    # A database-mode project with today's URL source.
    root, project_id, slug = _db_project(tmp_path, env)
    today = tmp_path / "today.db"
    with S.SQLiteStore.open(str(today)) as store:
        store.migrate()
        store.project_register(project_id, slug)
    _ok(_manager(_project_env(env, root), "enable", "notion", "--project", cwd=root,
                 extra={"CAPABILITIES_STORE_URL": str(today)}))
    with S.SQLiteStore.open(str(today)) as store:
        assert _policy(store, slug, "notion") == {"enabled": True}
    # The same project with no URL at all reaches the local default.
    default = Path(env["XDG_STATE_HOME"]) / "capabilities" / "store.db"
    default.parent.mkdir(parents=True)
    shutil.copy2(today, default)
    _ok(_manager(_project_env(env, root), "enable", "slack", "--project", cwd=root))
    with S.SQLiteStore.open(str(default)) as store:
        assert _policy(store, slug, "slack") == {"enabled": True}
    assert _ok(_manager(env, "path", "store")) == {"store": str(default), "source": "default"}
    # A files-mode project keeps its gate in settings.json.
    files = tmp_path / "files-project"
    (files / ".git").mkdir(parents=True)
    (files / "capabilities").mkdir()
    _ok(_manager(_project_env(env, files), "enable", "slack", "--project", cwd=files))
    assert json.loads((files / "capabilities" / "settings.json").read_text()) == {
        "capabilities": {"slack": {"enabled": True}}}
    assert _nothing_written(env)

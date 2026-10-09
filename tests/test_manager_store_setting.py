"""The database a project uses, and the machine's level of it:
`capabilities store set|show|doctor|unset`.

The shared database library resolves it per project through one cascade - the
project's `.env.local`/`.env`, then the process environment, then the machine's
store setting file `$XDG_CONFIG_HOME/agentkit/store.json` - and the manager asks
it, never deciding the order itself. `store show` and `store doctor` report every
level for a project; `store set` and `store unset` write only the machine file,
in the format the library reads.

Everything runs against a scratch HOME. The doctor tests build a throwaway
PostgreSQL with TLS on a loopback port when `initdb`, `pg_ctl` and `openssl` are
on PATH and psycopg is importable, and skip otherwise.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
# Distinctive, and carrying every character a URL or an env file could mangle,
# so its absence from output is meaningful and its round trip is proven.
PASSWORD = "pw-Zq7!x@%3A/#?&= 'q\"\\end"
# The same secret as a URL may carry it, percent-encoded.
URL_PASSWORD = "urlpw-Zq7x9secret"
ADMIN = "pgadmin"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")


@pytest.fixture(autouse=True)
def _no_ambient_database(monkeypatch):
    """The library reads the process environment, so this process carries none."""
    for key in DB_KEYS:
        monkeypatch.delenv(key, raising=False)


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR",
                "CAPABILITIES_AUTH_CONTEXT", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_DEV_SESSION",
                "CAPABILITIES_WORKSPACE", "STORE_FIX_PASSWORD", *DB_KEYS):
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


def _no_secret(result: subprocess.CompletedProcess, *secrets: str) -> None:
    for secret in (PASSWORD, URL_PASSWORD, *secrets):
        assert secret not in result.stdout and secret not in result.stderr


def _family(env: dict) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "agentkit" / "store.json"


def _written(env: dict) -> dict:
    return json.loads(_family(env).read_text())


def _legacy_files(env: dict) -> tuple[Path, Path]:
    """The pair the manager once read, which nothing reads or writes now."""
    home = Path(env["XDG_CONFIG_HOME"]) / "capabilities"
    return home / "store.json", home / "credentials.env"


def _nothing_written(env: dict) -> bool:
    return not _family(env).exists()


def _library_reads(env: dict):
    """The machine level as the shared database library reads it."""
    from capabilities_contract import db
    return db.resolve_setting(None, config_home=env["XDG_CONFIG_HOME"])


def _project(tmp_path: Path, env_text: str = "", name: str = ".env") -> Path:
    root = tmp_path / "consumer"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    if env_text:
        (root / name).write_text(env_text)
    return root


BASE = ("--host", "db.example.test", "--database", "app", "--user", "agent")


def _set(env: dict, *extra_args: str, password: str = PASSWORD,
         extra: dict | None = None) -> subprocess.CompletedProcess:
    return _manager(env, "store", "set", *BASE, *extra_args, "--password-stdin",
                    stdin=password + "\n", extra=extra)


# --- set ----------------------------------------------------------------------

def test_set_takes_the_password_from_stdin_and_writes_what_the_library_reads(tmp_path):
    env = _env(tmp_path)
    result = _set(env)
    _no_secret(result)
    payload = _ok(result)
    assert payload["changed"] is True and payload["configured"] is True
    assert payload["in_force"] == "machine"
    assert payload["setting"]["password"] == "***"
    assert payload["setting"]["sources"] == [str(_family(env))]
    assert _written(env)["password"] == PASSWORD
    read = _library_reads(env)
    assert (read.level, read.host, read.port, read.database, read.user, read.sslmode,
            read.schema, read.password) == ("machine", "db.example.test", 5432, "app",
                                            "agent", "require", "agentkit", PASSWORD)


def test_set_takes_the_password_from_a_file(tmp_path):
    env = _env(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text(PASSWORD + "\n")
    result = _manager(env, "store", "set", *BASE, "--password-file", str(secret))
    _no_secret(result)
    _ok(result)
    assert _library_reads(env).password == PASSWORD


def test_set_takes_the_password_from_a_named_environment_variable(tmp_path):
    env = _env(tmp_path)
    result = _manager(env, "store", "set", *BASE, "--password-env", "STORE_FIX_PASSWORD",
                      extra={"STORE_FIX_PASSWORD": PASSWORD})
    _no_secret(result)
    _ok(result)
    assert _library_reads(env).password == PASSWORD
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
    assert payload["setting"]["sslmode"] == "disable"
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
    assert payload["setting"]["sslmode"] == mode
    written = _written(env)
    assert written["sslmode"] == mode
    if extra:
        assert written["sslrootcert"] == str(root.resolve())


def test_set_defaults_the_port_and_the_sslmode(tmp_path):
    env = _env(tmp_path)
    payload = _ok(_set(env))
    assert payload["setting"]["port"] == 5432
    assert payload["setting"]["sslmode"] == "require"
    root = tmp_path / "root.crt"
    root.write_text("fixture certificate\n")
    payload = _ok(_set(env, "--sslrootcert", str(root), "--port", "6543"))
    assert payload["setting"]["sslmode"] == "verify-full"
    assert payload["setting"]["port"] == 6543
    assert _written(env)["port"] == 6543


def test_set_refuses_a_port_that_is_not_one(tmp_path):
    env = _env(tmp_path)
    result = _set(env, "--port", "seventy")
    assert result.returncode == 6 and _error(result)["code"] == "bad_store_setting"
    _no_secret(result)
    assert str(tmp_path) not in _error(result)["message"]
    assert _nothing_written(env)


def test_set_writes_the_machine_file_whole_at_0600_and_leaves_the_legacy_pair(tmp_path):
    env = _env(tmp_path)
    setting_file, password_file = _legacy_files(env)
    setting_file.parent.mkdir(parents=True)
    setting_file.write_text('{"schema": "capabilities.store.v1"}')
    password_file.write_text("OTHER_KEY=kept\n")
    legacy_before = (setting_file.read_text(), password_file.read_text())
    payload = _ok(_set(env))
    path = _family(env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _written(env) == {
        "schema": "agentkit.store.v1", "host": "db.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require", "password": PASSWORD}
    assert sorted(p.name for p in path.parent.iterdir()) == ["store.json"]
    assert (setting_file.read_text(), password_file.read_text()) == legacy_before
    assert payload["setting"]["host"] == "db.example.test"
    again = _ok(_set(env))
    assert again["changed"] is False
    changed = _ok(_set(env, password="another-" + PASSWORD))
    assert changed["changed"] is True
    assert _written(env)["password"] == "another-" + PASSWORD
    assert _ok(_manager(env, "store", "unset"))["changed"] is True
    assert (setting_file.read_text(), password_file.read_text()) == legacy_before


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
    shown = _ok(_manager(env, "store", "show"))
    assert shown["levels"]["machine"]["error"]["code"] == "store_setting_too_new"
    assert shown["error"]["code"] == "store_setting_too_new"
    assert shown["in_force"] == "machine" and shown["configured"] is False


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
    _ok(_set(env))
    written = _written(env)
    assert written["schema"] == "agentkit.store.v1" and "db_schema" not in written
    payload = _ok(_set(env, "--schema", "agentkit"))
    assert payload["changed"] is True
    assert _written(env)["db_schema"] == "agentkit"
    assert payload["setting"]["schema"] == "agentkit"
    assert _ok(_set(env, "--schema", "agentkit"))["changed"] is False
    assert _ok(_set(env, "--schema", "tools"))["setting"]["schema"] == "tools"
    assert _library_reads(env).schema == "tools"
    assert _ok(_set(env))["setting"]["schema"] == "agentkit"
    assert "db_schema" not in _written(env)


@pytest.mark.parametrize("name", ["public", "information_schema", "pg_toast", "Tools",
                                  "1tools", "tools-x", ""])
def test_set_refuses_a_schema_that_is_reserved_or_not_an_identifier(tmp_path, name):
    env = _env(tmp_path)
    result = _set(env, "--schema", name)
    assert result.returncode == 6 and _error(result)["code"] == "bad_schema_name"
    assert _nothing_written(env)


# --- unset --------------------------------------------------------------------

def test_unset_removes_the_machine_file(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env, "--schema", "tools"))
    result = _manager(env, "store", "unset")
    _no_secret(result)
    payload = _ok(result)
    assert payload["changed"] is True and payload["configured"] is False
    assert payload["in_force"] is None
    assert not _family(env).exists()
    assert _ok(_manager(env, "store", "unset"))["changed"] is False


def test_unset_removes_a_setting_that_no_longer_reads(tmp_path):
    env = _env(tmp_path)
    path = _family(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    shown = _ok(_manager(env, "store", "show"))
    assert shown["levels"]["machine"]["error"]["code"] == "bad_store_setting"
    assert _ok(_manager(env, "store", "unset"))["changed"] is True
    assert not path.exists()


def test_unset_is_refused_under_the_read_only_switch(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    result = _manager(env, "store", "unset", extra={"CAPABILITIES_READ_ONLY": "1"})
    assert result.returncode == 4 and _error(result)["code"] == "read_only_switch"
    assert _written(env)["password"] == PASSWORD


# --- show: every level of the cascade ------------------------------------------

def test_show_with_no_level_answering(tmp_path):
    env = _env(tmp_path)
    payload = _ok(_manager(env, "store", "show", "--json"))
    assert payload["configured"] is False and payload["setting"] is None
    assert payload["in_force"] is None and payload["error"] is None
    assert payload["project"] is None
    levels = payload["levels"]
    assert levels["project"] == {"asked": False, "answers": False, "setting": None,
                                 "error": None}
    assert levels["environment"]["answers"] is False
    assert levels["machine"]["path"] == str(_family(env))
    assert Path(levels["machine"]["path"]).is_absolute()
    assert levels["machine"]["exists"] is False and levels["machine"]["answers"] is False


def test_show_reports_the_machine_level_without_its_secret(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env, "--port", "6543"))
    result = _manager(env, "store", "show")
    _no_secret(result)
    payload = _ok(result)
    assert payload["in_force"] == "machine"
    assert payload["setting"] == {
        "level": "machine", "sources": [str(_family(env))], "schema": "agentkit",
        "host": "db.example.test", "port": 6543, "database": "app", "user": "agent",
        "sslmode": "require", "sslrootcert": None, "password": "***"}
    assert payload["levels"]["machine"]["setting"] == payload["setting"]
    assert payload["levels"]["machine"]["exists"] is True


def test_the_project_level_wins_and_every_level_is_still_reported(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    root = _project(tmp_path, f"AGENTKIT_DB_URL=postgresql://agent:{URL_PASSWORD}"
                              "@project.example.test:5433/projectdb\n"
                              "AGENTKIT_DB_SCHEMA=projectschema\n")
    for args, cwd in ((("--project", str(root)), None), ((), root)):
        result = _manager(env, "store", "show", *args, cwd=cwd,
                          extra={"AGENTKIT_DB_HOST": "env.example.test",
                                 "AGENTKIT_DB_NAME": "envdb", "AGENTKIT_DB_USER": "u"})
        _no_secret(result)
        payload = _ok(result)
        assert payload["project"] == str(root.resolve())
        assert payload["in_force"] == "project"
        setting = payload["setting"]
        assert setting["level"] == "project" and setting["schema"] == "projectschema"
        assert setting["sources"] == [str(root.resolve() / ".env")]
        assert setting["url"]["host"] == "project.example.test"
        assert setting["url"]["password"] == "***"
        levels = payload["levels"]
        assert levels["project"]["asked"] is True and levels["project"]["answers"] is True
        assert levels["environment"]["setting"]["host"] == "env.example.test"
        assert levels["environment"]["setting"]["sources"] == [
            "AGENTKIT_DB_HOST", "AGENTKIT_DB_NAME", "AGENTKIT_DB_USER"]
        assert levels["machine"]["setting"]["host"] == "db.example.test"


def test_env_local_wins_key_by_key_over_env(tmp_path):
    env = _env(tmp_path)
    root = _project(tmp_path, "AGENTKIT_DB_HOST=from-env.example.test\n"
                              "AGENTKIT_DB_NAME=app\nAGENTKIT_DB_USER=agent\n")
    (root / ".env.local").write_text("AGENTKIT_DB_HOST=from-local.example.test\n")
    payload = _ok(_manager(env, "store", "show", "--project", str(root)))
    assert payload["setting"]["host"] == "from-local.example.test"
    assert payload["setting"]["database"] == "app"
    assert sorted(payload["setting"]["sources"]) == sorted(
        [str(root.resolve() / ".env.local"), str(root.resolve() / ".env")])


def test_the_environment_level_answers_outside_a_project(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    result = _manager(env, "store", "show", extra={
        "AGENTKIT_DB_URL": f"postgresql://agent:{URL_PASSWORD}@env.example.test/envdb"})
    _no_secret(result)
    payload = _ok(result)
    assert payload["in_force"] == "environment"
    assert payload["setting"]["sources"] == ["AGENTKIT_DB_URL"]
    assert payload["levels"]["project"]["asked"] is False
    assert payload["levels"]["machine"]["answers"] is True


def test_a_level_the_library_refuses_is_reported_as_refused(tmp_path):
    env = _env(tmp_path)
    _ok(_set(env))
    root = _project(tmp_path, "AGENTKIT_DB_HOST=remote.example.test\nAGENTKIT_DB_NAME=app\n"
                              "AGENTKIT_DB_USER=agent\nAGENTKIT_DB_SSLMODE=disable\n")
    payload = _ok(_manager(env, "store", "show", "--project", str(root)))
    assert payload["configured"] is False and payload["setting"] is None
    assert payload["in_force"] == "project"
    assert payload["error"]["code"] == "sslmode_too_weak"
    assert payload["levels"]["project"]["error"]["code"] == "sslmode_too_weak"
    assert payload["levels"]["machine"]["setting"]["host"] == "db.example.test"
    doctor = _manager(env, "store", "doctor", "--project", str(root))
    assert doctor.returncode == 6 and _error(doctor)["code"] == "sslmode_too_weak"


def test_the_retired_variables_and_the_legacy_pair_decide_nothing(tmp_path):
    env = _env(tmp_path)
    setting_file, password_file = _legacy_files(env)
    setting_file.parent.mkdir(parents=True)
    setting_file.write_text(json.dumps({
        "schema": "capabilities.store.v1", "host": "legacy.example.test", "port": 5432,
        "database": "app", "user": "agent", "sslmode": "require"}))
    password_file.write_text("CAPABILITIES_STORE_PASSWORD=old\n")
    payload = _ok(_manager(env, "store", "show", extra={
        "CAPABILITIES_STORE_URL": "postgresql://a@retired.example.test/x",
        "AGENTKIT_STORE_URL": "postgresql://a@retired.example.test/y",
        "CAPABILITIES_STORE_PASSWORD": "old"}))
    assert payload["configured"] is False and payload["in_force"] is None
    doctor = _manager(env, "store", "doctor")
    assert doctor.returncode == 3 and _error(doctor)["code"] == "no_store_setting"


def test_show_and_doctor_take_only_a_project_and_json(tmp_path):
    env = _env(tmp_path)
    for sub in ("show", "doctor"):
        bad = _manager(env, "store", sub, "--verbose")
        assert bad.returncode == 6 and _error(bad)["code"] == "input"
        gone = _manager(env, "store", sub, "--project", str(tmp_path / "absent"))
        assert gone.returncode == 6 and _error(gone)["code"] == "input"


def test_path_store_is_gone(tmp_path):
    env = _env(tmp_path)
    result = _manager(env, "path", "store")
    assert result.returncode == 6 and _error(result)["code"] == "input"


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
            from psycopg import sql
            cur.execute(sql.SQL("CREATE ROLE agent LOGIN PASSWORD {}").format(
                sql.Literal(PASSWORD)))
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
        import psycopg
        return psycopg.connect(host="127.0.0.1", port=self.port, user=user,
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
    pytest.importorskip("psycopg")
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
# machine, so the checks a database across a network gets are the ones that run.
REMOTE_NAME = "127.1"


def _set_cluster(env: dict, cluster: Cluster, *extra_args: str,
                 host: str = REMOTE_NAME, database: str = "app") -> None:
    _ok(_manager(env, "store", "set", "--host", host, "--port", str(cluster.port),
                 "--database", database, "--user", "agent", *extra_args,
                 "--password-stdin", stdin=PASSWORD + "\n"))


def _cluster_url(cluster: Cluster, database: str = "app", host: str = REMOTE_NAME) -> str:
    from urllib.parse import quote
    return (f"postgresql://agent:{quote(PASSWORD, safe='')}@{host}:{cluster.port}/"
            f"{database}?sslmode=require")


# --- doctor -----------------------------------------------------------------------

def test_doctor_passes_when_tls_works_and_plain_text_is_refused(tmp_path, enforced):
    env = _env(tmp_path)
    _set_cluster(env, enforced)
    result = _manager(env, "store", "doctor", "--json")
    _no_secret(result)
    payload = _ok(result)
    assert payload["ok"] is True and payload["in_force"] == "machine"
    tls = payload["checks"]["tls"]
    assert tls["ok"] is True and tls["server_version"]
    assert tls["tls"]["version"].startswith("TLS") and tls["tls"]["cipher"]
    plain = payload["checks"]["plain_text_refused"]
    assert plain["ok"] is True and "no encryption" in plain["reason"]
    assert payload["setting"]["host"] == REMOTE_NAME
    assert payload["setting"]["port"] == enforced.port
    assert payload["db_schema"] == "agentkit" and payload["sslmode"] == "require"
    assert payload["checks"]["schema"]["ok"] is True
    assert payload["checks"]["schema"]["exists"] is False


def test_doctor_probes_the_project_level_when_it_answers(tmp_path, enforced):
    """The machine level names a database the role may not create its schema
    in; the project's URL names one it may. The project's is the one probed."""
    env = _env(tmp_path)
    _set_cluster(env, enforced, "--schema", "tools", database="foreign_db")
    machine_only = _manager(env, "store", "doctor")
    assert machine_only.returncode == 7, machine_only.stdout + machine_only.stderr
    root = _project(tmp_path, f"AGENTKIT_DB_URL={_cluster_url(enforced)}\n", name=".env.local")
    for args, cwd in ((("--project", str(root)), None), ((), root)):
        result = _manager(env, "store", "doctor", *args, cwd=cwd)
        _no_secret(result)
        payload = _ok(result)
        assert payload["ok"] is True and payload["in_force"] == "project"
        assert payload["setting"]["sources"] == [str(root.resolve() / ".env.local")]
        assert payload["setting"]["url"]["dbname"] == "app"
        assert payload["setting"]["url"]["password"] == "***"
        assert payload["checks"]["plain_text_refused"]["ok"] is True
        assert payload["checks"]["schema"]["name"] == "agentkit"
        assert payload["levels"]["machine"]["setting"]["database"] == "foreign_db"


def test_doctor_probes_the_environment_level(tmp_path, enforced):
    env = _env(tmp_path)
    result = _manager(env, "store", "doctor", extra={
        "AGENTKIT_DB_HOST": REMOTE_NAME, "AGENTKIT_DB_PORT": str(enforced.port),
        "AGENTKIT_DB_NAME": "app", "AGENTKIT_DB_USER": "agent",
        "AGENTKIT_DB_PASSWORD": PASSWORD, "AGENTKIT_DB_SCHEMA": "envschema"})
    _no_secret(result)
    payload = _ok(result)
    assert payload["ok"] is True and payload["in_force"] == "environment"
    assert payload["db_schema"] == "envschema"
    assert payload["levels"]["machine"]["answers"] is False


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
    _set_cluster(env, enforced, "--schema", "tools", database="foreign_db")
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


def test_no_manager_verb_creates_a_table(tmp_path, enforced):
    """Records are files; with a database configured at every level the
    manager's verbs still create nothing in it."""
    env = _env(tmp_path)
    _set_cluster(env, enforced)
    root = _project(tmp_path, f"AGENTKIT_DB_URL={_cluster_url(enforced)}\n")
    (root / "capabilities").mkdir()
    project_env = {**env, "CLAUDE_PROJECT_DIR": str(root)}
    for args in (("init",), ("enable", "slack", "--project"), ("list",),
                 ("relabel", "renamed-fixture"), ("store", "show"), ("store", "doctor"),
                 ("doctor",)):
        _manager(project_env, *args, cwd=root)
    assert json.loads((root / "capabilities" / "settings.json").read_text())[
        "capabilities"]["slack"] == {"enabled": True}
    admin = enforced.connect(ADMIN, "app")
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT schemaname, tablename FROM pg_tables WHERE schemaname "
                        "NOT IN ('pg_catalog', 'information_schema')")
            assert cur.fetchall() == []
    finally:
        admin.close()
    assert not list(tmp_path.rglob("*.db"))

"""`capabilities migrate`: an agent's file-held configuration moved into the store.

One machine is built for the module: a config home, a registry whose entries
are this checkout's capabilities, a throwaway PostgreSQL on a loopback port
holding the one store every agent uses, AgentKit's agent list naming every
agent root, and a launchctl that only records what it is asked. On it stand
agents shaped like the ones the transition meets: one heavy on telegram with a
deployment, one with legacy automations for production alone and for two
environments, one with whatsapp profile files, one with tasks workers,
profiles and a conveyor, one whose body sits in a subfolder, and one whose
records were already in the store with files gone stale.

The tests run in file order, as the transition does: refusals that need the
global rows absent, the global rows, each agent's dry run and then its apply,
an interrupted run, and the report and discard. The module skips without
`initdb`, `pg_ctl` or psycopg.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "lab"
MASTER = "mac1"
NAMES = ("agentrooms", "askproject", "automations", "coolify", "deployment", "memory",
         "slack", "tasks", "telegram", "whatsapp")
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
EVIDENCE = os.environ.get("CAPABILITIES_MIGRATE_EVIDENCE")

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _say(name: str, value) -> None:
    """Keep what a step answered, where the run was asked to keep it."""
    if EVIDENCE:
        Path(EVIDENCE).mkdir(parents=True, exist_ok=True)
        (Path(EVIDENCE) / f"{name}.json").write_text(
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str))


# --- the machine ------------------------------------------------------------------

LAUNCHCTL = """#!/bin/sh
echo "$@" >> "{log}"
case "$1" in
  print) grep -qx "${{2##*/}}" "{loaded}" 2>/dev/null && exit 0; exit 113 ;;
  bootout) grep -vx "${{2##*/}}" "{loaded}" > "{loaded}.tmp"; mv "{loaded}.tmp" "{loaded}"; exit 0 ;;
  bootstrap) echo "$3" >> "{loaded}"; exit 0 ;;
esac
exit 0
"""

CONTEXTKIT = """#!/bin/sh
# Answers `contextkit path capabilities` from the binding's body root.
if [ "$1" = "path" ] && [ "$2" = "capabilities" ]; then
  exec python3 -c 'import tomllib,os; c=tomllib.load(open(".contextkit/config.toml","rb")); b=(c.get("body") or {}).get("root") or "."; print(os.path.abspath(os.path.join(b, "capabilities")))'
fi
echo '{"error": {"code": "unsupported"}}' >&2
exit 2
"""


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.data = tmp / "pg"
        self.port = _free_port()
        self.home = tmp / "home"
        self.config = tmp / "config"
        self.state = tmp / "state"
        self.cache = tmp / "cache"
        self.registry = tmp / "registry"
        self.shims = tmp / "shims"
        self.launchctl_log = tmp / "launchctl.log"
        self.loaded = tmp / "launchctl.loaded"
        self.database = "t_" + uuid.uuid4().hex[:12]
        self.roots: dict[str, Path] = {}
        for d in (self.home, self.config, self.state, self.cache, self.registry, self.shims):
            d.mkdir(parents=True)
        (self.shims / "launchctl").write_text(LAUNCHCTL.format(log=self.launchctl_log,
                                                               loaded=self.loaded))
        (self.shims / "contextkit").write_text(CONTEXTKIT)
        for shim in self.shims.iterdir():
            shim.chmod(0o755)
        self.loaded.write_text("")

    # PostgreSQL
    def pg(self, *argv: str) -> None:
        env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
        result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, (argv, result.stdout + result.stderr)

    def start(self) -> None:
        # A running cluster given by its loopback port (superuser pgadmin,
        # trust) is reused, with a database of this run's own.
        port = os.environ.get("CAPABILITIES_TEST_PG_PORT")
        if port:
            self.port, self.external = int(port), True
            with self.admin() as conn:
                conn.execute(f"CREATE DATABASE {self.database} ENCODING 'UTF8' "
                             "TEMPLATE template0")
            return
        self.pg("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                "--locale=en_US.UTF-8", "--auth=trust")
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       "unix_socket_directories = ''\n")
        self.pg("pg_ctl", "-D", str(self.data), "-l", str(self.tmp / "pg.log"), "-w", "-t",
                "60", "start")
        with self.admin() as conn:
            conn.execute(f"CREATE DATABASE {self.database} ENCODING 'UTF8' TEMPLATE template0")

    def stop(self) -> None:
        if getattr(self, "external", False):
            with self.admin() as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {self.database} WITH (FORCE)")
            return
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "stop"],
                       capture_output=True, timeout=60,
                       env={**os.environ, "LC_ALL": "en_US.UTF-8"})

    def admin(self, database: str = "postgres"):
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN, dbname=database,
                               connect_timeout=10)
        conn.autocommit = True
        return conn

    def url(self) -> str:
        return f"postgresql://{ADMIN}@127.0.0.1:{self.port}/{self.database}?sslmode=disable"

    def sql(self, query: str, params: tuple = ()):
        with self.admin(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def tables(self) -> dict[str, str]:
        """Every table of the store, by a digest of its rows."""
        out = {}
        for (table,) in self.sql("SELECT tablename FROM pg_tables WHERE schemaname = %s "
                                 "ORDER BY tablename", (SCHEMA,)):
            rows = self.sql(f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), '
                            f"'')) FROM {SCHEMA}.\"{table}\" t")
            out[table] = rows[0][0]
        return out

    # The registry: each entry is this checkout's capability, as an install leaves it.
    def install(self, registry: Path, name: str, script: Path | None = None) -> None:
        entry = registry / name
        entry.mkdir(parents=True, exist_ok=True)
        target = entry / name
        if target.exists() or target.is_symlink():
            target.unlink()
        if script is None:
            target.symlink_to(REPO / "capabilities" / name / "bin" / name)
        else:
            shutil.copyfile(script, target)
            target.chmod(0o755)
        manifest = self.run([str(REPO / "capabilities" / name / "bin" / name), "manifest",
                             "--json"], cwd=self.tmp)
        assert manifest.returncode == 0, manifest.stderr
        (entry / "manifest.json").write_text(manifest.stdout)

    def env(self, root: Path | None = None, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in DB_KEYS and not k.startswith("CAPABILITIES_")
               and k not in ("AGENT_NODE_NAME", "VIRTUAL_ENV", "CLAUDE_PROJECT_DIR")}
        env.update({
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state), "XDG_CACHE_HOME": str(self.cache),
            "CAPABILITIES_HOME": str(self.registry), "USER": "tester",
            "AGENT_NODE_NAME": MASTER, "AGENTKIT_DB_URL": self.url(),
            "AGENTKIT_DB_SCHEMA": SCHEMA,
            "PATH": f"{self.shims}{os.pathsep}{os.environ.get('PATH', '')}",
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR")
            or str(Path.home() / ".cache" / "uv"),
        })
        if root is not None:
            env["CLAUDE_PROJECT_DIR"] = str(root)
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, argv: list[str], cwd: Path, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, env=self.env(cwd if cwd in self.roots.values()
                                                          else None, **extra),
                              text=True, capture_output=True, timeout=1800)

    def manager(self, root: Path, *args: str, **extra) -> subprocess.CompletedProcess:
        return self.run([str(MANAGER), *args], cwd=root, **extra)

    def cap(self, root: Path, name: str, *args: str, **extra) -> subprocess.CompletedProcess:
        return self.run([str(REPO / "capabilities" / name / "bin" / name), *args], cwd=root,
                        **extra)

    @staticmethod
    def answer(result: subprocess.CompletedProcess) -> dict:
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    @staticmethod
    def error(result: subprocess.CompletedProcess) -> dict:
        assert result.returncode != 0, result.stdout
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return json.loads(line)["error"]

    # Agents
    def write(self, path: Path, body) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if isinstance(body, str) else json.dumps(body, indent=2))

    def agent(self, name: str, agent_id: str | None, files: dict, *, body: str | None = None,
              bound_id: str | None = None, git: bool = True, ignore: tuple = ()) -> Path:
        root = self.tmp / "agents" / name
        root.mkdir(parents=True)
        base = root / body if body else root
        envelope = base / "capabilities"
        identity = {"schema": "capabilities.project.v1", "slug": name}
        if agent_id:
            identity["id"] = agent_id
        self.write(envelope / "project.json", identity)
        if bound_id:
            self.write(root / ".contextkit" / "config.toml",
                       'version = 1\ntype = "agent-project"\n\n'
                       + (f'[body]\nroot = "{body}"\n\n' if body else "")
                       + f'[identity]\nname = "{name}"\nid = "{bound_id}"\n')
        for relative, content in files.items():
            self.write(root / relative.replace("{env}", str(envelope.relative_to(root))),
                       content)
        if ignore:
            self.write(root / ".gitignore", "".join(f"{line}\n" for line in ignore))
        if git:
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.org",
                            "-c", "user.name=t", "commit", "-qm", "agent"], check=True)
        self.roots[name] = root
        return root

    def files(self, root: Path) -> dict[str, str]:
        out = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and ".git" not in path.parts:
                out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def everything(self) -> dict:
        """The store and every file the run could touch, as digests."""
        return {"tables": self.tables(),
                "roots": {name: self.files(root) for name, root in self.roots.items()},
                "config": self.files(self.config), "state": self.files(self.state),
                "launchctl": self.launchctl_log.read_text()
                if self.launchctl_log.exists() else ""}

    def agentkit_list(self) -> None:
        listing = self.home / "Library" / "Application Support" / "AgentKit" / "agents.plist"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_bytes(plistlib.dumps([{"path": str(root)} for root in self.roots.values()]))

    def through_switch(self, root: Path, names: list[str]) -> dict:
        """What the records adapter - the switch every capability opens its
        records through - resolves for this agent on the master."""
        envelope = (root / "agent" / "capabilities" if (root / "agent").is_dir()
                    else root / "capabilities")
        saved = dict(os.environ)
        os.environ.update({"AGENTKIT_DB_URL": self.url(), "AGENTKIT_DB_SCHEMA": SCHEMA,
                           "AGENT_NODE_NAME": MASTER, "HOME": str(self.home)})
        try:
            records = S.open_records(envelope, self.config, project_root=root)
            out = {"adapter": type(records).__name__, "gate": {}, "connections": {},
                   "defaults": {}, "identifiers": {}, "settings": {}}
            try:
                out["gate"] = {k: r["value"] for k, r in
                               records.resolve("capabilities", "policy").items()}
                for name in names:
                    for write_default in (False, True):
                        out["connections"][f"{name}/{write_default}"] = {
                            cid: [e["value"], e["allow_write"]] for cid, e in
                            records.connections(name, write_default=write_default).items()}
                    out["defaults"][name] = records.get(name, "setting", "connection.default")
                    out["identifiers"][name] = {k: [r["value"], r["note"] or ""] for k, r in
                                                records.resolve(name, "identifier").items()}
            finally:
                records.close()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        # Settings as the contract's settings reader gives them to the
        # capability: its project home's document and each declared key.
        for name in names:
            manifest = json.loads((self.registry / name / "manifest.json").read_text())
            if not manifest.get("settings"):
                continue
            shown = self.cap(root, name, "settings", "--scope", "project")
            if shown.returncode != 0:
                out["settings"][name] = {"error": self.error(shown)["code"]}
                continue
            report = json.loads(shown.stdout)
            out["settings"][name] = {
                "document": next((h["document"] for h in report["homes"]), None),
                "keys": {k["key"]: k["value"] for k in report["keys"]}}
        return out


@pytest.fixture(scope="module")
def world():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="capabilities-migrate-")).resolve()
    found = World(tmp)
    try:
        found.start()
        for name in NAMES:
            found.install(found.registry, name)
        build_agents(found)
        yield found
    finally:
        found.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# --- the agents ---------------------------------------------------------------------

IDS = {"relay": "prj_aaaaaaaaaaa1", "books": "prj_bbbbbbbbbbb1", "clinic": "prj_ccccccccccc1",
       "admin": "prj_ddddddddddd1", "body": "prj_eeeeeeeeeee1", "kept": "prj_fffffffffff1",
       "frozen": "prj_99999999aaa1", "later": "prj_99999999bbb1", "other": "prj_99999999ccc1",
       "split": "prj_99999999ddd1", "split-bound": "prj_99999999eee1"}

WORKER = ("---\nenabled: true\ntakes: [change]\nprofile: claude-act\n---\n\nYOUR PART IN IT\n\nDo the task "
          "within this project's own authority and leave it where the work leaves it.\n")
PROFILE = ('harness = "claude"\nmodel = "opus"\npermission_mode = "bypassPermissions"\n'
           'setting_sources = ["user", "project", "local"]\ntimeout_seconds = 3600\n')


def gate(*names: str, off: tuple = ()) -> dict:
    return {"capabilities": {**{n: {"enabled": True} for n in names},
                             **{n: {"enabled": False} for n in off}}}


def build_agents(w: World) -> None:
    # The config home: what --global writes as the rows every agent inherits.
    w.write(w.config / "capabilities" / "settings.json", gate("coolify", "memory"))
    w.write(w.config / "coolify" / "connections.json", {
        "default": "ops", "connections": {"ops": {"base_url": "https://coolify.example.org",
                                                  "token_env": "COOLIFY_TOKEN",
                                                  "allow_write": True}}})
    w.write(w.config / "coolify" / "identifiers.json",
            {"team": {"value": "0", "note": "the default team"}})
    w.write(w.config / "memory" / "service" / "settings.json", {"max_entries": 500})

    w.agent("relay", IDS["relay"], {
        "{env}/settings.json": gate("telegram", "coolify", "memory"),
        "{env}/telegram/connections.json": {
            "default": "main", "connections": {"main": {"session": "main",
                                                        "api_id_env": "TG_API_ID",
                                                        "api_hash_env": "TG_API_HASH"}}},
        "{env}/telegram/service/settings.json": {
            "connection": "main", "assistant_name": "Relay",
            "direct_messages": {"mode": "allowlist", "default_role": "direct_user"},
            "allowed_users": {"1001": {"name": "Owner", "role": "supervisor",
                                       "delegation": {"project": "/Users/someone/dev/ops-agent"}}},
            "allowed_groups": {"-1002": {"name": "Team", "member_role": "group_member",
                                         "members": {"1001": {"name": "Owner"}}}},
            "control": {"roles": {"supervisor": {"commands": ["*"]}, "direct_user": {}}}},
        "{env}/coolify/identifiers.json": {"app": {"value": "app-uuid-1", "note": "the app"}},
        "{env}/memory/service/settings.json": {"window_days": 14},
        "deployment/runtime.json": {
            "schema": "capabilities.deployment.runtime.v1", "name": "relay",
            "profile": "container",
            "service_policy": {"auto_include": True, "default_mode": "embedded",
                               "capabilities": {}}},
        "deployment/targets/production.json": {
            "schema": "capabilities.deployment.target.v1", "name": "production",
            "environment": "production", "provider": "coolify", "connection": "ops",
            "node": "srv-relay", "resource": {"type": "service"}},
        "deployment/targets/local.json": {
            "schema": "capabilities.deployment.target.v1", "name": "local",
            "environment": "local", "provider": "manual", "connection": None,
            "resource": {"compose_file": "docker-compose.yaml"}},
    })
    # The working tree differs from the commit: the old worker settings that
    # `service migrate-profiles` turns into a profile.
    relay = w.roots["relay"] / "capabilities" / "telegram" / "service" / "settings.json"
    edited = json.loads(relay.read_text())
    edited["defaults"] = {"worker": "claude", "workers": {"claude": {"model": "opus"}}}
    w.write(relay, edited)
    w.agent("books", IDS["books"], {
        "{env}/settings.json": gate("automations"),
        "{env}/automations/service/config.toml": (
            'version = 1\n\n[engine]\ntick_seconds = 1.0\nmax_parallel = 4\n'
            'environment = "development"\ntimezone = "Europe/Tallinn"\n\n'
            '[agents]\ndefault = "books"\n\n[agents.workers.books]\nengine = "claude"\n'
            'model = "sonnet"\nmode = "read"\n\n'
            '[[automations]]\nid = "daily-books"\nscript = "capabilities/automations/scripts/daily.py"\n'
            'schedule = "0 6 * * *"\nenvironments = ["production"]\n\n'
            '[[automations]]\nid = "watch"\nscript = "capabilities/automations/scripts/watch.py"\n'
            'every_seconds = 300\nenvironments = ["development", "production"]\n\n'
            '[[automations]]\nid = "by-hand"\nscript = "capabilities/automations/scripts/hand.py"\n'),
        "{env}/automations/scripts/daily.py": "print('daily')\n",
        "{env}/automations/scripts/watch.py": "print('watch')\n",
        "{env}/automations/scripts/hand.py": "print('hand')\n",
    })
    w.agent("clinic", IDS["clinic"], {
        "{env}/settings.json": gate("whatsapp"),
        "{env}/whatsapp/connections.json": {"connections": {"clinic": {"engine": "waha",
                                                                       "phone": "+10000000000"}}},
        "{env}/whatsapp/service/settings.json": {"connection": "clinic",
                                                 "dialogue": {"profile": "clinic-claude"}},
        "{env}/whatsapp/service/profiles/clinic-claude.toml": PROFILE,
        "{env}/whatsapp/service/profiles/clinic-night.toml": PROFILE.replace('"opus"', '"sonnet"'),
    }, ignore=("capabilities/whatsapp/connections.json",))
    w.agent("admin", IDS["admin"], {
        "{env}/settings.json": gate("tasks"),
        "{env}/tasks/service/config.toml": "version = 1\npoll_seconds = 30\n",
        "{env}/tasks/conveyor.toml": 'escalate_to = ["reviewer", "Owner"]\nstall_after = "6h"\n',
        "{env}/tasks/workers/reviewer.md": WORKER,
        "{env}/tasks/workers/builder.md": WORKER.replace("claude-act", "review-claude"),
        "{env}/tasks/profiles/review-claude.toml": PROFILE,
    })
    # A second checkout of the same agent: verify-only.
    subprocess.run(["git", "-C", str(w.roots["admin"]), "worktree", "add", "-q",
                    str(w.tmp / "agents" / "admin-copy")], check=True)
    w.agent("body", None, {
        "{env}/settings.json": gate("coolify", "askproject", "slack"),
        "{env}/coolify/connections.json": {"connections": {"box": {
            "base_url": "https://box.example.org", "token_env": "BOX_TOKEN"}}},
        "{env}/coolify/identifiers.json": {"server": {"value": "srv-uuid", "note": ""}},
        "{env}/askproject/profiles/peer-read.toml": PROFILE.replace('"opus"', '"haiku"'),
        "{env}/slack/policy.json": {"allowed_users": ["U01"], "allowed_channels": ["C01"]},
        "{env}/agentrooms/connections.json": {"connections": {"bot": {
            "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
            "token_env": "BOT_TOKEN"}}},
        "{env}/agentrooms/service/settings.json": {
            "version": 1, "agents": {"bot": {"profile": "claude-act", "turn_timeout": 600,
                                             "history": 20}}},
    }, body="agent", bound_id=IDS["body"])
    # An agent whose records went into the store earlier and whose files then
    # went stale: imported now, before the config home learns agentrooms.
    kept = w.agent("kept", IDS["kept"], {
        "{env}/settings.json": gate("coolify", "memory"),
        "{env}/coolify/connections.json": {"connections": {"kept": {
            "base_url": "https://kept.example.org", "token_env": "KEPT_TOKEN"}}},
        "{env}/coolify/identifiers.json": {"app": {"value": "kept-app", "note": ""}},
        "{env}/memory/service/settings.json": {"window_days": 21},
    })
    w.answer(w.manager(kept, "connections", "import"))
    w.write(kept / "capabilities" / "coolify" / "connections.json", {"connections": {"kept": {
        "base_url": "https://stale.example.org", "token_env": "KEPT_TOKEN"}}})
    w.write(kept / "capabilities" / "coolify" / "identifiers.json",
            {"app": {"value": "stale-app", "note": ""}})
    w.write(kept / "capabilities" / "settings.json", gate("coolify", off=("memory",)))
    # The global gate gains agentrooms only after that import, so the rows lack it.
    w.write(w.config / "capabilities" / "settings.json", gate("coolify", "memory", "agentrooms"))
    w.agent("frozen", IDS["frozen"], {
        "{env}/settings.json": gate("coolify", "tasks", "memory"),
        "{env}/coolify/identifiers.json": {"app": {"value": "frozen-app", "note": ""}},
        "{env}/memory/service/settings.json": {"window_days": 7},
        "{env}/tasks/workers/reviewer.md": WORKER,
    })
    w.agent("later", IDS["later"], {
        "{env}/settings.json": gate("memory"),
        "{env}/memory/service/settings.json": {"window_days": 3},
        "deployment/runtime.json": {"schema": "capabilities.deployment.runtime.v1",
                                    "name": "later", "profile": "container",
                                    "service_policy": {"auto_include": True,
                                                       "default_mode": "embedded"}},
        "deployment/targets/production.json": {
            "schema": "capabilities.deployment.target.v1", "name": "production",
            "provider": "manual", "node": "srv-later", "resource": {}},
    })
    w.agent("other", IDS["other"], {"{env}/settings.json": gate("memory")})
    w.agent("split", IDS["split"], {"{env}/settings.json": gate("memory")},
            bound_id=IDS["split-bound"])
    # Each capability's tables, as its install migrated them.
    for name in ("tasks", "automations", "whatsapp", "telegram", "memory", "agentrooms"):
        result = w.cap(w.roots["admin"], name, "migrate")
        assert result.returncode == 0, (name, result.stderr)
    # A launchd agent of relay's, loaded.
    plist = w.home / "Library" / "LaunchAgents" / "test.relay.telegram.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": "test.relay.telegram",
                                      "ProgramArguments": ["/bin/sleep", "1"],
                                      "WorkingDirectory": str(w.roots["relay"].resolve())}))
    w.loaded.write_text("test.relay.telegram\n")
    w.agentkit_list()


ANSWERS = {
    "relay": {"nodes": {"srv-relay": {"register": True, "address": "203.0.113.10"}},
               "deployment": {"targets": {"local": None}}},
    "books": {"nodes": {"srv-books": {"register": True}},
              "automations": {"environments": {"production": "srv-books"},
                              "automations": {"watch": "srv-books"}}},
    "clinic": {}, "admin": {}, "body": {}, "kept": {},
}
SWITCH_NAMES = {"relay": ["telegram", "coolify", "memory"], "books": ["automations"],
                "clinic": ["whatsapp"], "admin": ["tasks"],
                "body": ["coolify", "slack", "agentrooms"],
                "kept": ["coolify", "memory"]}


def answers_file(w: World, name: str, body: dict) -> str:
    path = w.tmp / f"answers-{name}.json"
    path.write_text(json.dumps(body))
    return str(path)


# --- refusals that need the global rows absent ---------------------------------------

def test_an_agent_refuses_before_the_global_rows_and_writes_nothing(world):
    root = world.roots["other"]
    before = world.everything()
    error = world.error(world.manager(root, "migrate", "--apply"))
    _say("refusal-global-not-migrated", error)
    assert error["code"] == "global_not_migrated"
    assert world.everything() == before


# --- the global rows -----------------------------------------------------------------

def test_global_dry_run_lists_rows_and_gate_flips_and_writes_nothing(world):
    root = world.roots["other"]
    before = world.everything()
    dry = world.answer(world.manager(root, "migrate", "--global"))
    _say("global-dry-run", dry)
    assert world.everything() == before
    assert dry["applied"] is False and dry["would_write"] is True
    policy = {row["capability"]: row["outcome"] for row in dry["plan"]["policy"]}
    assert policy["agentrooms"] == "add"
    grants = {row["connection"]: row for row in dry["plan"]["grants"]}
    assert grants["ops"]["allow_write"] is True and grants["ops"]["is_default"] is True
    flips = {entry["agent_id"]: entry["flips"] for entry in dry["gate_flips"]}
    assert flips[IDS["kept"]] == [{"capability": "agentrooms", "was": False, "now": True}]
    assert [s["document"] for s in dry["plan"]["settings"]] == ["memory"]


def test_global_apply_writes_the_rows_once(world):
    root = world.roots["other"]
    applied = world.answer(world.manager(root, "migrate", "--global", "--apply"))
    _say("global-apply", applied)
    assert applied["state"] == "migrated" and applied["global_migrated_at"]
    assert world.sql("SELECT enabled FROM capabilities_policy WHERE agent_id IS NULL "
                     "AND capability = 'agentrooms'") == [(True,)]
    # A config-home allow_write is a global grant's allow_write only, never enabled.
    assert world.sql("SELECT enabled, allow_write, is_default FROM "
                     "capabilities_connection_grants WHERE agent_id IS NULL") == [
        (None, True, True)]
    assert world.sql("SELECT path, value FROM capabilities_settings WHERE agent_id IS NULL") \
        == [(["max_entries"], 500)]
    before = world.everything()
    again = world.answer(world.manager(root, "migrate", "--global", "--apply"))
    assert again["changed"] is False and world.everything() == before


# --- refusals ----------------------------------------------------------------------------

def test_refusals_write_nothing(world):
    root = world.roots["other"]
    before = world.everything()
    refused = {}
    refused["node_unnamed"] = world.error(world.manager(root, "migrate", "--apply",
                                                        AGENT_NODE_NAME=None))
    refused["node_mismatch"] = world.error(world.manager(
        root, "migrate", "--apply", "--answers", answers_file(world, "mismatch",
                                                              {"node": "mac9"})))
    refused["question_open"] = world.error(world.manager(world.roots["books"], "migrate",
                                                         "--apply"))
    _say("refusals", refused)
    for code, error in refused.items():
        assert error["code"] == code, error
    assert world.everything() == before
    # Another node is the master of `other`: this one is not.
    world.answer(world.manager(root, "node", "add", "mac-other", "--kind", "mac", "--master",
                               AGENT_NODE_NAME="mac-other"))
    before = world.everything()
    error = world.error(world.manager(root, "migrate", "--apply"))
    _say("refusal-not-master", error)
    assert error["code"] == "not_master" and world.everything() == before
    # A payload older than the contract that carries `migrate files`.
    stale = world.tmp / "stale-registry"
    shutil.copytree(world.registry, stale, symlinks=True)
    old = stale / "memory" / "memory"
    old.unlink()
    old.write_text("#!/bin/sh\nexit 0\n")
    old.chmod(0o755)
    error = world.error(world.manager(world.roots["frozen"], "migrate", "--apply",
                                      CAPABILITIES_HOME=str(stale)))
    _say("refusal-payload-outdated", error)
    assert error["code"] == "payload_outdated" and error["detail"]["payloads"] == ["memory"]
    shutil.rmtree(stale)


# --- each agent: dry run, then apply ---------------------------------------------------

AGENTS = ("relay", "books", "clinic", "admin", "body", "kept")


@pytest.mark.parametrize("name", AGENTS)
def test_dry_run_reports_and_writes_nothing(world, name):
    root = world.roots[name]
    before = world.everything()
    dry = world.answer(world.manager(root, "migrate"))
    _say(f"dry-run-{name}", dry)
    assert world.everything() == before
    assert dry["applied"] is False and not dry["blockers"], dry["blockers"]
    plan = dry["plan"]
    questions = {q["id"] for q in dry["questions"]}
    if name == "relay":
        assert questions == {"nodes.srv-relay", "deployment.targets.local"}
        assert any("/Users/someone/dev/ops-agent" in w for w in plan["warnings"])
        assert {r["capability"] for r in plan["placement"]} >= {"telegram"}
        assert all(r["node"] == "srv-relay" for r in plan["placement"])
        assert {s["document"] for s in plan["settings"]} == {"telegram", "memory"}
        assert [i["kind"] for i in plan["movers"]["telegram"]["items"]] == ["profile", "setting"]
        modified = [p for p, state in plan["git"].items() if state == "modified"]
        assert modified == [str(root / "capabilities" / "telegram" / "service" / "settings.json")]
    elif name == "books":
        assert questions == {"automations.environments.production", "automations.automations.watch"}
        items = {(i["kind"], i["name"]): i["outcome"]
                 for i in plan["movers"]["automations"]["items"]}
        assert items == {("definition", "by-hand"): "would_write",
                         ("definition", "daily-books"): "question",
                         ("definition", "watch"): "question",
                         ("agent_profile", "books"): "would_write"}
        [settings] = plan["settings"]
        assert settings["excluded"] == [["automations"], ["engine", "timezone"],
                                        ["agents", "workers"]]
    elif name == "clinic":
        assert not questions
        assert plan["git"][str(root / "capabilities" / "whatsapp" / "connections.json")] \
            == "ignored"
        assert {i["name"] for i in plan["movers"]["whatsapp"]["items"]} == {
            "clinic-claude", "clinic-night"}
    elif name == "admin":
        assert not questions
        assert {(i["kind"], i["name"]) for i in plan["movers"]["tasks"]["items"]} == {
            ("worker", "reviewer"), ("worker", "builder"), ("profile", "review-claude")}
        assert {s["document"] for s in plan["settings"]} == {"tasks", "tasks.conveyor"}
        assert [(Path(v["root"]).name, v["differences"]) for v in plan["verify_only"]] == [
            ("admin-copy", [])]
    elif name == "body":
        assert not questions and dry["identity_write"]["id"] == IDS["body"]
        assert {s["document"] for s in plan["settings"]} == {"agentrooms", "slack.policy"}
        [settings] = [s for s in plan["settings"] if s["document"] == "agentrooms"]
        assert settings["excluded"] == [["agents"]]
        assert [(i["name"], i["outcome"]) for i in plan["movers"]["agentrooms"]["items"]] == [
            ("bot", "would_write")]
        assert {i["name"] for i in plan["movers"]["askproject"]["items"]} == {"peer-read"}
        assert dry["plan"]["files"][0].startswith(str(root / "agent" / "capabilities"))
    elif name == "kept":
        assert dry["mode"] == "store" and plan["records"]["kept"]
        assert any("stale.example.org" in line for line in plan["stale"])
        assert any("stale-app" in line for line in plan["stale"])
        assert len(plan["stale"]) == 3, plan["stale"]
    if questions:
        assert dry["would_write"] is False
        assert world.error(world.manager(root, "migrate", "--apply"))["code"] == "question_open"
        assert world.everything() == before


@pytest.mark.parametrize("name", AGENTS)
def test_apply_moves_everything_and_reads_the_same_through_the_switch(world, name):
    root = world.roots[name]
    names = SWITCH_NAMES[name]
    before_switch = world.through_switch(root, names)
    applied = world.answer(world.manager(root, "migrate", "--apply", "--answers",
                                         answers_file(world, name, ANSWERS[name])))
    _say(f"apply-{name}", applied)
    assert applied["state"] == "migrated" and applied["migrated_at"]
    assert not applied["verify"]["differences"]
    after_switch = world.through_switch(root, names)
    _say(f"switch-{name}", {"before": before_switch, "after": after_switch})
    assert after_switch.pop("adapter") == "AgentRecords"
    assert before_switch.pop("adapter") in ("FileRecords", "StoreRecords")
    if name == "books":
        # [[automations]] and engine.timezone moved into definitions, and the
        # agent profiles into harness profiles.
        document = before_switch["settings"]["automations"]["document"]
        document.pop("automations")
        document["engine"].pop("timezone")
        document["agents"].pop("workers")
    if name == "relay":
        # The old worker settings became a profile name.
        defaults = after_switch["settings"]["telegram"]["document"].pop("defaults")
        before_switch["settings"]["telegram"]["document"].pop("defaults")
        assert "worker" not in defaults and "workers" not in defaults and defaults["profile"]
        before_switch["settings"]["telegram"]["keys"].pop("defaults.profile")
        assert after_switch["settings"]["telegram"]["keys"].pop("defaults.profile") \
            == defaults["profile"]
    if name == "body":
        # The agents map became a worker, bound in the settings under a key
        # that holds a dot.
        assert after_switch["settings"]["agentrooms"]["document"] == {
            "version": 1, "binding.bot": {"worker": "bot"}}
        assert world.sql("SELECT path FROM capabilities_settings WHERE agent_id = %s AND "
                         "capability = 'agentrooms' ORDER BY path", (IDS[name],)) == [
            (["binding.bot", "worker"],), (["version"],)]
        assert world.sql("SELECT name FROM agentrooms_workers WHERE project_id = %s",
                         (IDS[name],)) == [("bot",)]
        before_switch["settings"]["agentrooms"]["document"].pop("agents")
        after_switch["settings"]["agentrooms"] = before_switch["settings"]["agentrooms"]
    if name == "kept":
        # Its records were the store's already; its settings were files.
        assert after_switch["identifiers"]["coolify"]["app"] == ["kept-app", ""]
        assert after_switch["identifiers"] == before_switch["identifiers"]
        assert after_switch["settings"] == before_switch["settings"]
        assert after_switch["settings"]["memory"]["document"] == {"window_days": 21}
        assert after_switch["gate"] == before_switch["gate"]
        assert after_switch["gate"]["memory"] == {"enabled": True}
    else:
        assert after_switch == before_switch
    identity = json.loads((Path(applied["identity"])).read_text())
    assert identity["records"] == "store" and identity["id"] == IDS[name]
    for path in applied["removed"]:
        assert not Path(path).exists()
    assert applied["removed"], "nothing was removed"
    for path in applied["archive"]["files"]:
        assert os.stat(path).st_mode & 0o777 == 0o600
    state = world.sql("SELECT migrating_since, migrated_at FROM capabilities_agents "
                      "WHERE id = %s", (IDS[name],))
    assert state[0][0] is None and state[0][1] is not None
    if name == "relay":
        nodes = dict(world.sql("SELECT node, address FROM capabilities_agent_nodes "
                               "WHERE agent_id = %s", (IDS[name],)))
        assert nodes == {MASTER: None, "srv-relay": "203.0.113.10"}
        placed = world.sql("SELECT capability FROM capabilities_policy WHERE agent_id = %s "
                           "AND node = 'srv-relay'", (IDS[name],))
        assert ("telegram",) in placed
        log = world.launchctl_log.read_text()
        assert "bootout gui/" in log and "bootstrap gui/" in log
        assert applied["services"]["launchd"] == ["test.relay.telegram"]
    if name == "books":
        profiles = world.sql("SELECT name, spec, created_by FROM harness_profiles WHERE "
                             "scope_id = %s AND active", (IDS[name],))
        assert profiles == [("automations-books", {
            "harness": "claude", "model": "sonnet", "timeout_seconds": 600.0, "permission_mode": "plan",
            "tools": ["Read", "Glob", "Grep"], "allowed_tools": ["Read", "Glob", "Grep"]},
            f"migrate@{MASTER}")]
        rows = world.sql("SELECT slug, node, schedule, timezone, created_by FROM "
                         "automations_definitions WHERE project_id = %s AND active "
                         "ORDER BY slug", (IDS[name],))
        assert rows == [("by-hand", MASTER, None, "Europe/Tallinn", f"migrate@{MASTER}"),
                        ("daily-books", "srv-books", "0 6 * * *", "Europe/Tallinn",
                         f"migrate@{MASTER}"),
                        ("watch", "srv-books", "*/5 * * * *", "Europe/Tallinn",
                         f"migrate@{MASTER}")]
    if name == "clinic":
        rows = world.sql("SELECT name FROM harness_profiles WHERE scope_id = %s AND active "
                         "ORDER BY name", (IDS[name],))
        assert rows == [("clinic-claude",), ("clinic-night",)]
        [archived] = applied["archive"]["files"]
        assert archived.endswith("capabilities/whatsapp/connections.json")
    if name == "relay":
        [archived] = applied["archive"]["files"]
        assert archived.endswith("capabilities/telegram/service/settings.json")
        assert "worker" in json.loads(Path(archived).read_text())["defaults"]
    if name == "admin":
        rows = world.sql("SELECT name FROM tasks_workers WHERE project_id = %s AND active "
                         "ORDER BY name", (IDS[name],))
        assert rows == [("builder",), ("reviewer",)]
        conveyor = world.sql("SELECT path, value FROM capabilities_settings WHERE "
                             "agent_id = %s AND capability = 'tasks.conveyor' ORDER BY path",
                             (IDS[name],))
        assert conveyor == [(["escalate_to"], ["reviewer", "Owner"]), (["stall_after"], "6h")]
        trail = world.sql("SELECT DISTINCT changed_by FROM capabilities_settings_changes "
                          "WHERE agent_id = %s", (IDS[name],))
        assert trail == [(f"migrate@{MASTER}",)]
    if name == "body":
        assert not (root / "agent" / "capabilities" / "slack" / "policy.json").exists()
        assert world.sql("SELECT value FROM capabilities_settings WHERE agent_id = %s AND "
                         "capability = 'slack.policy' AND path = '{allowed_users}'",
                         (IDS[name],)) == [(["U01"],)]
    # A second apply changes nothing.
    before = world.everything()
    again = world.answer(world.manager(root, "migrate", "--apply", "--answers",
                                       answers_file(world, name, ANSWERS[name])))
    assert again["state"] == "migrated" and again["changed"] is False
    assert world.everything() == before


def test_an_id_project_json_and_contextkit_disagree_on_is_a_question(world):
    root = world.roots["split"]
    before = world.everything()
    dry = world.answer(world.manager(root, "migrate"))
    _say("dry-run-split", dry)
    assert world.everything() == before
    [question] = dry["questions"]
    assert question["id"] == "id" and question["choices"] == [IDS["split"], IDS["split-bound"]]
    answers = answers_file(world, "split", {"id": IDS["split-bound"]})
    applied = world.answer(world.manager(root, "migrate", "--apply", "--answers", answers))
    assert applied["agent_id"] == IDS["split-bound"] and applied["state"] == "migrated"
    assert json.loads((root / "capabilities" / "project.json").read_text())["id"] \
        == IDS["split-bound"]


# --- interrupted ---------------------------------------------------------------------------

KILLER = """#!/bin/sh
# def _cmd_migrate_files(  - the contract this stands in for carries it
if [ "$1 $2 $3" = "migrate files verify" ] && [ -f "{flag}" ]; then
  rm -f "{flag}"
  kill -9 $PPID
  sleep 5
  exit 1
fi
exec "{real}" "$@"
"""


def test_a_run_killed_between_write_and_finalize_resumes(world):
    root = world.roots["frozen"]
    registry = world.tmp / "killer-registry"
    shutil.copytree(world.registry, registry, symlinks=True)
    flag = world.tmp / "kill.flag"
    killer = world.tmp / "killer.sh"
    killer.write_text(KILLER.format(flag=flag, real=REPO / "capabilities" / "tasks" / "bin" /
                                    "tasks"))
    world.install(registry, "tasks", script=killer)
    flag.write_text("1")
    killed = world.manager(root, "migrate", "--apply", CAPABILITIES_HOME=str(registry))
    assert killed.returncode != 0
    [(since, moved)] = world.sql("SELECT migrating_since, migrated_at FROM capabilities_agents "
                                 "WHERE id = %s", (IDS["frozen"],))
    assert since is not None and moved is None
    journal = json.loads((world.state / "capabilities" / "migrate" / IDS["frozen"] /
                          "journal.json").read_text())
    assert journal["phase"] == "written"
    # Frozen: nothing else writes the agent's records, in files or rows.
    frozen = {
        "ids_set": world.error(world.cap(root, "coolify", "ids", "set", "app", "other")),
        "set": world.error(world.manager(root, "set", "coolify", "connection", "x",
                                         '{"base_url": "https://x.example.org"}')),
        "settings": world.error(world.cap(root, "memory", "settings", "set",
                                          "window_days", "9")),
    }
    _say("interrupted-frozen", {"journal": journal, "refusals": frozen})
    assert {e["code"] for e in frozen.values()} == {"agent_migrating"}
    assert (root / "capabilities" / "coolify" / "identifiers.json").exists()
    resumed = world.answer(world.manager(root, "migrate", "--apply",
                                         CAPABILITIES_HOME=str(registry)))
    _say("interrupted-resumed", resumed)
    assert resumed["state"] == "migrated" and not resumed["verify"]["differences"]
    assert world.sql("SELECT value FROM capabilities_identifiers WHERE agent_id = %s",
                     (IDS["frozen"],)) == [("frozen-app",)]
    before = world.everything()
    again = world.answer(world.manager(root, "migrate", "--apply",
                                       CAPABILITIES_HOME=str(registry)))
    assert again["changed"] is False and world.everything() == before
    shutil.rmtree(registry)


def test_finalize_waits_for_every_target_node(world):
    root = world.roots["later"]
    answers = answers_file(world, "later", {"nodes": {"srv-later": {"register": "later"}}})
    error = world.error(world.manager(root, "migrate", "--apply", "--answers", answers))
    _say("refusal-target-node-unregistered", error)
    assert error["code"] == "target_node_unregistered" and error["detail"]["nodes"] == ["srv-later"]
    [(since, moved)] = world.sql("SELECT migrating_since, migrated_at FROM capabilities_agents "
                                 "WHERE id = %s", (IDS["later"],))
    assert since is not None and moved is None
    assert (root / "capabilities" / "settings.json").exists()
    assert "records" not in json.loads((root / "capabilities" / "project.json").read_text())
    before = world.everything()
    again = world.error(world.manager(root, "migrate", "--apply", "--answers", answers))
    after = world.everything()
    assert again["code"] == "target_node_unregistered"
    assert {k: v for k, v in after.items()} == before, [
        (k, sorted(set(after[k].items()) ^ set(before[k].items())))
        for k in ("tables", "state") if after[k] != before[k]]
    world.answer(world.manager(root, "node", "add", "srv-later", "--kind", "server",
                               "--target", "production"))
    done = world.answer(world.manager(root, "migrate", "--apply", "--answers", answers))
    assert done["state"] == "migrated"
    assert world.sql("SELECT capability FROM capabilities_policy WHERE agent_id = %s AND "
                     "node = 'srv-later'", (IDS["later"],)) == [("memory",)]


# --- report and discard -----------------------------------------------------------------

def test_report_and_discard(world):
    root = world.roots["other"]
    report = world.answer(world.manager(root, "migrate", "--report"))
    _say("report", report)
    agents = {Path(a["root"]).name: a for a in report["agents"]}
    assert agents["other"]["state"] == "registered" and agents["other"]["files"]
    assert agents["relay"]["state"] == "migrated" and not agents["relay"]["files"]
    assert report["config_home"]["files"] and report["config_home"]["global_migrated_at"]
    before = world.everything()
    error = world.error(world.manager(root, "migrate", "--discard"))
    _say("discard-refused", error)
    assert error["code"] == "agents_unmigrated" and world.everything() == before
    # Once `other` is gone from this machine, the files go without being read.
    shutil.rmtree(root)
    del world.roots["other"]
    world.agentkit_list()
    discarded = world.answer(world.manager(world.roots["relay"], "migrate", "--discard"))
    _say("discard", discarded)
    assert sorted(Path(p).relative_to(world.config).as_posix() for p in discarded["removed"]) \
        == ["capabilities/settings.json", "coolify/connections.json",
            "coolify/identifiers.json", "memory/service/settings.json"]

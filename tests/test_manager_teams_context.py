"""The `teams` block in the composed context, read from the store at composition.

While `teams` is enabled the manager adds, after its constant contribution,
the agent's own teams as the store holds them: each team it is a member of by
its line, and for a team in always mode its enabled always documents whole
under the team's name and its enabled on-request documents by their line.
The block carries nothing that changes between sessions, and an agent with
teams disabled gets none of it. The module starts a throwaway PostgreSQL when
`initdb` and `pg_ctl` are on PATH and psycopg is importable, and skips
otherwise.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
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
TEAMS = REPO / "capabilities" / "teams" / "bin" / "teams"
ADMIN = "pgadmin"
SCHEMA = "tmc"
AGENT = "prj_" + "0a1b2c3d4e5f"
OTHER = "prj_" + "1b2c3d4e5f60"
STYLE = "---\ndescription: How we write.\nload: always\n---\n# Style\n\nOne line per paragraph.\n"
DEPLOY = "---\ndescription: Read before a release.\nload: on-request\n---\nRelease from main.\n"
CLEARED = ("CAPABILITIES_", "CLAUDE", "AGENTKIT_DB_")


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_teams_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-teams-context-pg-"))
    port = _free_port()
    env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(*argv: str) -> None:
        done = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=120)
        assert done.returncode == 0, (argv, done.stdout + done.stderr)
    try:
        run("initdb", "-D", str(root / "data"), "-U", ADMIN, "-E", "UTF8",
            "--locale=en_US.UTF-8", "--auth=trust")
        with (root / "data" / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                       "unix_socket_directories = ''\n")
        run("pg_ctl", "-D", str(root / "data"), "-l", str(root / "pg.log"), "-w", "-t", "60",
            "start")
        yield port
    finally:
        subprocess.run(["pg_ctl", "-D", str(root / "data"), "-m", "immediate", "stop"],
                       env=env, capture_output=True, timeout=60)
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp: Path, port: int):
        import psycopg
        self.tmp = tmp
        database = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(host="127.0.0.1", port=port, user=ADMIN, dbname="postgres",
                             autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {database} ENCODING 'UTF8' TEMPLATE template0")
        self.url = f"postgresql://{ADMIN}@127.0.0.1:{port}/{database}?sslmode=disable"
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "capabilities").mkdir()
        (self.project / ".env.local").write_text(f"CAPABILITIES_PROJECT_ID={AGENT}\n")
        self.enable(True)
        self._snapshot()

    def enable(self, enabled: bool) -> None:
        (self.project / "capabilities" / "settings.json").write_text(json.dumps(
            {"capabilities": {"teams": {"enabled": enabled}}}))

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEARED)}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CAPABILITIES_BIN": str(self.tmp / "bin"),
            "CAPABILITIES_MANAGER_BIN": str(MANAGER),
            "AGENTKIT_DB_URL": self.url,
            "AGENTKIT_DB_SCHEMA": SCHEMA,
            "AGENT_NODE_ID": "746573742d6e",
            "USER": "tester",
        })
        return env

    def register(self, *agents: str) -> None:
        """The store holds a row only for an agent it has registered."""
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            for agent in agents:
                conn.execute("INSERT INTO agents (id, kind) VALUES (%s, 'agent') "
                             "ON CONFLICT (id) DO NOTHING", (agent,))

    def _snapshot(self) -> None:
        """teams as install records it: its stub and its own manifest."""
        manifest = json.loads(self.teams("manifest", "--json"))
        entry = self.tmp / "registry" / "teams"
        entry.mkdir(parents=True)
        (entry / "teams").write_text("#!/bin/sh\n")
        (entry / "stub").write_text(f"{manifest['summary']} Run `teams help`.\n")
        (entry / "manifest.json").write_text(json.dumps(manifest))

    def teams(self, *args: str, stdin: str | None = None) -> str:
        done = subprocess.run([sys.executable, str(TEAMS), *args], cwd=self.project,
                              env=self.env(), input=stdin, text=True, capture_output=True,
                              timeout=180)
        assert done.returncode == 0, done.stderr
        return done.stdout

    def fragment(self) -> str:
        done = subprocess.run([sys.executable, str(MANAGER), "context", "--fragment"],
                              cwd=self.project, env=self.env(), text=True,
                              capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stderr
        return done.stdout


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def _section(fragment: str, name: str = "teams") -> str | None:
    parts = re.split(r"(?m)^## (\S+)\n", fragment.split(M.CONTEXT_END, 1)[0])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}.get(name)


def _seed(lab: Lab) -> None:
    lab.teams("create", "crew", "--description", "The release crew: turn to it for releases.")
    lab.teams("create", "ops", "--description", "Operations: turn to it for the servers.")
    lab.teams("create", "far", "--description", "A team this agent is not in.", "--always")
    lab.register(AGENT, OTHER)
    for team in ("crew", "ops"):
        lab.teams("members", "add", team, AGENT)
    lab.teams("members", "add", "far", OTHER)
    for team in ("crew", "ops", "far"):
        lab.teams("doc", "save", team, "style", "--from", "-", stdin=STYLE)
        lab.teams("doc", "save", team, "deploy", "--from", "-", stdin=DEPLOY)


def test_teams_is_core():
    assert "teams" in M.CORE_CAPABILITIES


def test_an_ordinary_team_is_one_line_and_its_documents_stay_on_request(lab):
    _seed(lab)
    section = _section(lab.fragment())
    assert "Teams carry instructions shared by a group of agents." in section
    assert ("- **crew** - The release crew: turn to it for releases. "
            "(its instructions: `teams show crew`)") in section
    assert "- **ops** - Operations: turn to it for the servers." in section
    assert "far" not in section
    assert "One line per paragraph." not in section
    assert "### Team" not in section


def test_an_always_team_composes_its_enabled_always_documents_under_its_name(lab):
    _seed(lab)
    lab.teams("mode", "ops", "always")
    lab.teams("doc", "save", "ops", "secrets-off", "--from", "-",
              stdin="---\ndescription: Switched off.\nload: always\n---\nNever shown.\n")
    lab.teams("doc", "off", "ops", "secrets-off")
    section = _section(lab.fragment())
    assert "- **ops** - Operations: turn to it for the servers. (always in context, below)" \
        in section
    assert "### Team ops: style\n\n# Style\n\nOne line per paragraph.\n" in section
    assert ("### Team ops: on request\n\n"
            "- `teams doc show ops deploy` - Read before a release.\n") in section
    assert "Never shown." not in section and "secrets-off" not in section
    assert "### Team crew" not in section


def test_the_block_is_the_same_in_every_session_and_follows_the_store(lab):
    _seed(lab)
    lab.teams("mode", "crew", "always")
    first = lab.fragment()
    assert lab.fragment() == first
    assert not re.search(r"\b20\d\d-\d\d-\d\d|Rendered|tokens", _section(first))
    lab.teams("doc", "save", "crew", "style", "--from", "-",
              stdin=STYLE.replace("One line", "Each line"))
    assert "Each line per paragraph." in _section(lab.fragment())


def test_an_agent_in_no_team_carries_only_the_constant_block(lab):
    lab.teams("create", "crew", "--description", "The release crew.")
    section = _section(lab.fragment())
    assert section.strip().startswith("Teams of agents")
    assert "is a member of these teams" not in section


def test_teams_disabled_for_the_agent_composes_nothing_of_it(lab):
    _seed(lab)
    lab.enable(False)
    fragment = lab.fragment()
    assert _section(fragment) is None
    assert "The release crew" not in fragment

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CAPABILITIES_BIN": str(tmp_path / "bin"),
    })
    env.pop("CAPABILITIES_WORKSPACE", None)
    return env


def _run(env: dict[str, str], *args: str, check: bool = True):
    result = subprocess.run(
        [str(MANAGER), *args], cwd=REPO, env=env, text=True,
        capture_output=True, timeout=120,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"capabilities {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def test_authoring_workspace_is_canonical_and_install_is_strict(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = tmp_path / "home" / "capabilities-sources" / "personal"
    assert initialized["path"] == str(workspace)
    assert (workspace / ".git").is_dir()
    assert (workspace / "AUTHORING.md").is_file()
    assert (workspace / "AGENTS.md").is_file()
    assert (workspace / "contract" / "preamble.py").read_bytes() == \
        (REPO / "contract" / "preamble.py").read_bytes()

    created = json.loads(_run(
        env, "new", "demo", "--source", "personal").stdout)
    script = Path(created["executable"])
    source_text = script.read_text()
    assert "# >>> contract: capability core" in source_text
    assert "# >>> contract: connections" in source_text
    assert "PROTOCOL" not in source_text
    core_created = json.loads(_run(
        env, "new", "localtool", "--source", "personal", "--core-only").stdout)
    core_text = Path(core_created["executable"]).read_text()
    assert "# >>> contract: capability core" in core_text
    assert "# >>> contract: connections" not in core_text
    for path in (script, Path(core_created["executable"])):
        finalized = path.read_text().replace(
            "TODO: describe the capability's smallest useful surface.",
            "Test capability with a completed managed manifest.",
        ).replace(
            "Replace this scaffold check with",
            "Test readiness uses",
        )
        path.write_text(finalized)
    source_text = script.read_text()

    _run(env, "source", "index", "personal")
    checked = json.loads(_run(env, "source", "check", "personal").stdout)
    assert checked["ok"] is True
    installed = json.loads(_run(
        env, "install", "demo", "--source", "personal").stdout)
    installed_script = Path(installed["registry"]) / "demo"
    before = installed_script.read_bytes()
    manifest = json.loads(subprocess.run(
        [str(installed_script), "manifest", "--json"], env=env, text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout)
    assert "protocol" not in manifest

    script.write_text(source_text.replace(
        "# --- Error reporting", "# hand-edited generated region"))
    rejected = _run(env, "install", "demo", "--from", str(script), check=False)
    assert rejected.returncode == 7
    assert json.loads(rejected.stderr.splitlines()[-1])["error"]["code"] == "audit_failed"
    assert installed_script.read_bytes() == before
    synced = json.loads(_run(env, "source", "sync", "personal").stdout)
    assert synced["stamped"] == [{
        "capability": "demo", "regions": ["capability core"]}]
    _run(env, "source", "index", "personal")
    assert json.loads(_run(
        env, "source", "check", "personal").stdout)["ok"] is True


def test_remote_source_catalog_search_and_install(tmp_path):
    author_env = _env(tmp_path / "author")
    _run(author_env, "source", "init", "personal")
    created = json.loads(_run(
        author_env, "new", "demo", "--source", "personal").stdout)
    script = Path(created["executable"])
    script.write_text(script.read_text().replace(
        "TODO: describe the capability's smallest useful surface.",
        "Test capability with a completed managed manifest.",
    ).replace(
        "Replace this scaffold check with",
        "Test readiness uses",
    ))
    _run(author_env, "source", "index", "personal")
    workspace = tmp_path / "author" / "home" / "capabilities-sources" / "personal"
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Add demo"], cwd=workspace, check=True,
        capture_output=True, text=True,
    )

    consumer_env = _env(tmp_path / "consumer")
    _run(consumer_env, "source", "add", "shared", str(workspace))
    search = json.loads(_run(
        consumer_env, "search", "demo", "--source", "shared").stdout)
    assert search["matches"] == [{
        "name": "demo",
        "source": "shared",
        "summary": "Test capability with a completed managed manifest.",
        "installed": False,
    }]
    installed = json.loads(_run(
        consumer_env, "install", "demo", "--source", "shared").stdout)
    meta = json.loads((Path(installed["registry"]) / "meta.json").read_text())
    assert meta["source_id"] == "shared"
    assert meta["source_commit"]
    assert meta["source_dirty"] is False

    clone_env = _env(tmp_path / "clone")
    cloned = json.loads(_run(
        clone_env, "source", "clone", "personal", str(workspace)).stdout)
    assert cloned["path"] == str(
        tmp_path / "clone" / "home" / "capabilities-sources" / "personal")
    assert json.loads(_run(
        clone_env, "source", "check", "personal").stdout)["ok"] is True

from __future__ import annotations

import json
import os
import shutil
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
    cwd = REPO
    config_path = Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "sources.json"
    config = json.loads(config_path.read_text()) if config_path.is_file() else {"sources": {}}
    source_id = None
    if args and args[0] == "new" and "--source" in args:
        source_id = args[args.index("--source") + 1]
    elif len(args) >= 3 and args[:2] in (
            ("source", "index"), ("source", "check"),
            ("source", "verify"), ("source", "sync")):
        source_id = args[2]
    if source_id:
        entry = (config.get("sources") or {}).get(source_id) or {}
        if entry.get("path"):
            cwd = Path(entry["path"])
    elif len(args) >= 3 and args[:2] == ("source", "path"):
        cwd = Path(env["CAPABILITIES_HOME"]).parent
    result = subprocess.run(
        [str(MANAGER), *args], cwd=cwd, env=env, text=True,
        capture_output=True, timeout=120,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"capabilities {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _git(repo: Path, *args: str, check: bool = True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
        timeout=60, check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _commit(repo: Path, message: str) -> str:
    _git(
        repo,
        "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_manager_and_generated_scaffold_each_have_one_pep723_block(tmp_path):
    assert MANAGER.read_text().splitlines().count("# /// script") == 1

    env = _env(tmp_path)
    _run(env, "source", "init", "personal")
    created = json.loads(_run(
        env, "new", "demo", "--source", "personal").stdout)
    generated = Path(created["executable"]).read_text()
    assert generated.splitlines().count("# /// script") == 1


def test_source_catalog_ignores_runtime_cache_directories(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = Path(initialized["path"])
    created = json.loads(_run(
        env, "new", "demo", "--source", "personal").stdout)
    capdir = Path(created["path"])
    _run(env, "source", "index", "personal")
    catalog_path = workspace / ".capability-source" / "catalog.json"
    before = json.loads(catalog_path.read_text())["capabilities"]["demo"] \
        ["payload_sha256"]

    cache = capdir / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text('["demo"]\n')
    bytecode = capdir / "bin" / "__pycache__"
    bytecode.mkdir()
    (bytecode / "demo.pyc").write_bytes(b"runtime cache")
    _run(env, "source", "index", "personal")

    after = json.loads(catalog_path.read_text())["capabilities"]["demo"] \
        ["payload_sha256"]
    assert after == before


def test_custom_authoring_refuses_a_different_marked_checkout(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = Path(initialized["path"])
    refused = subprocess.run(
        [str(MANAGER), "new", "demo", "--source", "personal"],
        cwd=REPO, env=env, text=True, capture_output=True, timeout=120,
        check=False,
    )
    assert refused.returncode == 6
    assert json.loads(refused.stderr)["error"]["code"] == "source_checkout_mismatch"
    created = json.loads(subprocess.run(
        [str(MANAGER), "new", "demo", "--source", "personal"],
        cwd=workspace, env=env, text=True, capture_output=True, timeout=120,
        check=True,
    ).stdout)
    assert Path(created["path"]).parent == workspace / "capabilities"


def test_authoring_workspace_is_canonical_and_install_is_strict(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = tmp_path / "registry" / "sources" / "personal"
    assert initialized["path"] == str(workspace)
    assert (workspace / ".git").is_dir()
    assert (workspace / "AUTHORING.md").is_file()
    assert (workspace / "AGENTS.md").is_file()
    assert (workspace / "contract" / "preamble.py").read_bytes() == \
        (REPO / "contract" / "preamble.py").read_bytes()
    listed = json.loads(_run(env, "source", "list").stdout)
    assert listed["workspace_root"] == str(tmp_path / "registry" / "sources")

    reserved = _run(
        env, "new", "sources", "--source", "personal", check=False)
    assert reserved.returncode == 6
    assert json.loads(reserved.stderr)["error"]["code"] == \
        "reserved_capability_name"
    refused_uninstall = _run(env, "uninstall", "sources", check=False)
    assert refused_uninstall.returncode == 6
    assert workspace.is_dir()

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
    assert json.loads(rejected.stderr.splitlines()[-1])["error"]["code"] == \
        "install_validation_failed"
    assert installed_script.read_bytes() == before
    synced = json.loads(_run(env, "source", "sync", "personal").stdout)
    assert synced["stamped"] == [{
        "capability": "demo", "regions": ["capability core"]}]
    _run(env, "source", "index", "personal")
    assert json.loads(_run(
        env, "source", "check", "personal").stdout)["ok"] is True


def test_source_sync_inserts_a_new_required_contract_region(tmp_path):
    env = _env(tmp_path)
    _run(env, "source", "init", "personal")
    created = json.loads(_run(
        env, "new", "legacy", "--source", "personal", "--core-only").stdout)
    script = Path(created["executable"])
    text = script.read_text()
    start = text.index("# >>> contract: store")
    close = "# <<< contract: store <<<"
    end = text.index(close, start) + len(close)
    while end < len(text) and text[end] == "\n":
        end += 1
    script.write_text(
        text[:start] + text[end:] +
        "\n\ndef _check(a, b, c, d, e):\n"
        "    return (a, b, c, d, e)\n")

    synced = json.loads(_run(env, "source", "sync", "personal").stdout)

    assert {"capability": "legacy", "regions": ["store"]} in synced["stamped"]
    repaired = script.read_text()
    assert repaired.index("# >>> contract: capability core") < \
        repaired.index("# >>> contract: store")
    assert "def _store_check(" in repaired
    assert "def _check(a, b, c, d, e):" in repaired
    _run(env, "source", "index", "personal")
    checked = json.loads(_run(env, "source", "check", "personal").stdout)
    assert checked["ok"] is True


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
    workspace = tmp_path / "author" / "registry" / "sources" / "personal"
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
        tmp_path / "clone" / "registry" / "sources" / "personal")
    assert json.loads(_run(
        clone_env, "source", "check", "personal").stdout)["ok"] is True


def test_legacy_workspace_is_migrated_into_capabilities_home(tmp_path):
    env = _env(tmp_path)
    _run(env, "source", "init", "personal")
    canonical = tmp_path / "registry" / "sources" / "personal"
    legacy = tmp_path / "home" / "capabilities-sources" / "personal"
    legacy.parent.mkdir(parents=True)
    canonical.rename(legacy)

    config_path = tmp_path / "config" / "capabilities" / "sources.json"
    config = json.loads(config_path.read_text())
    config["sources"]["personal"]["path"] = str(legacy)
    config_path.write_text(json.dumps(config))

    resolved = json.loads(_run(env, "source", "path", "personal").stdout)
    assert resolved["path"] == str(canonical)
    assert canonical.is_dir()
    assert not (tmp_path / "home" / "capabilities-sources").exists()
    migrated = json.loads(config_path.read_text())
    assert migrated["sources"]["personal"]["path"] == str(canonical)


def test_standalone_manager_has_complete_authoring_kit(tmp_path):
    env = _env(tmp_path)
    manager_dir = tmp_path / "registry" / ".manager"
    manager_dir.mkdir(parents=True)
    standalone = manager_dir / "capabilities"
    shutil.copy2(MANAGER, standalone)
    for relative in (
        "SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md", "ROUTINES.md",
        "contract/preamble.py", "contract/store.py",
        "guides/authoring.md", "guides/conforming.md",
        "guides/contract.md", "guides/dev.md", "guides/grooming.md",
        "guides/publishing.md", "guides/repositories.md", "guides/sanitizing.md",
    ):
        target = manager_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)

    result = subprocess.run(
        [str(standalone), "source", "init", "friend"], cwd=tmp_path,
        env=env, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    initialized = json.loads(result.stdout)
    assert initialized["path"] == str(
        tmp_path / "registry" / "sources" / "friend")
    assert Path(initialized["path"], "AUTHORING.md").is_file()
    assert Path(initialized["path"], "contract", "preamble.py").is_file()


def test_standalone_manager_selfcheck_reports_missing_source_without_traceback(tmp_path):
    env = _env(tmp_path)
    manager_dir = tmp_path / "registry" / ".manager"
    manager_dir.mkdir(parents=True)
    standalone = manager_dir / "capabilities"
    shutil.copy2(MANAGER, standalone)
    for relative in (
        "SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md", "ROUTINES.md",
        "contract/preamble.py", "contract/store.py",
        "guides/authoring.md", "guides/conforming.md",
        "guides/contract.md", "guides/dev.md", "guides/grooming.md",
        "guides/publishing.md", "guides/repositories.md", "guides/sanitizing.md",
    ):
        target = manager_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)

    result = subprocess.run(
        [str(standalone), "selfcheck"], cwd=tmp_path, env=env, text=True,
        capture_output=True, timeout=120,
    )
    assert result.returncode == 7
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert any(finding.startswith("source:") for finding in report["findings"])


def test_staged_index_is_commit_exact_and_verify_rejects_worktree_hashes(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = Path(initialized["path"])
    created = json.loads(_run(
        env, "new", "demo", "--source", "personal").stdout)
    script = Path(created["executable"])
    script.write_text(script.read_text().replace(
        "TODO: describe the capability's smallest useful surface.",
        "Test capability with a completed managed manifest.",
    ).replace(
        "Replace this scaffold check with",
        "Test readiness uses",
    ))
    _git(workspace, "add", ".")
    indexed = json.loads(_run(
        env, "source", "index", "personal", "--staged").stdout)
    assert indexed["basis"] == "staged"
    assert indexed["staged"] is True
    initial_commit = _commit(workspace, "Initial valid source")
    assert json.loads(_run(
        env, "source", "verify", "personal", "--ref", "HEAD").stdout)["ok"] is True

    audit_marker = tmp_path / "source-index-ran-audit"
    marker_hook = '''if os.environ.get("SOURCE_INDEX_AUDIT_MARKER") and sys.argv[1:] == ["refs"]:
    Path(os.environ["SOURCE_INDEX_AUDIT_MARKER"]).write_text("audit executed\\n")


'''
    staged_text = script.read_text().replace(
        "Test readiness uses", "Staged readiness uses").replace(
        'if __name__ == "__main__":',
        marker_hook + 'if __name__ == "__main__":',
    )
    script.write_text(staged_text)
    _git(workspace, "add", str(script.relative_to(workspace)))
    script.write_text(staged_text + "\n# unstaged working-tree noise\n")
    index_env = dict(env)
    index_env["SOURCE_INDEX_AUDIT_MARKER"] = str(audit_marker)
    _run(index_env, "source", "index", "personal", "--staged")
    assert not audit_marker.exists()
    committed = _commit(workspace, "Index the staged tree")
    verified = json.loads(_run(
        env,
        "source", "verify", "personal",
        "--ref", committed,
        "--base", initial_commit,
    ).stdout)
    assert verified["ok"] is True
    assert verified["manager_checked"] is False
    assert verified["audited_capabilities"] == ["demo"]
    committed_script = _git(
        workspace, "show", f"{committed}:capabilities/demo/bin/demo"
    ).stdout
    assert "unstaged working-tree noise" not in committed_script

    _git(workspace, "restore", str(script.relative_to(workspace)))
    script.write_text(script.read_text() + "\n# payload change left uncommitted\n")
    _run(env, "source", "index", "personal")
    _git(workspace, "add", ".capability-source/catalog.json")
    bad_commit = _commit(workspace, "Commit only a worktree-derived catalog")
    rejected = _run(
        env, "source", "verify", "personal", "--ref", bad_commit, check=False)
    assert rejected.returncode == 7
    failure = json.loads(rejected.stdout)
    assert failure["ok"] is False
    assert "catalog" in failure["failures"]


def test_staged_index_uses_editorial_profile_for_docs_only_change(tmp_path):
    env = _env(tmp_path)
    initialized = json.loads(_run(env, "source", "init", "personal").stdout)
    workspace = Path(initialized["path"])
    created = json.loads(_run(
        env, "new", "demo", "--source", "personal").stdout)
    script = Path(created["executable"])
    source = script.read_text().replace(
        "TODO: describe the capability's smallest useful surface.",
        "Test capability with a completed managed manifest.",
    ).replace(
        "Replace this scaffold check with",
        "Test readiness uses",
    )
    marker_hook = '''if os.environ.get("UNCHANGED_MANIFEST_MARKER") and sys.argv[1:3] == ["manifest", "--json"]:
    Path(os.environ["UNCHANGED_MANIFEST_MARKER"]).write_text("manifest executed\\n")


'''
    script.write_text(source.replace(
        'if __name__ == "__main__":',
        marker_hook + 'if __name__ == "__main__":',
    ))
    _git(workspace, "add", ".")
    initial = json.loads(_run(
        env, "source", "index", "personal", "--staged").stdout)
    assert initial["verification_profile"] == "manager"
    initial_commit = _commit(workspace, "Initial valid source")

    authoring = workspace / "AUTHORING.md"
    authoring.write_text(authoring.read_text() + "\nEditorial clarification.\n")
    catalog_path = workspace / ".capability-source" / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    original_summary = catalog["capabilities"]["demo"]["summary"]
    catalog["capabilities"]["demo"]["summary"] = "manually staged summary"
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
    _git(workspace, "add", "AUTHORING.md", ".capability-source/catalog.json")
    marker = tmp_path / "manifest-ran"
    editorial_env = dict(env)
    editorial_env["UNCHANGED_MANIFEST_MARKER"] = str(marker)
    indexed = json.loads(_run(
        editorial_env, "source", "index", "personal", "--staged").stdout)

    assert indexed["verification_profile"] == "editorial"
    assert not marker.exists()
    repaired = json.loads(catalog_path.read_text())
    assert repaired["capabilities"]["demo"]["summary"] == original_summary
    candidate = _commit(workspace, "Editorial source change")
    verified = json.loads(_run(
        editorial_env,
        "source", "verify", "personal",
        "--ref", candidate,
        "--base", initial_commit,
    ).stdout)
    assert verified["ok"] is True
    assert verified["manager_checked"] is False
    assert verified["audited_capabilities"] == []
    assert not marker.exists()

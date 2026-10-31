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
    assert not (workspace / "contract").exists()
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
    assert '_contract_bind(globals(), "core")' in source_text
    assert '_contract_bind(globals(), "connections")' in source_text
    assert '"capabilities-contract>=0.8,<1"' in source_text
    assert "PROTOCOL" not in source_text
    core_created = json.loads(_run(
        env, "new", "localtool", "--source", "personal", "--core-only").stdout)
    core_text = Path(core_created["executable"]).read_text()
    assert '_contract_bind(globals(), "core")' in core_text
    assert '_contract_bind(globals(), "connections")' not in core_text
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
        '"capabilities-contract>=0.8,<1"', '"capabilities-contract==0.7.2"'))
    rejected = _run(env, "install", "demo", "--from", str(script), check=False)
    assert rejected.returncode == 7
    refusal = json.loads(rejected.stderr.splitlines()[-1])["error"]
    assert refusal["code"] == "install_validation_failed"
    assert "older than 0.8" in refusal["hint"]
    assert installed_script.read_bytes() == before
    script.write_text(source_text.replace(
        '"capabilities-contract>=0.8,<1"', '"capabilities-contract>=0.8,<0.9"'))
    narrower = json.loads(_run(
        env, "install", "demo", "--from", str(script)).stdout)
    assert subprocess.run(
        [str(Path(narrower["registry"]) / "demo"), "stub"], env=env, text=True,
        capture_output=True, check=True, timeout=60,
    ).stdout.strip().endswith("`demo help`.")
    script.write_text(source_text)
    synced = json.loads(_run(env, "source", "sync", "personal").stdout)
    assert synced["ok"] is True
    _run(env, "source", "index", "personal")
    assert json.loads(_run(
        env, "source", "check", "personal").stdout)["ok"] is True


def test_a_script_that_does_not_bind_the_store_tier_is_refused(tmp_path):
    env = _env(tmp_path)
    _run(env, "source", "init", "personal")
    created = json.loads(_run(
        env, "new", "legacy", "--source", "personal", "--core-only").stdout)
    script = Path(created["executable"])
    script.write_text(script.read_text().replace(
        '_contract_bind(globals(), "store")\n', ""))

    refused = _run(env, "install", "legacy", "--from", str(script), check=False)

    assert refused.returncode == 7
    refusal = json.loads(refused.stderr.splitlines()[-1])["error"]
    assert refusal["code"] == "install_validation_failed"
    assert "does not bind the store tier" in refusal["hint"]


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
        "icon": False,
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


def test_search_fetches_the_official_source_a_fresh_machine_has_not_cached(tmp_path):
    author_env = _env(tmp_path / "author")
    _run(author_env, "source", "init", "personal")
    created = json.loads(_run(
        author_env, "new", "demo", "--source", "personal").stdout)
    script = Path(created["executable"])
    script.write_text(script.read_text().replace(
        "TODO: describe the capability's smallest useful surface.",
        "Test capability with a completed managed manifest.",
    ).replace("Replace this scaffold check with", "Test readiness uses"))
    _run(author_env, "source", "index", "personal")
    workspace = tmp_path / "author" / "registry" / "sources" / "personal"
    _git(workspace, "add", ".")
    _commit(workspace, "Add demo")

    # An installed manager sits outside any checkout, so the official source is
    # its cache clone; git is pointed from the official URL to the local source.
    machine = tmp_path / "machine"
    manager = machine / "registry" / ".manager" / "capabilities"
    manager.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, manager)
    env = _env(machine)
    official = "https://github.com/ai-cluster-one/capabilities.git"

    def search(remote: Path):
        return subprocess.run(
            [str(manager), "search", "--json"], cwd=machine,
            env={**env, "GIT_CONFIG_COUNT": "1",
                 "GIT_CONFIG_KEY_0": f"url.{remote}.insteadOf",
                 "GIT_CONFIG_VALUE_0": official},
            text=True, capture_output=True, timeout=120)

    # A cache it cannot write to and a source it cannot reach both leave
    # official unavailable, as before the fetch.
    old_answer = {"matches": [], "unavailable_sources": [
        {"source": "official", "hint": "capabilities source refresh official"}]}
    cache = machine / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o555)
    try:
        unwritable = search(workspace)
    finally:
        cache.chmod(0o755)
    assert unwritable.returncode == 0, unwritable.stderr
    assert json.loads(unwritable.stdout) == old_answer

    unreachable = search(tmp_path / "nowhere")
    assert unreachable.returncode == 0, unreachable.stderr
    assert json.loads(unreachable.stdout) == old_answer

    fetched = search(workspace)
    assert fetched.returncode == 0, fetched.stderr
    answer = json.loads(fetched.stdout)
    assert answer["unavailable_sources"] == []
    assert [(row["name"], row["source"]) for row in answer["matches"]] == [
        ("demo", "official")]
    assert (machine / "cache" / "capabilities" / "sources" / "official"
            / ".capability-source" / "catalog.json").is_file()


def test_a_bundle_icon_is_checked_catalogued_and_read_through_the_manager(tmp_path):
    import struct
    import zlib

    def png(width: int, height: int) -> bytes:
        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data)))
        raw = b"".join(b"\0" + b"\0\0\0\0" * width for _ in range(height))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    author_env = _env(tmp_path / "author")
    _run(author_env, "source", "init", "personal")
    for name in ("demo", "plain"):
        created = json.loads(_run(
            author_env, "new", name, "--source", "personal").stdout)
        script = Path(created["executable"])
        script.write_text(script.read_text().replace(
            "TODO: describe the capability's smallest useful surface.",
            "Test capability with a completed managed manifest.",
        ).replace("Replace this scaffold check with", "Test readiness uses"))
    workspace = tmp_path / "author" / "registry" / "sources" / "personal"
    icon = workspace / "capabilities" / "demo" / "icon.png"

    icon.write_bytes(b"not a png")
    refused = _run(author_env, "source", "index", "personal", check=False)
    assert refused.returncode == 7
    assert "icon: icon.png is not a PNG" in json.loads(refused.stdout)["failures"]["demo"]
    icon.unlink()
    (icon.parent / "icon.svg").write_text("<svg/>")
    refused = _run(author_env, "source", "index", "personal", check=False)
    assert refused.returncode == 7
    assert any("icon.svg is not the icon's place" in problem
               for problem in json.loads(refused.stdout)["failures"]["demo"])
    (icon.parent / "icon.svg").unlink()

    icon.write_bytes(png(128, 128))
    _run(author_env, "source", "index", "personal")
    catalog = json.loads((workspace / ".capability-source" / "catalog.json").read_text())
    assert catalog["capabilities"]["demo"]["icon"] is True
    assert catalog["capabilities"]["plain"]["icon"] is False
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Add demo"], cwd=workspace, check=True,
        capture_output=True, text=True,
    )

    consumer_env = _env(tmp_path / "consumer")
    _run(consumer_env, "source", "add", "shared", str(workspace))
    search = json.loads(_run(consumer_env, "search", "--source", "shared").stdout)
    assert {row["name"]: row["icon"] for row in search["matches"]} == {
        "demo": True, "plain": False}
    for name in ("demo", "plain"):
        _run(consumer_env, "install", name, "--source", "shared")
    listed = json.loads(_run(consumer_env, "list").stdout)["installed"]
    assert {row["name"]: row["icon"] for row in listed} == {"demo": True, "plain": False}

    answer = json.loads(_run(consumer_env, "icon", "demo", "--json").stdout)
    assert answer["name"] == "demo"
    assert {k: answer["icon"][k] for k in ("format", "width", "height")} == {
        "format": "png", "width": 128, "height": 128}
    assert Path(answer["icon"]["path"]).read_bytes() == icon.read_bytes()
    missing = _run(consumer_env, "icon", "plain", check=False)
    assert missing.returncode == 3
    assert json.loads(missing.stderr.splitlines()[-1])["error"]["code"] == "no_icon"


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
    assert not Path(initialized["path"], "contract").exists()


def test_standalone_manager_selfcheck_reports_missing_source_without_traceback(tmp_path):
    env = _env(tmp_path)
    manager_dir = tmp_path / "registry" / ".manager"
    manager_dir.mkdir(parents=True)
    standalone = manager_dir / "capabilities"
    shutil.copy2(MANAGER, standalone)
    for relative in (
        "SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md", "ROUTINES.md",
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

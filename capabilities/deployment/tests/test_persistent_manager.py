"""A container node can keep the tools it installs and updates.

With compiler.container.persistent_manager the capabilities manager's whole
writable home is compiled onto named volumes. What is proven here: every
directory the manager writes sits on exactly one of them and every rename it
makes keeps both ends on one; the image puts the launcher directory first on
PATH and installs the catalogue there; each boot brings the registry to the
catalogue the image names; a capability service's own state mount keeps working beside them; and a
runtime that does not opt in compiles exactly as it did.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_checkout_profile import BUNDLE, _project, _run, _setup

HOME = "/home/agent"
# The compiler declares these once; the suite re-declares them so a change to
# the layout has to move both.
VOLUMES = {
    "capabilities_registry": HOME + "/.capabilities",
    "capabilities_bin": HOME + "/.capabilities-bin",
    "capabilities_config": HOME + "/.config/capabilities",
    "capabilities_state": HOME + "/.local/state/capabilities",
    "capabilities_data": HOME + "/.local/share/capabilities",
    "capabilities_cache": HOME + "/.cache/capabilities",
}
MANAGER = Path(__file__).resolve().parents[3] / "bin" / "capabilities"
PROFILES = ("agent-box-checkout", "agent-box")


def _opt_in(root: Path, env: dict[str, str], value: object = True) -> subprocess.CompletedProcess[str]:
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    container = runtime.setdefault("compiler", {}).setdefault("container", {})
    if value is None:
        container.pop("persistent_manager", None)
    else:
        container["persistent_manager"] = value
    path.write_text(json.dumps(runtime, indent=2) + "\n")
    return _run(root, env, "sync")


def _compiled(root: Path) -> dict[str, str]:
    files = {"runtime.json": (root / "deployment" / "runtime.json").read_text()}
    for path in sorted((root / BUNDLE).iterdir()):
        files[path.name] = path.read_text()
    return files


def _assert_settled(root: Path, env: dict[str, str]) -> None:
    """What a sync wrote is what doctor expects, and the runtime raises no
    finding of its own; an unconfigured service is the fixture's, not the
    option's."""
    doctor = json.loads(_run(root, env, "doctor").stdout)
    errors = [f for f in doctor["findings"] if f.get("severity") == "error"
              and "is not configured; run" not in f["message"]]
    assert errors == []


def _volume_of(path: str) -> str | None:
    owners = [name for name, mount in VOLUMES.items()
              if path == mount or path.startswith(mount + "/")]
    assert len(owners) <= 1, (path, owners)
    return owners[0] if owners else None


@pytest.mark.parametrize("profile", PROFILES)
def test_every_manager_directory_is_a_volume_every_container_mounts(tmp_path: Path, profile: str) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, profile)
    assert _opt_in(root, env).returncode == 0
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    for name, mount in VOLUMES.items():
        assert runtime["volumes"][name]["mount"] == mount
        assert name in runtime["services"]["agent"]["state"]
    compose = (root / BUNDLE / "docker-compose.yaml").read_text()
    declared = compose.split("\nvolumes:\n", 1)[1].splitlines()
    for name, mount in VOLUMES.items():
        assert f"      - {name}:{mount}" in compose
        assert f"  {name}:" in declared
    _assert_settled(root, env)


@pytest.mark.skipif(not MANAGER.is_file(), reason="the manager source is not beside this bundle")
def test_the_manager_writes_and_renames_only_inside_one_volume_each() -> None:
    # The manager's own paths as it resolves them in the image's environment.
    probe = r"""
import importlib.machinery, importlib.util, json, sys
loader = importlib.machinery.SourceFileLoader("manager_probe", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
releases = m.CAP_HOME / ".manager" / "releases"
bin_dir = m._bin_dir()
writes = {
    "registry entry": m._registry_dir("example"),
    "manager release": releases / "release-id",
    "skill": m.CANONICAL_SKILL_DIR,
    "source workspace": m.SOURCE_WORKSPACE_ROOT / "official",
    "launcher": bin_dir / "example",
    "sources.json": m.SOURCES_FILE, "global gate": m.GLOBAL_GATE_FILE,
    "machine.json": m.MACHINE_FILE, "source cache": m.SOURCE_CACHE_ROOT,
    "envelope cache": m.ENVELOPE_CACHE_ROOT,
    "dev state": m.DEV_STATE_ROOT, "dev data": m.DEV_DATA_ROOT,
    "mutation lock": m.MANAGER_MUTATION_LOCK,
    "release record": m._release_state_path(m.Path("/app"), "main"),
}
# (temporary or previous, final): each pair the manager renames between.
renames = {
    "payload stage -> registry entry": (m.CAP_HOME / ".example-stage-x", m._registry_dir("example")),
    "registry entry -> backup": (m._registry_dir("example"), m.CAP_HOME / ".example-previous"),
    "release stage -> release": (releases / ".manager-release-x", releases / "release-id"),
    "manager link swap": (bin_dir / ".capabilities.manager-release-x", bin_dir / "capabilities"),
    "bootstrap manager": (m.CAP_HOME / ".manager" / "capabilities.bootstrap-tmp", m.CAP_HOME / ".manager" / "capabilities"),
    "bootstrap link": (bin_dir / "capabilities.bootstrap-tmp", bin_dir / "capabilities"),
    "settings write": (m.GLOBAL_GATE_FILE.with_name(".settings.json.x.tmp"), m.GLOBAL_GATE_FILE),
    "release record write": (m._release_state_path(m.Path("/app"), "main").with_suffix(".json.tmp"),
                             m._release_state_path(m.Path("/app"), "main")),
    "envelope cache record": (m.ENVELOPE_CACHE_ROOT / "x.1.tmp", m.ENVELOPE_CACHE_ROOT / "x"),
}
print(json.dumps({"writes": {k: str(v) for k, v in writes.items()},
                  "renames": {k: [str(a), str(b)] for k, (a, b) in renames.items()}}))
"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("XDG_", "CAPABILITIES_"))}
    env.update(HOME=HOME, XDG_STATE_HOME=HOME + "/.local/state",
               CAPABILITIES_BIN=VOLUMES["capabilities_bin"])
    done = subprocess.run([sys.executable, "-c", probe, str(MANAGER)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    mapping = json.loads(done.stdout)
    for what, path in mapping["writes"].items():
        assert _volume_of(path), f"{what} at {path} is on no volume"
    for what, (before, after) in mapping["renames"].items():
        assert _volume_of(before) == _volume_of(after) is not None, what


@pytest.mark.parametrize("profile", PROFILES)
def test_the_image_puts_the_launchers_on_a_volume_first_on_path(tmp_path: Path, profile: str) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, profile)
    before = (root / BUNDLE / "Dockerfile").read_text()
    assert _opt_in(root, env).returncode == 0
    dockerfile = (root / BUNDLE / "Dockerfile").read_text()
    launchers = VOLUMES["capabilities_bin"]
    assert f"ENV CAPABILITIES_BIN={launchers}\n" in dockerfile
    assert f"ENV PATH={launchers}:{HOME}/.local/bin:${{PATH}}\n" in dockerfile
    assert f"ENV PATH={HOME}/.local/bin:${{PATH}}\n" not in dockerfile
    # Set in the image's own environment before the manager and the catalogue
    # are installed, so both land in the launcher directory.
    assert dockerfile.index("ENV CAPABILITIES_BIN=") < dockerfile.index("install.sh")
    assert 'capabilities install "$name" --allow' in dockerfile
    chown = next(line for line in dockerfile.splitlines() if line.startswith(" && chown "))
    mkdir = next(line for line in dockerfile.splitlines() if line.startswith("RUN mkdir -p ") and "/capabilities" in line)
    for mount in VOLUMES.values():
        relative = mount.replace(HOME, "$HOME", 1)
        assert f'"{relative}"' in mkdir and f'"{relative}"' in chown
    # The agent CLIs keep their links in the image.
    for line in (f"RUN ln -sf {HOME}/.local/bin/claude /usr/local/bin/claude",
                 f" && ln -sf {HOME}/.local/bin/codex /usr/local/bin/codex"):
        assert line in before and line in dockerfile
    assert '"$HOME/.local/bin/codex"' in dockerfile


def _boot_block(entrypoint: str) -> str:
    start = entrypoint.index("# --- Catalogue capabilities at the catalogue this image names")
    end = entrypoint.index("\nfi\n", entrypoint.index(
        "could not read the capability catalogue", start)) + len("\nfi\n")
    block = entrypoint[start:end]
    assert '"${CAPABILITIES_SOURCE}/.capability-source/catalog.json"' in block
    return block


@pytest.mark.parametrize("persistent", (False, True))
@pytest.mark.parametrize("profile", PROFILES)
def test_each_boot_installs_only_the_catalogue_the_registry_lacks(
        tmp_path: Path, profile: str, persistent: bool) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, profile)
    if persistent:
        assert _opt_in(root, env).returncode == 0
    entrypoint = (root / BUNDLE / "entrypoint.sh").read_text()
    assert "capabilities.lock" not in entrypoint
    assert subprocess.run(["bash", "-n", str(root / BUNDLE / "entrypoint.sh")]).returncode == 0
    box = tmp_path / "box"
    registry = box / "registry"
    (registry / "kept").mkdir(parents=True)
    (registry / "kept" / "kept").write_text("updated on the node\n")
    catalogue = box / "catalog.json"
    catalogue.write_text(json.dumps({"capabilities": {
        "kept": {}, "added": {}, "broken": {}}}))
    fake = box / "bin" / "capabilities"
    fake.parent.mkdir()
    fake.write_text(f"""#!/usr/bin/env bash
echo "$*" >> {box}/calls
if [ "$1 $2" = "self-update --check" ]; then echo '{{"update_available": false}}'; exit 0; fi
[ "$2" = broken ] && exit 5
mkdir -p "$CAPABILITIES_HOME/$2" && echo installed > "$CAPABILITIES_HOME/$2/$2"
""")
    fake.chmod(0o755)
    curl = box / "bin" / "curl"
    curl.write_text(f"""#!/usr/bin/env bash
echo "$*" >> {box}/fetched
[ -f {box}/offline ] && exit 22
cat {catalogue}
""")
    curl.chmod(0o755)
    script = box / "boot.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + _boot_block(entrypoint))
    run_env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
               "CAPABILITIES_HOME": str(registry),
               "CAPABILITIES_SOURCE": "https://catalogue.invalid/ref"}
    first = subprocess.run(["bash", str(script)], env=run_env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert (box / "fetched").read_text().split() == [
        "-fsSL", "https://catalogue.invalid/ref/.capability-source/catalog.json"]
    # Only a manager whose home persists can be older than the image's.
    manager = ["self-update --check"] if persistent else []
    assert (box / "calls").read_text().splitlines() == manager + [
        "install added --allow", "install broken --allow"]
    assert "could not install broken" in first.stderr
    # What the node updated is never reinstalled over.
    assert (registry / "kept" / "kept").read_text() == "updated on the node\n"
    (box / "calls").unlink()
    second = subprocess.run(["bash", str(script)], env=run_env, capture_output=True, text=True)
    assert second.returncode == 0
    assert (box / "calls").read_text().splitlines() == manager + ["install broken --allow"]
    # A catalogue that cannot be read still boots the box on what it has.
    (box / "calls").unlink()
    (box / "offline").touch()
    third = subprocess.run(["bash", str(script)], env=run_env, capture_output=True, text=True)
    assert third.returncode == 0, third.stderr
    calls = box / "calls"
    assert (calls.read_text().splitlines() if calls.exists() else []) == manager
    assert "could not read the capability catalogue" in third.stderr


@pytest.mark.parametrize("persistent", (False, True))
def test_each_boot_brings_installed_capabilities_to_the_catalogue_the_image_names(
        tmp_path: Path, persistent: bool) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box")
    if persistent:
        assert _opt_in(root, env).returncode == 0
    entrypoint = (root / BUNDLE / "entrypoint.sh").read_text()
    box = tmp_path / "box"
    registry = box / "registry"
    image = "https://catalogue.invalid/new"

    def payload(name: str, source: str | None, **meta: str) -> None:
        (registry / name).mkdir(parents=True)
        (registry / name / name).write_text(f"{name}\n")
        if source is not None:
            (registry / name / "meta.json").write_text(json.dumps({"source": source, **meta}))

    payload("older", "https://catalogue.invalid/old/capabilities/older/bin/older")
    payload("behind", f"{image}/capabilities/behind/bin/behind")
    payload("level", f"{image}/capabilities/level/bin/level")
    payload("local", "/somewhere/capabilities/local")
    payload("registered", f"{image}/capabilities/registered/bin/registered", source_id="dev")
    (box / "bin").mkdir(parents=True)
    (box / "catalog.json").write_text(json.dumps({"capabilities": {
        name: {} for name in ("older", "behind", "level", "local", "registered")}}))
    (box / "check.json").write_text(json.dumps({"checked": [
        {"name": name, "update_available": True}
        for name in ("behind", "local", "registered")] + [
        {"name": "level", "update_available": False}]}))
    (box / "manager.json").write_text(json.dumps({"update_available": True}))
    fake = box / "bin" / "capabilities"
    fake.write_text(f"""#!/usr/bin/env bash
echo "$*" >> {box}/calls
if [ "$1 $2" = "self-update --check" ]; then cat {box}/manager.json; exit 0; fi
if [ "$1 $2" = "update --check" ]; then cat {box}/check.json; exit 0; fi
""")
    fake.chmod(0o755)
    curl = box / "bin" / "curl"
    curl.write_text(f"#!/usr/bin/env bash\ncat {box / 'catalog.json'}\n")
    curl.chmod(0o755)
    script = box / "boot.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + _boot_block(entrypoint))
    run_env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
               "CAPABILITIES_HOME": str(registry), "CAPABILITIES_SOURCE": image}
    booted = subprocess.run(["bash", str(script)], env=run_env, capture_output=True, text=True)
    assert booted.returncode == 0, booted.stderr
    # The manager comes first, since an older one refuses newer payloads. One
    # from another catalogue is installed again from this one, keeping its
    # machine state; one from this catalogue is updated only where the manager
    # finds a different payload; one from elsewhere is never touched.
    manager = ["self-update --check", "self-update"] if persistent else []
    assert (box / "calls").read_text().splitlines() == manager + [
        "install older", "update --check", "update behind"]
    # A registry already at the catalogue does no update work.
    (box / "calls").unlink()
    (box / "manager.json").write_text(json.dumps({"update_available": False}))
    (box / "check.json").write_text(json.dumps({"checked": [
        {"name": name, "update_available": False} for name in ("older", "behind", "level")]}))
    (registry / "older" / "meta.json").write_text(json.dumps(
        {"source": f"{image}/capabilities/older/bin/older"}))
    again = subprocess.run(["bash", str(script)], env=run_env, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr
    assert (box / "calls").read_text().splitlines() == manager[:1] + ["update --check"]
    assert "updating" not in again.stdout


def test_a_service_state_mount_inside_the_manager_state_keeps_its_own_volume(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("automations",))
    _setup(root, env, "agent-box-checkout")
    assert _opt_in(root, env).returncode == 0
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    nested = HOME + "/.local/state/capabilities/projects"
    owners = [name for name, volume in runtime["volumes"].items() if volume.get("mount") == nested]
    assert owners, runtime["volumes"]
    compose = (root / BUNDLE / "docker-compose.yaml").read_text()
    for name in owners:
        assert f"- {name}:{nested}" in compose
    assert f"- capabilities_state:{VOLUMES['capabilities_state']}" in compose
    # Both mount points exist in the image, owned by the agent: Docker mounts the
    # parent volume before the child, which then covers its own directory.
    dockerfile = (root / BUNDLE / "Dockerfile").read_text()
    chown = next(line for line in dockerfile.splitlines() if line.startswith(" && chown "))
    assert '"$HOME/.local/state/capabilities/projects"' in chown
    assert '"$HOME/.local/state/capabilities"' in chown
    _assert_settled(root, env)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_runtime_that_does_not_opt_in_compiles_exactly_as_before(tmp_path: Path, profile: str) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, profile)
    before = _compiled(root)
    assert "capabilities_registry" not in json.dumps(before)
    assert _opt_in(root, env).returncode == 0
    assert _compiled(root) != before
    assert _opt_in(root, env, None).returncode == 0
    after = _compiled(root)
    assert after == before


def test_a_malformed_or_misplaced_opt_in_is_named(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    _setup(root, env, "agent-box-checkout")
    _opt_in(root, env, "yes")
    doctor = _run(root, env, "doctor")
    assert doctor.returncode != 0
    assert "persistent_manager must be true or false" in doctor.stdout + doctor.stderr

"""What the askproject suite shares: the script, the fake harnesses, and one ask.

The fakes under fakes/ speak the protocols callva-harness-runner's SDKs speak,
so an ask here runs the real library against them. Each launch is appended to
$PEER_RECORD, so a test reads what the harness was actually handed.

The script imports callva-harness-runner when a profile or a peer is needed, so
the suite runs where that library is importable:

    uv run --with pytest --with 'callva-harness-runner==0.4.0' \\
        python -m pytest capabilities/askproject/tests -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")
FAKES = Path(__file__).resolve().parent / "fakes"

def shipped_path(name: str) -> Path:
    """Where the library keeps the profile it ships as `name`."""
    from callva.harness_runner.discovery import shipped_folder
    return Path(str(shipped_folder().joinpath(f"{name}.toml")))


CHAIN_KEYS = ("ASKPROJECT_ENGINE", "ASKPROJECT_MODEL", "ASKPROJECT_EFFORT",
              "ASKPROJECT_TIMEOUT")


def fake_source(engine: str) -> str:
    return (FAKES / engine).read_text()


def install_fakes(bin_dir: Path) -> Path:
    """Copy both fake engines into `bin_dir`, executable, and return it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for engine in ("claude", "codex"):
        path = bin_dir / engine
        path.write_text(fake_source(engine))
        path.chmod(0o755)
    return bin_dir


def project(path: Path, git: bool = False) -> Path:
    """A directory the root detector takes for a project with askproject enabled."""
    envelope = path / "capabilities"
    envelope.mkdir(parents=True, exist_ok=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")
    if git and not (path / ".git").exists():
        (path / "README.md").write_text("target\n")
        subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                       cwd=path, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=path, check=True)
    return path


class Lab:
    """A caller project, a git target, both fakes on PATH, and isolated homes."""

    def __init__(self, root: Path):
        self.root = root
        self.caller = project(root / "caller")
        self.target = project(root / "target", git=True)
        self.bin = install_fakes(root / "bin")
        self.record = root / "peer-record.jsonl"
        self.config = root / "config"
        self.env = os.environ.copy()
        for key in (*CHAIN_KEYS, "CLAUDE_PROJECT_DIR", "CAPABILITIES_READ_ONLY",
                    "CAPABILITIES_STORE_URL", "CAPABILITIES_PROJECT_ENVELOPE",
                    "CAPABILITIES_PROJECT_ENVELOPE_ROOT"):
            self.env.pop(key, None)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["XDG_CONFIG_HOME"] = str(self.config)
        self.env["XDG_STATE_HOME"] = str(root / "state")
        self.env["PEER_RECORD"] = str(self.record)

    def write_profile(self, where: str, name: str, body: str) -> Path:
        """Write profile `name` into this project's folder ("project"), the
        library's machine folder ("machine"), or any folder under config."""
        folder = {
            "project": self.caller / "capabilities" / "askproject" / "profiles",
            "machine": self.config / "callva-harness-runner" / "profiles",
        }.get(where) or self.config / where
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.toml"
        path.write_text(body)
        return path

    def dotenv(self, name: str, values: dict | None) -> None:
        path = self.caller / name
        if values is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def run(self, *args: str, env: dict | None = None, timeout: int = 60):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.caller,
            env={**self.env, **(env or {})}, text=True, capture_output=True,
            timeout=timeout)

    def ask(self, *extra: str, env: dict | None = None, quiet: bool = True,
            timeout: int = 60):
        """One ask of the target; returns (process, parsed stdout or None, launch or None)."""
        self.record.unlink(missing_ok=True)
        args = [str(self.target), "what is here?", *extra]
        if quiet:
            args.append("--quiet")
        proc = self.run(*args, env=env, timeout=timeout)
        launches = ([json.loads(line) for line in self.record.read_text().splitlines()]
                    if self.record.exists() else [])
        try:
            result = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            result = None
        return proc, result, (launches[-1] if launches else None)


def value(argv: list[str], flag: str) -> str | None:
    """The value of `flag` in argv, in either spelling the SDK uses."""
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return None


def request(launch: dict, method: str) -> dict:
    """The params of the codex request `method` in a recorded launch."""
    return next(r["params"] for r in launch["requests"] if r["method"] == method)


def thread_params(launch: dict) -> dict:
    return next(r["params"] for r in launch["requests"]
                if r["method"] in ("thread/start", "thread/resume"))


def model_of(launch: dict) -> str | None:
    if launch["harness"] == "claude":
        return value(launch["argv"], "--model")
    return thread_params(launch).get("model")


def effort_of(launch: dict) -> str | None:
    if launch["harness"] == "claude":
        return value(launch["argv"], "--effort")
    return request(launch, "turn/start").get("effort")


def instructions_of(launch: dict) -> str | None:
    if launch["harness"] == "claude":
        return value(launch["argv"], "--append-system-prompt")
    return thread_params(launch).get("developerInstructions")

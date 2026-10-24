"""A run keeps its uv script environments to itself.

uv keys a script's environment on the script's path, and the suite installs
capabilities into a fresh temporary registry for nearly every test, so a run
against the machine's uv cache leaves one environment there per test and per
run, and nothing removes them. The run is given a uv cache of its own instead,
holding every bucket of the cache it was handed apart from the environments: the
wheels and interpreters stay shared, and the environments go with the run.

A script's first run in a cache reports its install on stderr, which a test
reading that stderr as JSON would take for its answer, so the checkout's own
scripts have their environments made quietly in the run's cache before any test
runs them."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_RUN_CACHE: Path | None = None


def pytest_configure(config):
    global _RUN_CACHE
    uv = shutil.which("uv")
    if uv is None or _RUN_CACHE is not None:
        return
    found = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True)
    if found.returncode != 0 or not found.stdout.strip():
        return
    shared = Path(found.stdout.strip())
    shared.mkdir(parents=True, exist_ok=True)
    # Beside the shared cache, on its filesystem, so what uv unpacks into a
    # shared bucket is still renamed into place and installs still link from it.
    run_cache = Path(tempfile.mkdtemp(prefix="uv-test-run-", dir=shared.parent))
    for entry in shared.iterdir():
        if not entry.name.startswith((".", "environments-")):
            (run_cache / entry.name).symlink_to(entry)
    os.environ["UV_CACHE_DIR"] = str(run_cache)
    _RUN_CACHE = run_cache
    checkout = Path(__file__).resolve().parent.parent
    scripts = [checkout / "bin" / "capabilities", *sorted(checkout.glob("capabilities/*/bin/*"))]
    for script in scripts:
        subprocess.run([uv, "sync", "-q", "--script", str(script)], capture_output=True)


def pytest_unconfigure(config):
    if _RUN_CACHE is None:
        return
    # uv also leaves a lock per script environment in the temporary directory,
    # named for the environment's digest.
    for bucket in _RUN_CACHE.glob("environments-*"):
        for environment in bucket.iterdir():
            digest = environment.name.rsplit("-", 1)[-1]
            Path(tempfile.gettempdir(), f"uv-{digest}.lock").unlink(missing_ok=True)
    # rmtree removes the links themselves and never descends through them.
    shutil.rmtree(_RUN_CACHE, ignore_errors=True)

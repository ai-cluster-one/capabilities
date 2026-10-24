"""Load the capability's executable and its service engine as modules, for tests.

The executable is one script; a source checkout keeps it under `bin/` and an
installed bundle at the bundle root, so the path is resolved rather than named.

Run with:
  uv run --no-project --with pytest --with httpx --with "psycopg[binary]" \
      --with 'capabilities-contract>=0.8,<1' --with callva-harness-runner==0.10.1 --with pyyaml \
      pytest capabilities/agentrooms/tests -q

The store-backed cases read AGENTROOMS_TEST_DSN, a libpq URL for a throwaway
database, and are skipped without it. Nothing here reaches a homeserver: the
CLI's HTTP client is handed a fake one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next((path for path in (CAPABILITY_DIR / "bin" / "agentrooms",
                                   CAPABILITY_DIR / "agentrooms") if path.is_file()),
                CAPABILITY_DIR / "bin" / "agentrooms")
ENGINE_PATH = CAPABILITY_DIR / "service" / "engine.py"


def load_cli():
    spec = importlib.util.spec_from_loader("agentrooms_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def load_engine():
    spec = importlib.util.spec_from_file_location("agentrooms_engine_test", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agentrooms_engine_test"] = module
    spec.loader.exec_module(module)
    return module

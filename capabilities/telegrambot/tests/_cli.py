"""Load the capability's executable and its bundled service modules, for tests.

The executable is one script; a source checkout keeps it under `bin/` and an
installed bundle at the bundle root, so the path is resolved rather than named.

Run inside the session's lane with:
  uv run --no-project --with pytest --with httpx --with "psycopg[binary]" \
      --with 'capabilities-contract>=0.42,<1' --with telethon==1.45.0 \
      --with callva-harness-runner==0.10.2 pytest capabilities/telegrambot/tests -q

The store-backed cases read TELEGRAMBOT_TEST_DSN, a libpq URL for a throwaway
database, and are skipped without it. Nothing here reaches Telegram: the HTTP
client is handed a fake Bot API and the MTProto reader a fake client.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next((path for path in (CAPABILITY_DIR / "bin" / "telegrambot",
                                   CAPABILITY_DIR / "telegrambot") if path.is_file()),
                CAPABILITY_DIR / "bin" / "telegrambot")
SERVICE_DIR = CAPABILITY_DIR / "service"


def load_cli():
    spec = importlib.util.spec_from_loader("telegrambot_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def load_service(name: str):
    path = SERVICE_DIR / f"{name}.py"
    key = f"telegrambot_{name}_test"
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module

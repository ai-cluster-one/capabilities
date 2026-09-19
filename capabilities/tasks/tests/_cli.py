#!/usr/bin/env python3
"""Load the capability's executable as a module, for tests.

The capability is one script, so a test reaches its internals the way the
manager installs it: as that file. The store driver is imported where a verb
connects, so the parser, the identity resolver and the schema plumbing load
with no store and nothing installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = CAPABILITY_DIR / "bin" / "tasks"


def load() -> object:
    """The executable as a module."""
    spec = importlib.util.spec_from_loader("tasks_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace(
        'if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module

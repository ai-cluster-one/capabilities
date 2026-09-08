#!/usr/bin/env python3
"""Load the capability's executable as a module, for tests.

The capability is one script, so a test reaches its internals the way the
manager installs it: as that file. The engine is imported lazily by the script,
so everything below the network — the store, the parser, the envelope — is
reachable with no engine present and no dependency to install.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = CAPABILITY_DIR / "bin" / "whatsapp"


class _Unused:
    """Stands in for a dependency the loaded module names but these tests never
    exercise. Reaching for anything on it yields another one, so import-time use
    resolves and call-time use would be visible immediately."""

    def __getattr__(self, name):
        return _Unused()

    def __call__(self, *args, **kwargs):
        return _Unused()


def load(*, require: tuple[str, ...] = ()) -> object:
    """The executable as a module. Names in `require` must import for real."""
    for name in ("httpx", "puremagic"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            if name in require:
                raise
            sys.modules[name] = _Unused()
    spec = importlib.util.spec_from_loader("whatsapp_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace(
        'if __name__ == "__main__":\n    _run()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def engine_available(module) -> bool:
    """Whether the compiled engine loads on this host. It ships per platform, so
    the chunk-parsing tests skip rather than fail where it was not built."""
    try:
        module._engine()
        return True
    except BaseException:
        return False

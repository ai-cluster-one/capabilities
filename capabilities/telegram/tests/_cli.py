#!/usr/bin/env python3
"""Where the capability's executable is, for tests.

The path differs between the two trees a test runs in. A source checkout keeps
the executable under `bin/`; an installed bundle keeps it at the bundle root.
A test that names only one of them passes where it was written and fails where
the capability is actually installed - and the installed tree is the one the
suite is never run in until a release refuses to land on someone's machine.

So the resolution lives here once. A test that reads the executable imports
this rather than spelling a path of its own, and a test file added later
inherits the answer instead of guessing at it again.
"""

from __future__ import annotations

from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next(
    (path for path in (CAPABILITY_DIR / "bin" / "telegram",
                       CAPABILITY_DIR / "telegram")
     if path.is_file()),
    CAPABILITY_DIR / "bin" / "telegram")

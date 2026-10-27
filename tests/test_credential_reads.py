"""No capability module but its CLI names a credential file.

A capability's CLI binds the contract library's connections tier, and that
tier is the credential cascade (SHEBANG.md, "The credential cascade"): project
env, user config, process env, the agent's vault. A bundled service runs
without the CLI in process, and telegram's daemon and call recorder each grew a
cascade of their own that read `credentials.env` and `shared.env` and stopped
there, so a key `telegram connections` found in the vault the service never
did. A service asks the CLI's cascade instead (telegram's `service/records.py`,
`credential`), so whatever the library's cascade learns next reaches it too.

Every second cascade has to name the user-config file to be one, so that name
is what this looks for: a string that is a path to `credentials.env` or
`shared.env`, in any Python module of a bundle other than its CLI. Tests are
left out; they write these files to prove the cascade reads them.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAPABILITIES = REPO / "capabilities"
CREDENTIAL_FILE = re.compile(r"(?:^|/)(?:credentials|shared)\.env$")


def _python_modules(bundle: Path):
    """Every Python file of the bundle except its CLI and its tests."""
    cli = bundle / "bin" / bundle.name
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path == cli:
            continue
        rel = path.relative_to(bundle).parts
        if "tests" in rel or "__pycache__" in rel:
            continue
        if path.suffix == ".py":
            yield path
            continue
        try:
            first = path.open("rb").readline()
        except OSError:
            continue
        if first.startswith(b"#!") and (b"python" in first or b"uv run" in first):
            yield path


def _credential_paths(path: Path):
    """(line, value) for each string constant naming a credential file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and CREDENTIAL_FILE.search(node.value.strip())):
            yield node.lineno, node.value


class CredentialReadsTests(unittest.TestCase):
    def test_no_module_but_the_cli_names_a_credential_file(self):
        found = []
        for bundle in sorted(p for p in CAPABILITIES.iterdir() if p.is_dir()):
            for module in _python_modules(bundle):
                for line, value in _credential_paths(module):
                    found.append(f"{module.relative_to(REPO)}:{line}: {value!r}")
        self.assertEqual(found, [], (
            "a module outside the CLI names a credential file, so it resolves "
            "credentials by a cascade of its own; ask the CLI's cascade "
            "(the contract library's connections tier) instead:\n"
            + "\n".join(found)))


if __name__ == "__main__":
    unittest.main()

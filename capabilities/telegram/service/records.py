"""The records adapter, reached from the bundle's service processes.

The daemon and the call recorder run without the CLI in process, and read
their configuration through the same adapter the CLI's verbs do: the
contract's store region, which the contract vendors into this bundle's CLI and
which carries everything it needs. It is executed here on its own, so a
service process takes the adapter and nothing else of the CLI with it.

In a files-mode project the adapter reads the envelope's files and then the
user's config home's; in a store-mode project it reads connections and grants
from the project's store, and for an agent whose records moved into the store
everything, service settings included, comes from it.
"""
from __future__ import annotations

import types
from pathlib import Path

NAME = "telegram"
# A service process writes through a connection only where its grant says so;
# nothing declared opens it.
WRITE_DEFAULT = False
REGION_START = "# >>> contract: store "
REGION_END = "# <<< contract: store <<<"

_STORE = None


def _cli_path() -> Path:
    """The bundle's CLI: under `bin/` in a source bundle, beside `service/` in
    an installed payload."""
    bundle = Path(__file__).resolve().parent.parent
    for candidate in (bundle / "bin" / NAME, bundle / NAME):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"the {NAME} CLI is missing from its bundle: {bundle}")


def store():
    """The contract's store region, executed once from the bundle's CLI."""
    global _STORE
    if _STORE is None:
        path = _cli_path()
        text = path.read_text()
        try:
            start = text.index(REGION_START)
            end = text.index(REGION_END, start)
        except ValueError:
            raise RuntimeError(f"{path} carries no contract store region") from None
        module = types.ModuleType(f"{NAME}_contract_store")
        module.__file__ = str(path)
        exec(compile(text[start:end], str(path), "exec"), module.__dict__)
        _STORE = module
    return _STORE


def open_records(envelope: Path, config_home: Path, project_root: Path,
                 project_id: str | None = None):
    """The adapter for the project whose envelope is `envelope`, its global
    scope `config_home`. Raises the region's StoreError."""
    return store().open_records(envelope, config_home, project_root=project_root,
                                project_id=project_id or None,
                                application_name=NAME)


def service_settings(adapter) -> dict:
    """The service settings, the project's over the config home's key by key.

    Each scope's settings document is the one the adapter keeps for it: the
    file in a project whose records are files, the store's rows for an agent
    whose records moved there. The adapter's setting collection also carries
    the default pointer kept with the connections, which the settings do not
    include, so reading them never depends on the connections being
    readable."""
    merged: dict = {}
    for scope in ("project", "global"):
        body = adapter.setting_document(NAME, scope)
        if isinstance(body, dict):
            for key, value in body.items():
                merged.setdefault(key, value)
    merged.pop("connection.default", None)
    return merged


def connections(adapter) -> tuple[dict | None, str]:
    """The connections this project may use, in the envelope shape the
    services select from, and where the collection was read.

    `connections` holds what may be acted on, each carrying the `allow_write`
    its grant decided; `withheld` names what the project can see but was not
    granted, so selection can refuse it by name. None where nothing is
    declared at all."""
    effective = adapter.connections(NAME, write_default=WRITE_DEFAULT,
                                    include_disabled=True)
    source = adapter.collection_source(NAME, "connection")
    if not effective:
        return None, source
    return {
        "default": adapter.get(NAME, "setting", "connection.default"),
        "connections": {cid: {**entry["value"], "allow_write": entry["allow_write"]}
                        for cid, entry in effective.items() if entry["enabled"]},
        "withheld": sorted(cid for cid, entry in effective.items()
                           if not entry["enabled"]),
    }, source

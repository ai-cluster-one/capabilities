"""A connection's service state on this machine, kept under its id: state made
under the name the connection had before connections were given ids is found
and moved under the id once. No network, no Slack."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

CLI_PATH = next((path for path in (Path(__file__).resolve().parents[1] / "bin" / "slack",
                                   Path(__file__).resolve().parents[1] / "slack")
                 if path.is_file()), Path(__file__).resolve().parents[1] / "bin" / "slack")


def _load():
    """The executable as a module, run up to its entry point and never past it."""
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("slack_cli_state_test", loader=None))
    module.__file__ = str(CLI_PATH)
    exec(compile(CLI_PATH.read_text(), str(CLI_PATH), "exec"), module.__dict__)
    return module


def test_service_state_made_under_the_former_name_is_found_and_moved(tmp_path, monkeypatch):
    cli = _load()
    cid = "a1b2c3d4e5f6"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_connection_former_name",
                        lambda wanted: "work" if wanted == cid else None, raising=False)
    made = tmp_path / "state" / "slack" / "work" / "service" / "watermarks.json"
    made.parent.mkdir(parents=True)
    made.write_text("{}")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        found = cli._service_state_dir(cid)
    assert found == tmp_path / "state" / "slack" / cid / "service"
    assert (found / "watermarks.json").read_text() == "{}"
    assert not (tmp_path / "state" / "slack" / "work").exists()
    assert "work" in err.getvalue()

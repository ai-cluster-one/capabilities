import json
import stat

import pytest
from runtime_settings import RuntimeSettings


def test_runtime_settings_persist_and_return_copies(tmp_path):
    path = tmp_path / "state" / "conversation-settings.json"
    store = RuntimeSettings(path)
    store.replace("D1", {"settings": {"worker": "codex"}})

    row = store.get("D1")
    row["settings"]["worker"] = "stub"

    assert RuntimeSettings(path).get("D1") == {"settings": {"worker": "codex"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_runtime_settings_fail_closed_on_corruption(tmp_path):
    path = tmp_path / "conversation-settings.json"
    path.write_text("{not-json")
    with pytest.raises(ValueError, match="cannot load runtime settings"):
        RuntimeSettings(path)


def test_runtime_settings_require_an_object(tmp_path):
    path = tmp_path / "conversation-settings.json"
    path.write_text(json.dumps([]))
    with pytest.raises(ValueError, match="must contain a JSON object"):
        RuntimeSettings(path)

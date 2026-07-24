import json
from pathlib import Path

from register import Register


def test_reserve_is_true_once_then_false(tmp_path):
    reg = Register(tmp_path / "register.json")
    assert reg.reserve("C1:100.1") is True
    assert reg.reserve("C1:100.1") is False  # dedup


def test_terminal_state_persists_across_instances(tmp_path):
    path = tmp_path / "register.json"
    r = Register(path)
    r.reserve("C1:100.1")
    r.mark_done("C1:100.1")
    assert Register(path).reserve("C1:100.1") is False  # done persists → dedup


def test_reserved_is_reset_on_load(tmp_path):
    path = tmp_path / "register.json"
    Register(path).reserve("C1:100.1")               # in-progress, never completed
    assert Register(path).reserve("C1:100.1") is True  # reset-and-recover → re-reservable


def test_mark_done_and_is_done(tmp_path):
    reg = Register(tmp_path / "register.json")
    reg.reserve("C1:100.1")
    assert reg.is_done("C1:100.1") is False
    reg.mark_done("C1:100.1")
    assert reg.is_done("C1:100.1") is True


def test_file_is_valid_json_after_writes(tmp_path):
    path = tmp_path / "register.json"
    reg = Register(path)
    reg.reserve("C1:100.1")
    reg.mark_done("C1:100.1")
    data = json.loads(path.read_text())
    assert data["C1:100.1"] == "done"


def test_missing_parent_dir_is_created(tmp_path):
    reg = Register(tmp_path / "nested" / "register.json")
    assert reg.reserve("C1:1") is True
    assert (tmp_path / "nested" / "register.json").is_file()


def test_mark_error_is_terminal_and_dedups(tmp_path):
    path = tmp_path / "register.json"
    r = Register(path)
    r.reserve("C1:100.1")
    r.mark_error("C1:100.1")
    assert Register(path).reserve("C1:100.1") is False  # error persists → not retried


def test_prune_caps_terminal_keys(tmp_path):
    import register as reg_mod
    reg_mod.REGISTER_MAX_KEYS = 3
    path = tmp_path / "register.json"
    r = Register(path)
    for i in range(6):
        k = f"C1:{i}.0"
        r.reserve(k)
        r.mark_done(k)
    assert len(r._data) <= 3
    # the most recent key survives; the oldest is pruned
    assert "C1:5.0" in r._data
    assert "C1:0.0" not in r._data
    reg_mod.REGISTER_MAX_KEYS = 5000

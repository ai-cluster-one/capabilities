from watermark import Watermark


def test_get_none_initially(tmp_path):
    assert Watermark(tmp_path / "w.json").get("C1") is None


def test_advance_sets_and_moves_forward(tmp_path):
    w = Watermark(tmp_path / "w.json")
    assert w.advance("C1", "100.5") is True
    assert w.get("C1") == "100.5"
    assert w.advance("C1", "101.0") is True
    assert w.get("C1") == "101.0"


def test_advance_ignores_older_or_equal(tmp_path):
    w = Watermark(tmp_path / "w.json")
    w.advance("C1", "200.0")
    assert w.advance("C1", "150.0") is False
    assert w.advance("C1", "200.0") is False
    assert w.get("C1") == "200.0"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "w.json"
    Watermark(path).advance("C1", "300.0")
    assert Watermark(path).get("C1") == "300.0"


def test_keys_lists_channels(tmp_path):
    w = Watermark(tmp_path / "w.json")
    w.advance("C1", "1.0")
    w.advance("D2", "2.0")
    assert sorted(w.keys()) == ["C1", "D2"]

"""Full build: bundled TabPFN checkpoint lookup, selftest and its CLI/web entry points."""

import importlib.util
import json
import sys
import types

import pytest

from slothfile import cli, predict

HAS_TABPFN = importlib.util.find_spec("tabpfn") is not None and importlib.util.find_spec("torch") is not None
CKPT = "tabpfn-v2-classifier-finetuned-zk73skhh.ckpt"


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Pretend to be the PyInstaller exe with _internal at tmp_path/_internal."""
    internal = tmp_path / "_internal"
    internal.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    return internal


def test_no_bundled_checkpoint_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "tabpfn_models").mkdir()
    (tmp_path / "tabpfn_models" / CKPT).write_bytes(b"x")
    assert predict.bundled_checkpoint() is None
    assert "downloaded on first use" in predict.weights_source()


def test_bundled_checkpoint_found_when_frozen(frozen):
    assert predict.bundled_checkpoint() is None  # lean build: no models folder
    models = frozen / "tabpfn_models"
    models.mkdir()
    (models / "notes.txt").write_text("x")
    (models / "tabpfn-v2-classifier-dir.ckpt").mkdir()  # a directory is not a checkpoint
    assert predict.bundled_checkpoint() is None
    (models / CKPT).write_bytes(b"x")
    assert predict.bundled_checkpoint() == models / CKPT
    assert predict.weights_source() == f"bundled with the app ({CKPT})"


def test_frozen_classifier_uses_bundled_model_path(frozen, monkeypatch):
    (frozen / "tabpfn_models").mkdir()
    (frozen / "tabpfn_models" / CKPT).write_bytes(b"x")
    calls = []

    class FakeClf:
        @classmethod
        def create_default_for_version(cls, version, **kw):
            calls.append((version, kw))
            return cls()

    ModelVersion = types.SimpleNamespace(V2="v2", V3_5_FAST="v3.5-fast")
    monkeypatch.setitem(sys.modules, "tabpfn", types.SimpleNamespace(TabPFNClassifier=FakeClf))
    monkeypatch.setitem(sys.modules, "tabpfn.constants", types.SimpleNamespace(ModelVersion=ModelVersion))
    monkeypatch.setenv("TABPFN_TOKEN", "t")  # would pick v3.5 on a source install
    predict._make_classifier()
    version, kw = calls[-1]
    assert version == "v2" and kw["model_path"] == str(frozen / "tabpfn_models" / CKPT)
    assert kw["device"] == "cpu" and kw["n_estimators"] == predict.N_ESTIMATORS
    # without the bundled file the old behaviour (cache/download, maybe v3.5) is kept
    (frozen / "tabpfn_models" / CKPT).unlink()
    predict._make_classifier()
    version, kw = calls[-1]
    assert version == "v3.5-fast" and "model_path" not in kw


def test_info_shape(frozen):
    info = predict.info()
    assert set(info) == {"available", "detail", "weights", "bundled", "history", "min_rows",
                         "min_per_class", "last_run", "backtest"}
    assert info["bundled"] is False and info["history"]["rows"] == 0 and info["backtest"] is None


def test_auc():
    assert predict._auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0
    assert predict._auc([1, 0], [0.5, 0.5]) == 0.5
    assert predict._auc([1, 0, 1, 0], [0.1, 0.9, 0.2, 0.8]) == 0.0


def test_synthetic_rows_are_valid_reviewlog_rows():
    import random
    from slothfile import reviewlog
    rows = predict._synthetic_rows(50, random.Random(1))
    assert all(reviewlog._clean(r) == r for r in rows)
    assert {r["label"] for r in rows} == {0, 1}


def test_selftest_cli_without_tabpfn(monkeypatch, capsys):
    monkeypatch.setattr(predict, "available", lambda: (False, "torch is not installed"))
    assert cli.main(["predictor", "selftest"]) == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["available"] is False


def test_status_cli(capsys):
    assert cli.main(["predictor", "status"]) == 0
    assert "history" in json.loads(capsys.readouterr().out)


@pytest.mark.skipif(not HAS_TABPFN, reason="tabpfn not installed")
def test_selftest_cli_really_predicts(monkeypatch, capsys):
    monkeypatch.setattr(predict, "_model_cache", None)
    assert cli.main(["predictor", "selftest"]) == 0, capsys.readouterr().out
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["auc"] > 0.85 and out["accuracy"] > 0.8, out
    from slothfile import reviewlog
    assert reviewlog.load_rows() == []  # the user's history is never touched

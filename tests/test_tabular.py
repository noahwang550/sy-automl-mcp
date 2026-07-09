"""End-to-end Tabular tests (requires AutoGluon — run inside the container).

Skipped automatically when AutoGluon is not importable, so this file is safe
to collect on a dev host without the heavy install.
"""
from __future__ import annotations

import io
import sys
import time

import numpy as np
import pytest

ag = pytest.importorskip("autogluon.tabular")  # noqa: F841

from tasks import get_task_manager  # noqa: E402
from tasks.manager import FAILED, SUCCESS  # noqa: E402
from tools.data import load_dataset  # noqa: E402
from tools.tabular import (  # noqa: E402
    evaluate_tabular,
    feature_importance_tabular,
    leaderboard_tabular,
    predict_tabular,
    train_tabular,
)


def _synthetic_csv(n: int = 80, seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    x1 = rng.normal(0, 1, n)
    x2 = rng.normal(0, 1, n)
    # Simple linear-separable label
    label = (x1 + x2 > 0).astype(int)
    rows = ["f1,f2,y"]
    for a, b, y in zip(x1, x2, label, strict=False):
        rows.append(f"{a:.4f},{b:.4f},{int(y)}")
    return "\n".join(rows) + "\n"


def _wait(task_id, mgr, timeout=120):
    for _ in range(int(timeout / 0.5)):
        st = mgr.status(task_id)["status"]
        if st in {SUCCESS, FAILED}:
            return st
        time.sleep(0.5)
    return mgr.status(task_id)["status"]


def _data(result):
    assert result["success"] is True, result.get("error")
    return result["data"]


def test_train_predict_leaderboard(isolated_artifacts):
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_synthetic_csv(), dataset_id="synth")
    res = train_tabular(
        dataset_id="synth",
        target="y",
        model_id="synth_model",
        time_limit=30,
        presets=None,
        hyperparameters={"RF": {}},  # RandomForest; GBM requires lightgbm which may not be installed
        random_seed=0,
    )
    payload = _data(res)
    assert payload["model_id"] == "synth_model"
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

    lb = _data(leaderboard_tabular("synth_model"))
    assert lb["best_model"] is not None
    assert isinstance(lb["leaderboard"], list)

    pred = _data(predict_tabular(
        model_id="synth_model",
        inline_csv="f1,f2\n1.0,1.0\n-1.0,-1.0\n",
    ))
    assert "predictions" in pred
    assert len(pred["predictions"]) == 2

    # Default evaluate returns a dict of all available metrics.
    ev_all = _data(evaluate_tabular("synth_model", "synth"))
    assert isinstance(ev_all["metrics"], dict)
    assert len(ev_all["metrics"]) >= 1

    # Requested metric subset is honored (only keys that exist are returned);
    # this previously crashed because the tool passed `metric=` to
    # TabularPredictor.evaluate(), which AutoGluon 1.5.0 does not accept.
    ev_sub = _data(evaluate_tabular("synth_model", "synth", metrics=["accuracy"]))
    assert set(ev_sub["metrics"].keys()) <= {"accuracy"}
    assert len(ev_sub["metrics"]) == 1


def test_feature_importance_tabular_regression(isolated_artifacts):
    """feature_importance must not pass unsupported kwargs (e.g. verbosity)."""
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_synthetic_csv(), dataset_id="synth")
    res = train_tabular(
        dataset_id="synth",
        target="y",
        model_id="fi_model",
        time_limit=30,
        presets=None,
        hyperparameters={"RF": {}},
        random_seed=0,
    )
    payload = _data(res)
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

    fi = _data(feature_importance_tabular("fi_model"))
    assert "importances" in fi
    assert isinstance(fi["importances"], list)
    assert len(fi["importances"]) > 0

    fi_with_data = _data(feature_importance_tabular("fi_model", dataset_id="synth"))
    assert "importances" in fi_with_data
    assert isinstance(fi_with_data["importances"], list)


def test_predict_requires_exactly_one_data_source(isolated_artifacts):
    res = predict_tabular(model_id="m", dataset_id="d", inline_csv="a,b\n1,2\n")
    assert res["success"] is False
    assert "exactly one" in res["error"].lower()

    res = predict_tabular(model_id="m")
    assert res["success"] is False
    assert "exactly one" in res["error"].lower()


def test_train_with_bad_target(isolated_artifacts):
    load_dataset(source=_synthetic_csv(), dataset_id="synth")
    res = train_tabular(
        dataset_id="synth",
        target="missing_column",
        model_id="bad_target_model",
        time_limit=10,
    )
    payload = _data(res)
    from tasks import get_task_manager

    mgr = get_task_manager()
    status = _wait(payload["task_id"], mgr, timeout=60)
    assert status == FAILED


def test_evaluate_unsupported_metric_returns_empty(isolated_artifacts):
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_synthetic_csv(), dataset_id="synth")
    res = train_tabular(
        dataset_id="synth",
        target="y",
        model_id="eval_model",
        time_limit=30,
        presets=None,
        hyperparameters={"RF": {}},
        random_seed=0,
    )
    payload = _data(res)
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

    ev = _data(evaluate_tabular("eval_model", "synth", metrics=["not_a_real_metric"]))
    assert ev["metrics"] == {}


def test_fit_summary_after_train(isolated_artifacts):
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_synthetic_csv(), dataset_id="synth")
    res = train_tabular(
        dataset_id="synth",
        target="y",
        model_id="summary_model",
        time_limit=30,
        presets=None,
        hyperparameters={"RF": {}},
        random_seed=0,
    )
    payload = _data(res)
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

def test_concurrent_training_keeps_stdout_clean(isolated_artifacts, monkeypatch):
    """With MCP_MAX_WORKERS>=2, concurrent trainings must not leak stdout."""
    from tasks import manager as manager_mod

    monkeypatch.setattr(manager_mod, "MCP_MAX_WORKERS", 2)
    manager_mod._SINGLETON = None

    real_stdout = sys.stdout
    capture = io.StringIO()
    sys.stdout = capture
    try:
        load_dataset(source=_synthetic_csv(), dataset_id="synth")
        mgr = get_task_manager()
        ids = []
        for i in range(2):
            res = train_tabular(
                dataset_id="synth",
                target="y",
                model_id=f"conc_model_{i}",
                time_limit=20,
                presets=None,
                hyperparameters={"RF": {}},
                random_seed=i,
            )
            payload = _data(res)
            ids.append(payload["task_id"])

        for tid in ids:
            status = _wait(tid, mgr, timeout=120)
            assert status == SUCCESS, mgr.result(tid)
    finally:
        sys.stdout = real_stdout

    leaked = capture.getvalue()
    forbidden = ["epoch", "Epoch", "training", "Progress", "bar", "LightGBM", "torch"]
    found = [token for token in forbidden if token in leaked]
    assert not found, f"stdout leakage detected: {found}"

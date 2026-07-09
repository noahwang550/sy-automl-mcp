"""End-to-end Tabular tests (requires AutoGluon — run inside the container).

Skipped automatically when AutoGluon is not importable, so this file is safe
to collect on a dev host without the heavy install.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

ag = pytest.importorskip("autogluon.tabular")  # noqa: F841

from tasks.manager import FAILED, SUCCESS  # noqa: E402
from tools.data import load_dataset  # noqa: E402
from tools.tabular import (  # noqa: E402
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
    for a, b, y in zip(x1, x2, label):
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

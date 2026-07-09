"""Tests for AutoGluon task-log progress parsing (no AutoGluon required).

Covers the best-effort parser in ``tasks/progress.py``: absent/missing logs,
multi-model logs with validation scores, and logs with no AutoGluon markers.
"""
from __future__ import annotations

from pathlib import Path

from tasks.progress import parse_progress


def _write_log(tmp_path: Path, name: str, lines: list[str]) -> str:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_parse_progress_no_log_path():
    out = parse_progress(None, "pending")
    assert out["available"] is False
    assert out["status"] == "pending"


def test_parse_progress_missing_file(tmp_path):
    out = parse_progress(str(tmp_path / "does-not-exist.log"), "running")
    assert out["available"] is False
    assert out["status"] == "running"


def test_parse_progress_full_training_log(tmp_path):
    log = _write_log(
        tmp_path,
        "train.log",
        [
            "[2026-07-09T09:15:26] START train_tabular params={'target': 'y'}",
            "Warning: CatBoost missing — skipping.",
            "Fitting 5 model(s) per training round (L-BFGS): ...",
            "Fitting model: RandomForestGini ... training",
            "  0.8531  = Validation score (accuracy) | RandomForestGini",
            "Fitting model: ExtraTreesEntr ... training",
            "  0.9100  = Validation score (accuracy) | ExtraTreesEntr",
            "[2026-07-09T09:15:36] SUCCESS summary={'best_model': 'ExtraTreesEntr'}",
        ],
    )
    out = parse_progress(log, "success")
    assert out["available"] is True
    assert out["status"] == "success"
    assert out["announced_models"] == 5
    assert out["models_attempted"] == 2
    # Latest (last seen) score/model — not a claimed "best".
    assert out["latest_score"] == 0.91
    assert out["latest_model"] == "ExtraTreesEntr"
    assert out["metric"] == "accuracy"
    assert len(out["recent_lines"]) > 0
    # The terminal SUCCESS line should be the last recent line surfaced.
    assert out["recent_lines"][-1].startswith("[2026-07-09T09:15:36]")


def test_parse_progress_keeps_latest_score_when_scores_decrease(tmp_path):
    """The parser reports the latest score, not the max — so a later, lower
    score overwrites an earlier higher one."""
    log = _write_log(
        tmp_path,
        "train.log",
        [
            "Fitting 3 model(s) ...",
            "Fitting model: GoodModel ...",
            "  0.95  = Validation score (accuracy) | GoodModel",
            "Fitting model: WorseModel ...",
            "  0.42  = Validation score (accuracy) | WorseModel",
        ],
    )
    out = parse_progress(log, "running")
    assert out["latest_score"] == 0.42
    assert out["latest_model"] == "WorseModel"
    assert out["models_attempted"] == 2
    assert out["announced_models"] == 3


def test_parse_progress_log_without_autogluon_markers(tmp_path):
    log = _write_log(
        tmp_path,
        "plain.log",
        [
            "[2026-07-09T09:00:00] START predict_tabular params={}",
            "[2026-07-09T09:00:01] SUCCESS summary={}",
        ],
    )
    out = parse_progress(log, "success")
    assert out["available"] is True
    assert out["announced_models"] is None
    assert out["models_attempted"] == 0
    assert out["latest_score"] is None
    assert out["latest_model"] is None
    assert out["metric"] is None
    assert len(out["recent_lines"]) == 2


def test_parse_progress_drops_blank_lines_from_recent(tmp_path):
    log = _write_log(
        tmp_path,
        "blank.log",
        ["Fitting 2 model(s) ...", "", "  ", "Fitting model: X ..."],
    )
    out = parse_progress(log, "running")
    assert all(line.strip() for line in out["recent_lines"])
    assert out["models_attempted"] == 1

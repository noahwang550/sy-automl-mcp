"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from tasks import manager as manager_mod


@pytest.fixture()
def isolated_artifacts(tmp_path: Path, monkeypatch) -> Path:
    """Redirect all artifact paths to a temp dir and reset the TaskManager singleton."""
    config.configure(tmp_path / "artifacts")
    # Force a fresh TaskManager bound to the temp dirs.
    manager_mod._SINGLETON = None
    yield tmp_path / "artifacts"
    manager_mod._SINGLETON = None


@pytest.fixture()
def iris_csv() -> str:
    """A tiny inline CSV (4 rows, 2 features + label) for fast tests."""
    return (
        "sepal_length,sepal_width,species\n"
        "5.1,3.5,setosa\n"
        "4.9,3.0,setosa\n"
        "6.2,3.4,versicolor\n"
        "5.9,3.0,versicolor\n"
    )

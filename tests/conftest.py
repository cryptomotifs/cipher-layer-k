"""Shared fixtures for cipher-layer-k tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "trades.db"


@pytest.fixture()
def tmp_outflow_db(tmp_path: Path) -> Path:
    return tmp_path / "outflow.db"

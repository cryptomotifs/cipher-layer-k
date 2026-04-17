"""Unit tests for `cipher_layer_k.pnl_tracker`."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from cipher_layer_k.pnl_tracker import PnLTracker, ensure_schema


def _insert(
    db_path: Path,
    *,
    realised: float,
    filled: float = 100.0,
    status: str = "landed",
    mode: str = "paper",
    created_at: float | None = None,
) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO trades (
                trade_id, signal_id, asset_ticker, mint, side, mode, reason,
                requested_size_usd, filled_size_usd, realised_pnl_usd,
                status, tx_signature, created_at, completed_at, error
            ) VALUES (?, ?, 'TEST', 'MINT', 'BUY', ?, 'ENTRY',
                      ?, ?, ?, ?, NULL, ?, ?, NULL)
            """,
            (
                str(uuid.uuid4()),
                "sig-1",
                mode,
                filled,
                filled,
                realised,
                status,
                created_at or time.time(),
                created_at or time.time(),
            ),
        )
        conn.commit()


def test_empty_db_safe(tmp_db):
    ensure_schema(tmp_db)
    t = PnLTracker(db_path=tmp_db)
    assert t.cumulative_pnl() == 0.0
    assert t.win_rate() == 0.0
    assert t.max_drawdown() == 0.0
    assert t.sharpe_30d() == 0.0
    assert t.summary()["trade_count"] == 0


def test_cumulative_and_win_rate(tmp_db):
    ensure_schema(tmp_db)
    for pnl in [10.0, -5.0, 7.0, -3.0, 2.0]:
        _insert(tmp_db, realised=pnl)
    t = PnLTracker(db_path=tmp_db)
    assert round(t.cumulative_pnl(), 6) == 11.0
    # 3 winners / 5 total = 0.6
    assert round(t.win_rate(), 4) == 0.6


def test_max_drawdown(tmp_db):
    ensure_schema(tmp_db)
    # Running: 10, 5, 15, 12, 2. Peak 15, trough 2. DD = 13.
    now = time.time()
    for i, pnl in enumerate([10.0, -5.0, 10.0, -3.0, -10.0]):
        _insert(tmp_db, realised=pnl, created_at=now + i)
    t = PnLTracker(db_path=tmp_db)
    assert round(t.max_drawdown(), 6) == 13.0


def test_to_csv_has_header_and_rows(tmp_db):
    ensure_schema(tmp_db)
    _insert(tmp_db, realised=1.0)
    _insert(tmp_db, realised=-1.0)
    csv = PnLTracker(db_path=tmp_db).to_csv()
    lines = [ln for ln in csv.splitlines() if ln.strip()]
    assert lines[0].startswith("trade_id,")
    assert len(lines) == 3  # header + 2 data rows


def test_sharpe_returns_finite(tmp_db):
    ensure_schema(tmp_db)
    base = time.time() - (5 * 86_400)
    for i, pnl in enumerate([2.0, -1.0, 3.0, -2.0, 1.5]):
        _insert(tmp_db, realised=pnl, created_at=base + (i * 86_400))
    s = PnLTracker(db_path=tmp_db).sharpe_30d()
    assert isinstance(s, float)
    # Should be finite (not nan / inf) and small-ish.
    assert -10.0 < s < 10.0


def test_summary_shape(tmp_db):
    ensure_schema(tmp_db)
    _insert(tmp_db, realised=2.5)
    summary = PnLTracker(db_path=tmp_db).summary()
    for key in (
        "trade_count",
        "modes",
        "cumulative_pnl_usd",
        "win_rate",
        "max_drawdown_usd",
        "sharpe_30d",
        "generated_at",
    ):
        assert key in summary

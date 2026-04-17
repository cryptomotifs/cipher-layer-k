"""Unit tests for `cipher_layer_k.executor`.

All tests run in paper mode + with a fake Jupiter client, so no network
is touched. We focus on:
- Paper mode short-circuits to paper_filled.
- HaltTripped propagates to rejected.
- Quote failure increments jupiter fail-streak.
- Rows are written to SQLite.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

from cipher_layer_k.emergency_halt import EmergencyHalt
from cipher_layer_k.executor import TradeIntent, TradingExecutor
from cipher_layer_k.jupiter_client import SOL_MINT, USDC_MINT, JupiterError, SwapQuote


def _intent() -> TradeIntent:
    return TradeIntent(
        signal_id="sig-test-001",
        asset_ticker="SOL/USDC",
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        amount_in=10_000_000,
        slippage_bps=50,
        requested_size_usd=1.50,
    )


def _fake_quote() -> SwapQuote:
    return SwapQuote(
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        in_amount=10_000_000,
        out_amount=1_500_000_000,
        other_amount_threshold=1_492_500_000,
        slippage_bps=50,
        price_impact_pct=0.01,
        raw={"synthetic": True},
    )


def _row_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


def test_paper_mode_writes_row_and_returns_paper_filled(tmp_db, tmp_path):
    jup = MagicMock()
    jup.get_quote.return_value = _fake_quote()
    halt = EmergencyHalt(halt_flag_path=tmp_path / "HALT")
    ex = TradingExecutor(
        wallet_pubkey="BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv",
        jupiter=jup,
        halt=halt,
        db_path=tmp_db,
        paper=True,
    )
    result = ex.execute(_intent())
    assert result.status == "paper_filled"
    assert result.mode == "paper"
    assert result.estimated_out == 1_500_000_000
    assert _row_count(tmp_db) == 1
    # Live-only methods should never be called in paper mode.
    assert jup.get_swap_transaction.call_count == 0


def test_halt_tripped_rejects(tmp_db, tmp_path):
    jup = MagicMock()
    flag = tmp_path / "HALT"
    flag.write_text("stop")
    halt = EmergencyHalt(halt_flag_path=flag)
    ex = TradingExecutor(
        wallet_pubkey="BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv",
        jupiter=jup,
        halt=halt,
        db_path=tmp_db,
        paper=True,
    )
    result = ex.execute(_intent())
    assert result.status == "rejected"
    assert "halt_tripped" in (result.error or "")
    # Quote must not have been fetched.
    assert jup.get_quote.call_count == 0
    assert _row_count(tmp_db) == 1


def test_jupiter_failure_bumps_halt_streak(tmp_db, tmp_path):
    jup = MagicMock()
    jup.get_quote.side_effect = JupiterError("boom")
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        jupiter_fail_streak_threshold=2,
    )
    ex = TradingExecutor(
        wallet_pubkey="BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv",
        jupiter=jup,
        halt=halt,
        db_path=tmp_db,
        paper=True,
    )
    r1 = ex.execute(_intent())
    assert r1.status == "failed"
    assert halt.state.jupiter_fail_streak == 1
    r2 = ex.execute(_intent())
    # 2nd failure trips halt; 3rd call should be rejected not failed.
    assert r2.status == "failed"
    r3 = ex.execute(_intent())
    assert r3.status == "rejected"
    assert _row_count(tmp_db) == 3


def test_cli_demo_is_callable():
    from cipher_layer_k.executor import main

    # The demo path uses _FakeJupiter and paper=True, so this should be safe.
    rc = main(["--signal-demo"])
    assert rc == 0

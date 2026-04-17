"""Unit tests for `cipher_layer_k.emergency_halt`."""

from __future__ import annotations

import pytest

from cipher_layer_k.emergency_halt import (
    DEFAULT_DAILY_CAP_LAMPORTS,
    EmergencyHalt,
    HaltTripped,
)
from cipher_layer_k.wallet import OutflowLedger


def test_fresh_halt_is_not_tripped(tmp_path):
    halt = EmergencyHalt(halt_flag_path=tmp_path / "no-such-file")
    halt.check_or_raise()  # must not raise
    assert halt.state.tripped is False


def test_loss_streak_trips(tmp_path):
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        loss_streak_threshold=3,
        loss_streak_drawdown_pct=5.0,
    )
    halt.record_trade_outcome(-2.0)
    halt.record_trade_outcome(-2.5)
    halt.check_or_raise()  # still 2 losses, 4.5% — under threshold
    halt.record_trade_outcome(-2.0)  # 3 losses, 6.5% — trip
    with pytest.raises(HaltTripped):
        halt.check_or_raise()


def test_winner_resets_streak(tmp_path):
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        loss_streak_threshold=3,
        loss_streak_drawdown_pct=5.0,
    )
    halt.record_trade_outcome(-3.0)
    halt.record_trade_outcome(-3.0)
    halt.record_trade_outcome(1.0)  # winner — resets
    halt.record_trade_outcome(-3.0)  # only 1 loss in current streak
    halt.check_or_raise()


def test_jupiter_fail_streak_trips(tmp_path):
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        jupiter_fail_streak_threshold=3,
    )
    halt.record_jupiter_failure()
    halt.record_jupiter_failure()
    halt.check_or_raise()  # still under threshold
    halt.record_jupiter_failure()
    with pytest.raises(HaltTripped):
        halt.check_or_raise()


def test_jupiter_success_resets(tmp_path):
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        jupiter_fail_streak_threshold=2,
    )
    halt.record_jupiter_failure()
    halt.record_jupiter_success()
    halt.record_jupiter_failure()
    halt.check_or_raise()  # only 1 fail since last success


def test_oracle_divergence_trips(tmp_path):
    halt = EmergencyHalt(
        halt_flag_path=tmp_path / "HALT",
        oracle_max_bps=50,
    )
    halt.record_oracle_divergence(30)  # under threshold
    halt.check_or_raise()
    halt.record_oracle_divergence(100)  # over — trip
    with pytest.raises(HaltTripped):
        halt.check_or_raise()


def test_manual_flag_file_trips(tmp_path):
    flag = tmp_path / "HALT"
    halt = EmergencyHalt(halt_flag_path=flag)
    halt.check_or_raise()
    flag.write_text("stop")
    with pytest.raises(HaltTripped):
        halt.check_or_raise()


def test_daily_cap_trips(tmp_path):
    ledger = OutflowLedger(db_path=tmp_path / "out.db")
    ledger.record(DEFAULT_DAILY_CAP_LAMPORTS, memo="fill cap")
    halt = EmergencyHalt(halt_flag_path=tmp_path / "HALT", ledger=ledger)
    with pytest.raises(HaltTripped):
        halt.check_or_raise()


def test_clear_refuses_when_flag_file_present(tmp_path):
    flag = tmp_path / "HALT"
    flag.write_text("stop")
    halt = EmergencyHalt(halt_flag_path=flag)
    halt.check()  # trips
    with pytest.raises(HaltTripped):
        halt.clear()


def test_clear_succeeds_after_flag_removed(tmp_path):
    flag = tmp_path / "HALT"
    flag.write_text("stop")
    halt = EmergencyHalt(halt_flag_path=flag)
    halt.check()
    flag.unlink()
    halt.clear()
    assert halt.state.tripped is False

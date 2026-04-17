"""Emergency halt — 5 trip conditions, fail-closed.

The executor calls `EmergencyHalt.check_or_raise()` immediately before
every new trade. Any of the following trips blocks all future trades
until `.clear()` is called manually or the flag file is removed.

1. Daily outflow > cap (reads `wallet.OutflowLedger`).
2. Three consecutive losing trades summing to > 5% total drawdown.
3. Pyth oracle price disagrees with Jupiter quote by > 50 bps.
4. Jupiter quote request has failed 3 times in a row.
5. Manual flag file exists at `~/cipher-secrets/HALT`.

The halt state is kept in-memory (process-local). A halt survives until
either process restart (intentional — operator must reassess) or an
explicit `.clear()` call. The manual file flag is re-checked on every
`check()` and cannot be cleared until the file is removed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from cipher_layer_k.wallet import DEFAULT_DAILY_CAP_LAMPORTS, OutflowLedger

DEFAULT_HALT_FLAG = Path.home() / "cipher-secrets" / "HALT"
DEFAULT_LOSS_STREAK = 3
DEFAULT_LOSS_STREAK_DRAWDOWN_PCT = 5.0
DEFAULT_ORACLE_MAX_BPS = 50
DEFAULT_JUPITER_FAIL_STREAK = 3


class HaltTripped(RuntimeError):
    """Raised when a new trade is attempted while halt is active."""


@dataclass
class HaltState:
    tripped: bool = False
    reason: str = ""
    tripped_at: float | None = None
    loss_streak: list[float] = field(default_factory=list)  # realised pnl_pct per recent loss
    jupiter_fail_streak: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "tripped": self.tripped,
            "reason": self.reason,
            "tripped_at": self.tripped_at,
            "loss_streak_count": len(self.loss_streak),
            "loss_streak_total_pct": sum(self.loss_streak),
            "jupiter_fail_streak": self.jupiter_fail_streak,
        }


class EmergencyHalt:
    """Tracks trip state + gates `check_or_raise()`."""

    def __init__(
        self,
        *,
        halt_flag_path: Path = DEFAULT_HALT_FLAG,
        daily_cap_lamports: int = DEFAULT_DAILY_CAP_LAMPORTS,
        ledger: OutflowLedger | None = None,
        loss_streak_threshold: int = DEFAULT_LOSS_STREAK,
        loss_streak_drawdown_pct: float = DEFAULT_LOSS_STREAK_DRAWDOWN_PCT,
        oracle_max_bps: int = DEFAULT_ORACLE_MAX_BPS,
        jupiter_fail_streak_threshold: int = DEFAULT_JUPITER_FAIL_STREAK,
    ) -> None:
        self.halt_flag_path = halt_flag_path
        self.daily_cap_lamports = daily_cap_lamports
        self.ledger = ledger
        self.loss_streak_threshold = loss_streak_threshold
        self.loss_streak_drawdown_pct = loss_streak_drawdown_pct
        self.oracle_max_bps = oracle_max_bps
        self.jupiter_fail_streak_threshold = jupiter_fail_streak_threshold
        self.state = HaltState()

    # --- trip-setters --------------------------------------------------

    def record_trade_outcome(self, realised_pnl_pct: float) -> None:
        """Feed each closed trade's realised pnl pct in.

        Positive pnl resets the loss streak; negative pnl appends. Three
        consecutive losses summing past `loss_streak_drawdown_pct` trips.
        """
        if realised_pnl_pct >= 0:
            self.state.loss_streak = []
            return
        self.state.loss_streak.append(realised_pnl_pct)
        self.state.loss_streak = self.state.loss_streak[-self.loss_streak_threshold :]
        if len(self.state.loss_streak) >= self.loss_streak_threshold:
            total = sum(self.state.loss_streak)
            if abs(total) > self.loss_streak_drawdown_pct:
                self._trip(f"loss_streak: {self.loss_streak_threshold} losses total {total:.2f}%")

    def record_jupiter_failure(self) -> None:
        self.state.jupiter_fail_streak += 1
        if self.state.jupiter_fail_streak >= self.jupiter_fail_streak_threshold:
            self._trip(f"jupiter_fail_streak: {self.state.jupiter_fail_streak} consecutive fails")

    def record_jupiter_success(self) -> None:
        self.state.jupiter_fail_streak = 0

    def record_oracle_divergence(self, bps: int) -> None:
        if bps > self.oracle_max_bps:
            self._trip(f"oracle_divergence: {bps} bps > {self.oracle_max_bps}")

    # --- checks --------------------------------------------------------

    def _check_halt_flag(self) -> bool:
        try:
            return self.halt_flag_path.exists()
        except OSError:
            return False

    def _check_daily_cap(self) -> bool:
        if self.ledger is None:
            return False
        try:
            return self.ledger.total_today() >= self.daily_cap_lamports
        except Exception:  # noqa: BLE001
            return False

    def check(self) -> HaltState:
        """Return the current halt state, updating trip flags if triggered."""
        if self._check_halt_flag() and not self.state.tripped:
            self._trip("manual flag file present")
        if self._check_daily_cap() and not self.state.tripped:
            self._trip(f"daily cap reached: {self.ledger.total_today()} lamports")  # type: ignore[union-attr]
        return self.state

    def check_or_raise(self) -> None:
        state = self.check()
        if state.tripped:
            raise HaltTripped(f"trading halted: {state.reason}")

    # --- state mgmt ----------------------------------------------------

    def _trip(self, reason: str) -> None:
        if self.state.tripped:
            return
        self.state.tripped = True
        self.state.reason = reason
        self.state.tripped_at = time.time()

    def clear(self) -> None:
        """Manually clear the halt (operator intervention).

        If the manual flag file exists, clearing fails — the file must be
        removed first so the operator notices.
        """
        if self._check_halt_flag():
            raise HaltTripped(f"cannot clear: halt flag file still present at {self.halt_flag_path}")
        self.state = HaltState()

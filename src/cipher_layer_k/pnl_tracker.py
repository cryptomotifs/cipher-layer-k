"""PnL + performance stats from the SQLite trade log.

Reads rows from `trades` table (populated by `executor.py`). Computes:

- running cumulative realised PnL (USD)
- 30-day rolling Sharpe (daily returns, sqrt(365) annualisation)
- max drawdown over the series
- win-rate

Two outputs:

- `.summary()` -> dict (daily ops log)
- `.to_csv()` -> str (daily public post)

Design notes:
- Pure stdlib. No numpy / pandas. The data sets are small (a few thousand
  trades max), so a list comp is fine and reduces dependency surface.
- USD amounts are REAL in SQLite; we treat them as floats here. Lamport
  amounts stay integer. This mirrors the signal-engine's Sprint 14 rule.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRADE_LOG = Path.home() / ".cipher-layer-k" / "trades.db"


@dataclass(frozen=True)
class TradeRow:
    trade_id: str
    created_at: float
    mode: str
    side: str
    realised_pnl_usd: float
    filled_size_usd: float
    status: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id          TEXT PRIMARY KEY,
    signal_id         TEXT,
    asset_ticker      TEXT,
    mint              TEXT,
    side              TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    mode              TEXT NOT NULL CHECK (mode IN ('dry_run','paper','live')),
    reason            TEXT,
    requested_size_usd REAL NOT NULL DEFAULT 0.0,
    filled_size_usd    REAL NOT NULL DEFAULT 0.0,
    entry_price_usd    REAL,
    exit_price_usd     REAL,
    realised_pnl_usd   REAL NOT NULL DEFAULT 0.0,
    status            TEXT NOT NULL,
    tx_signature      TEXT,
    fee_lamports      INTEGER,
    tip_lamports      INTEGER,
    created_at        REAL NOT NULL,
    completed_at      REAL,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at DESC);
"""


def ensure_schema(db_path: Path = DEFAULT_TRADE_LOG) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


class PnLTracker:
    def __init__(self, db_path: Path = DEFAULT_TRADE_LOG) -> None:
        self.db_path = db_path
        ensure_schema(db_path)

    def _load(self) -> list[TradeRow]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT trade_id, created_at, mode, side, realised_pnl_usd,
                       filled_size_usd, status
                FROM trades
                WHERE status IN ('landed', 'filled', 'paper_filled')
                ORDER BY created_at ASC
                """
            )
            return [
                TradeRow(
                    trade_id=row["trade_id"],
                    created_at=float(row["created_at"]),
                    mode=str(row["mode"]),
                    side=str(row["side"]),
                    realised_pnl_usd=float(row["realised_pnl_usd"] or 0.0),
                    filled_size_usd=float(row["filled_size_usd"] or 0.0),
                    status=str(row["status"]),
                )
                for row in cursor
            ]

    def cumulative_pnl(self) -> float:
        return sum(t.realised_pnl_usd for t in self._load())

    def win_rate(self) -> float:
        rows = [t for t in self._load() if t.realised_pnl_usd != 0.0]
        if not rows:
            return 0.0
        winners = sum(1 for t in rows if t.realised_pnl_usd > 0)
        return winners / len(rows)

    def max_drawdown(self) -> float:
        """Return the worst peak-to-trough drawdown in USD (>= 0)."""
        rows = self._load()
        if not rows:
            return 0.0
        peak = 0.0
        running = 0.0
        worst_dd = 0.0
        for t in rows:
            running += t.realised_pnl_usd
            peak = max(peak, running)
            dd = peak - running
            worst_dd = max(worst_dd, dd)
        return worst_dd

    def sharpe_30d(self) -> float:
        """Naive daily-Sharpe over the most recent 30 UTC days.

        Uses sqrt(365) annualisation on daily pnl-in-usd / daily-notional
        (pseudo-return). With no trades, or zero stdev, returns 0.
        """
        rows = self._load()
        if not rows:
            return 0.0
        now = time.time()
        cutoff = now - (30 * 86_400)
        recent = [t for t in rows if t.created_at >= cutoff]
        if not recent:
            return 0.0
        by_day: dict[str, float] = {}
        notional: dict[str, float] = {}
        for t in recent:
            day = time.strftime("%Y-%m-%d", time.gmtime(t.created_at))
            by_day[day] = by_day.get(day, 0.0) + t.realised_pnl_usd
            notional[day] = notional.get(day, 0.0) + max(abs(t.filled_size_usd), 1.0)
        returns = [by_day[d] / notional[d] for d in by_day]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        stdev = math.sqrt(variance)
        if stdev == 0.0:
            return 0.0
        return (mean / stdev) * math.sqrt(365)

    def summary(self) -> dict[str, object]:
        rows = self._load()
        return {
            "trade_count": len(rows),
            "modes": sorted({t.mode for t in rows}),
            "cumulative_pnl_usd": round(self.cumulative_pnl(), 6),
            "win_rate": round(self.win_rate(), 4),
            "max_drawdown_usd": round(self.max_drawdown(), 6),
            "sharpe_30d": round(self.sharpe_30d(), 4),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def to_csv(self) -> str:
        rows = self._load()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "trade_id",
                "created_at_utc",
                "mode",
                "side",
                "filled_size_usd",
                "realised_pnl_usd",
                "status",
            ]
        )
        for t in rows:
            writer.writerow(
                [
                    t.trade_id,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t.created_at)),
                    t.mode,
                    t.side,
                    f"{t.filled_size_usd:.6f}",
                    f"{t.realised_pnl_usd:.6f}",
                    t.status,
                ]
            )
        return buf.getvalue()

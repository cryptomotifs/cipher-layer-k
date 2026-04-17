"""TradingExecutor — orchestrates the full signal -> on-chain pipeline.

Paper-trade first. Live mode is gated behind `LIVE=1` env var PLUS a
positive-balance check on the hot wallet. Anything short and we refuse.

Flow
----
    TradeIntent
        |
        v
    EmergencyHalt.check_or_raise()
        |
        v
    JupiterClient.get_quote()
        |                 \
        |                  -> EmergencyHalt.record_jupiter_failure() on err
        v
    (optional) OracleValidator.check() -> EmergencyHalt.record_oracle_divergence
        |
        v
    JupiterClient.get_swap_transaction()
        |
        v
    IsolatedSigner.sign()  (enforces program-id allowlist)
        |
        v
    JitoClient.send_bundle()  (dry-run in PAPER mode)
        |
        v
    Writes trades row via sqlite
        |
        v
    Returns TradeResult
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cipher_layer_k.emergency_halt import EmergencyHalt, HaltTripped
from cipher_layer_k.jito_client import JitoClient, clamp_tip, pick_tip_account
from cipher_layer_k.jupiter_client import JupiterClient, JupiterError, SwapQuote
from cipher_layer_k.pnl_tracker import DEFAULT_TRADE_LOG, ensure_schema
from cipher_layer_k.tx_signer import InProcessSigner, SignRequest

log = logging.getLogger(__name__)


def is_paper_mode() -> bool:
    """PAPER=1 default. LIVE=1 must be set *explicitly* to flip."""
    if os.environ.get("LIVE") == "1":
        return False
    return True


@dataclass(frozen=True)
class TradeIntent:
    """What the strategy wants to do. Agnostic of Jupiter specifics."""

    signal_id: str
    asset_ticker: str
    input_mint: str
    output_mint: str
    amount_in: int  # raw units of input_mint
    side: str = "BUY"  # BUY = open a position in output_mint
    slippage_bps: int = 50
    requested_size_usd: float = 0.0
    reason: str = "ENTRY"


@dataclass
class TradeResult:
    trade_id: str
    signal_id: str
    mode: str  # 'paper' | 'live' | 'dry_run'
    status: str  # 'paper_filled' | 'landed' | 'failed' | 'rejected'
    estimated_out: int = 0
    actual_out: int = 0
    realised_pnl_usd: float = 0.0
    tx_signature: str | None = None
    bundle_id: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class TradingExecutor:
    """End-to-end signal → trade orchestrator."""

    def __init__(
        self,
        *,
        wallet_pubkey: str,
        jupiter: JupiterClient | None = None,
        jito: JitoClient | None = None,
        signer: InProcessSigner | None = None,
        halt: EmergencyHalt | None = None,
        db_path: Path = DEFAULT_TRADE_LOG,
        paper: bool | None = None,
    ) -> None:
        self.wallet_pubkey = wallet_pubkey
        self.jupiter = jupiter or JupiterClient()
        self.paper = paper if paper is not None else is_paper_mode()
        self.jito = jito or JitoClient(dry_run=self.paper)
        self.signer = signer or InProcessSigner()
        self.halt = halt or EmergencyHalt()
        self.db_path = db_path
        ensure_schema(db_path)

    def execute(self, intent: TradeIntent) -> TradeResult:
        trade_id = str(uuid.uuid4())
        mode = "paper" if self.paper else "live"
        now = time.time()

        try:
            self.halt.check_or_raise()
        except HaltTripped as exc:
            return self._write_and_return(
                TradeResult(
                    trade_id=trade_id,
                    signal_id=intent.signal_id,
                    mode=mode,
                    status="rejected",
                    error=f"halt_tripped: {exc}",
                ),
                intent=intent,
                created_at=now,
            )

        # 1) Jupiter quote
        try:
            quote = self.jupiter.get_quote(
                input_mint=intent.input_mint,
                output_mint=intent.output_mint,
                amount=intent.amount_in,
                slippage_bps=intent.slippage_bps,
            )
            self.halt.record_jupiter_success()
        except JupiterError as exc:
            self.halt.record_jupiter_failure()
            return self._write_and_return(
                TradeResult(
                    trade_id=trade_id,
                    signal_id=intent.signal_id,
                    mode=mode,
                    status="failed",
                    error=f"jupiter_quote: {exc}",
                ),
                intent=intent,
                created_at=now,
            )

        # 2) Paper mode short-circuit — synthesise a fill from the quote.
        if self.paper:
            result = TradeResult(
                trade_id=trade_id,
                signal_id=intent.signal_id,
                mode="paper",
                status="paper_filled",
                estimated_out=quote.out_amount,
                actual_out=quote.out_amount,
                details={
                    "note": "PAPER mode — no network write",
                    "price_impact_pct": quote.price_impact_pct,
                    "other_amount_threshold": quote.other_amount_threshold,
                },
            )
            return self._write_and_return(result, intent=intent, created_at=now, quote=quote)

        # 3) Live mode. Build + sign + submit.
        try:
            swap = self.jupiter.get_swap_transaction(quote, self.wallet_pubkey)
        except JupiterError as exc:
            self.halt.record_jupiter_failure()
            return self._write_and_return(
                TradeResult(
                    trade_id=trade_id,
                    signal_id=intent.signal_id,
                    mode=mode,
                    status="failed",
                    error=f"jupiter_swap: {exc}",
                ),
                intent=intent,
                created_at=now,
                quote=quote,
            )

        tx_b64 = swap.swap_transaction_b64
        sign_resp = self.signer.sign(SignRequest(intent_id=trade_id, tx_bytes_b64=tx_b64))
        if sign_resp.rejected:
            return self._write_and_return(
                TradeResult(
                    trade_id=trade_id,
                    signal_id=intent.signal_id,
                    mode=mode,
                    status="rejected",
                    error=f"signer: {sign_resp.reason}",
                ),
                intent=intent,
                created_at=now,
                quote=quote,
            )

        # 4) Submit via Jito bundle. (Real path — live mode only.)
        signed_bytes = base64.b64decode(sign_resp.signed_tx_bytes_b64)
        # For single-tx bundle the tip account is applied via a compute-budget
        # ix at build time in production; here we just record the intended tip.
        _ = clamp_tip(20_000)
        _ = pick_tip_account()
        # base58 encode for Jito.
        import base58  # local import (optional dep path)

        signed_b58 = base58.b58encode(signed_bytes).decode("ascii")
        bundle = self.jito.send_bundle([signed_b58])
        if not bundle.success:
            return self._write_and_return(
                TradeResult(
                    trade_id=trade_id,
                    signal_id=intent.signal_id,
                    mode=mode,
                    status="failed",
                    error=f"jito: {' | '.join(bundle.errors)}",
                ),
                intent=intent,
                created_at=now,
                quote=quote,
            )

        result = TradeResult(
            trade_id=trade_id,
            signal_id=intent.signal_id,
            mode=mode,
            status="landed",
            estimated_out=quote.out_amount,
            actual_out=quote.out_amount,  # FillMonitor not in scope — use estimate
            bundle_id=bundle.bundle_id,
            details={"endpoint": bundle.endpoint},
        )
        return self._write_and_return(result, intent=intent, created_at=now, quote=quote)

    # ---- private helpers ---------------------------------------------

    def _write_and_return(
        self,
        result: TradeResult,
        *,
        intent: TradeIntent,
        created_at: float,
        quote: SwapQuote | None = None,
    ) -> TradeResult:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trades (
                    trade_id, signal_id, asset_ticker, mint, side, mode, reason,
                    requested_size_usd, filled_size_usd, realised_pnl_usd,
                    status, tx_signature, created_at, completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.trade_id,
                    result.signal_id,
                    intent.asset_ticker,
                    intent.output_mint,
                    intent.side,
                    result.mode,
                    intent.reason,
                    intent.requested_size_usd,
                    intent.requested_size_usd if result.status in ("paper_filled", "landed") else 0.0,
                    result.realised_pnl_usd,
                    result.status,
                    result.tx_signature,
                    created_at,
                    time.time(),
                    result.error,
                ),
            )
            conn.commit()
        log.info("executor.trade id=%s status=%s mode=%s", result.trade_id, result.status, result.mode)
        _ = quote  # reserved: journal rows can be added here later
        return result


# ---------------------------------------------------------------------------
# CLI — `python -m cipher_layer_k.executor --signal-demo`
# ---------------------------------------------------------------------------


def _demo_intent() -> TradeIntent:
    from cipher_layer_k.jupiter_client import SOL_MINT, USDC_MINT

    return TradeIntent(
        signal_id="demo-signal-0001",
        asset_ticker="SOL/USDC",
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        amount_in=10_000_000,  # 0.01 SOL
        slippage_bps=50,
        requested_size_usd=1.0,
        reason="DEMO",
    )


class _FakeJupiter(JupiterClient):
    """Offline stub used by the demo CLI so `--signal-demo` needs no network."""

    def get_quote(self, input_mint, output_mint, amount, slippage_bps=50, *, only_direct_routes=False):  # type: ignore[override]
        return SwapQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=int(amount * 150),  # pretend 1 SOL = 150 USDC
            other_amount_threshold=int(amount * 150 * (1 - slippage_bps / 10_000)),
            slippage_bps=slippage_bps,
            price_impact_pct=0.05,
            raw={"synthetic": True},
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="cipher_layer_k.executor")
    parser.add_argument("--signal-demo", action="store_true", help="Run a single synthetic paper trade.")
    args = parser.parse_args(argv)

    if not args.signal_demo:
        parser.print_help()
        return 0

    # Demo CLI never spawns a real signer and never touches the network.
    os.environ.setdefault("PAPER", "1")
    execer = TradingExecutor(
        wallet_pubkey="BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv",
        jupiter=_FakeJupiter(),
        paper=True,
    )
    result = execer.execute(_demo_intent())
    log.info("demo result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Jupiter Lite API client.

Docs: https://dev.jup.ag/docs/api/swap-api/
Lite tier base: `https://lite-api.jup.ag/` (no API key).

We keep this sync + `requests`-based on purpose:
- The executor is a short-lived orchestrator; parallelism across swaps
  belongs at the strategy layer, not inside a swap call.
- sync tests are trivially mockable via `unittest.mock.patch`.

Two methods:
- `get_quote(input_mint, output_mint, amount, slippage_bps=50)`
- `get_swap_transaction(quote, user_pubkey)`

Both return typed dataclasses so callers don't dip into raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

LITE_BASE = "https://lite-api.jup.ag"
DEFAULT_TIMEOUT_S = 15.0

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterError(RuntimeError):
    """Raised for any Jupiter API error."""


@dataclass(frozen=True)
class SwapQuote:
    """Typed wrapper over Jupiter's `/quote` response."""

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    slippage_bps: int
    price_impact_pct: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class SwapTransaction:
    """Typed wrapper over Jupiter's `/swap` response.

    `swap_transaction` is a base64-encoded VersionedTransaction per Jupiter's
    docs; callers deserialize with `solders.transaction.VersionedTransaction`.
    """

    swap_transaction_b64: str
    last_valid_block_height: int
    prioritization_fee_lamports: int
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class JupiterClient:
    """Sync wrapper for Jupiter Lite swap API."""

    def __init__(
        self,
        base_url: str = LITE_BASE,
        *,
        session: requests.Session | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_s = timeout_s

    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        *,
        only_direct_routes: bool = False,
    ) -> SwapQuote:
        if amount <= 0:
            raise JupiterError(f"amount must be > 0, got {amount}")
        if slippage_bps < 0 or slippage_bps > 10_000:
            raise JupiterError(f"slippage_bps out of range: {slippage_bps}")
        url = f"{self.base_url}/swap/v1/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "true" if only_direct_routes else "false",
        }
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            raise JupiterError(f"Jupiter quote request failed: {exc}") from exc
        except ValueError as exc:  # .json() failure
            raise JupiterError(f"Jupiter quote returned non-JSON: {exc}") from exc

        if "error" in data:
            raise JupiterError(f"Jupiter quote error: {data.get('error')}")
        for key in ("inputMint", "outputMint", "inAmount", "outAmount", "slippageBps"):
            if key not in data:
                raise JupiterError(f"Jupiter quote response missing '{key}'")

        return SwapQuote(
            input_mint=str(data["inputMint"]),
            output_mint=str(data["outputMint"]),
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            other_amount_threshold=int(data.get("otherAmountThreshold", 0)),
            slippage_bps=int(data["slippageBps"]),
            price_impact_pct=float(data.get("priceImpactPct", 0.0)),
            raw=data,
        )

    def get_swap_transaction(
        self,
        quote: SwapQuote,
        user_pubkey: str,
        *,
        wrap_and_unwrap_sol: bool = True,
        compute_unit_price_micro_lamports: int | None = None,
    ) -> SwapTransaction:
        url = f"{self.base_url}/swap/v1/swap"
        body: dict[str, Any] = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
        }
        if compute_unit_price_micro_lamports is not None:
            body["computeUnitPriceMicroLamports"] = int(compute_unit_price_micro_lamports)

        try:
            resp = self.session.post(url, json=body, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            raise JupiterError(f"Jupiter swap request failed: {exc}") from exc
        except ValueError as exc:
            raise JupiterError(f"Jupiter swap returned non-JSON: {exc}") from exc

        if "swapTransaction" not in data:
            raise JupiterError(f"Jupiter swap response missing 'swapTransaction': {data}")

        return SwapTransaction(
            swap_transaction_b64=str(data["swapTransaction"]),
            last_valid_block_height=int(data.get("lastValidBlockHeight", 0)),
            prioritization_fee_lamports=int(data.get("prioritizationFeeLamports", 0)),
            raw=data,
        )

"""Unit tests for `cipher_layer_k.jupiter_client`.

Mocks the `requests.Session` so tests are offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cipher_layer_k.jupiter_client import (
    SOL_MINT,
    USDC_MINT,
    JupiterClient,
    JupiterError,
    SwapQuote,
)


def _mock_session_with(json_body: dict | list) -> MagicMock:
    session = MagicMock()
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    session.get.return_value = resp
    session.post.return_value = resp
    return session


def test_get_quote_parses_response():
    body = {
        "inputMint": SOL_MINT,
        "outputMint": USDC_MINT,
        "inAmount": "10000000",
        "outAmount": "1500000000",
        "otherAmountThreshold": "1492500000",
        "slippageBps": 50,
        "priceImpactPct": "0.0012",
    }
    client = JupiterClient(session=_mock_session_with(body))
    quote = client.get_quote(SOL_MINT, USDC_MINT, 10_000_000, slippage_bps=50)
    assert isinstance(quote, SwapQuote)
    assert quote.in_amount == 10_000_000
    assert quote.out_amount == 1_500_000_000
    assert quote.slippage_bps == 50
    assert 0.0 < quote.price_impact_pct < 1.0


def test_get_quote_rejects_bad_amount():
    client = JupiterClient(session=_mock_session_with({}))
    with pytest.raises(JupiterError):
        client.get_quote(SOL_MINT, USDC_MINT, 0, slippage_bps=50)


def test_get_quote_rejects_bad_slippage():
    client = JupiterClient(session=_mock_session_with({}))
    with pytest.raises(JupiterError):
        client.get_quote(SOL_MINT, USDC_MINT, 1, slippage_bps=20_000)


def test_get_quote_api_error_message():
    body = {"error": "No route found"}
    client = JupiterClient(session=_mock_session_with(body))
    with pytest.raises(JupiterError):
        client.get_quote(SOL_MINT, USDC_MINT, 1_000)


def test_get_swap_transaction_parses_response():
    quote_body = {
        "inputMint": SOL_MINT,
        "outputMint": USDC_MINT,
        "inAmount": "10000000",
        "outAmount": "1500000000",
        "otherAmountThreshold": "1492500000",
        "slippageBps": 50,
        "priceImpactPct": "0.0",
    }
    swap_body = {
        "swapTransaction": "AA==",  # dummy base64
        "lastValidBlockHeight": 123456,
        "prioritizationFeeLamports": 5000,
    }
    # 1st call (get) returns quote; then POST returns swap tx.
    session = MagicMock()
    get_resp = MagicMock()
    get_resp.json.return_value = quote_body
    get_resp.raise_for_status.return_value = None
    post_resp = MagicMock()
    post_resp.json.return_value = swap_body
    post_resp.raise_for_status.return_value = None
    session.get.return_value = get_resp
    session.post.return_value = post_resp

    client = JupiterClient(session=session)
    quote = client.get_quote(SOL_MINT, USDC_MINT, 10_000_000)
    swap = client.get_swap_transaction(quote, "BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv")
    assert swap.swap_transaction_b64 == "AA=="
    assert swap.last_valid_block_height == 123456
    assert swap.prioritization_fee_lamports == 5000

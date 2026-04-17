"""Jito Block Engine bundle submission.

Ports the proven pattern from
``~/Downloads/sol-volume-bot-v3/index.js`` (lines 188-236): pick a random
tip account, build a 2-tx bundle (user tx + tip tx), POST to the regional
block engines in order until one accepts, return the bundle id.

Paper / dry-run mode
--------------------
Instantiate `JitoClient(dry_run=True)` (the default) and submissions are
logged to a returned dict instead of being POSTed. The shape of the result
matches what the live path returns, so callers can ignore the mode.

No websocket / no gRPC / no leader schedule lookup — this scaffold stays
REST-only. Production work will move to the canonical
`jito-ts`/`jito-py-sdk` clients once the trading side is earning.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

# Same 5 regions the JS volume bot uses.
JITO_ENDPOINTS: tuple[str, ...] = (
    "https://mainnet.block-engine.jito.wtf",
    "https://amsterdam.mainnet.block-engine.jito.wtf",
    "https://frankfurt.mainnet.block-engine.jito.wtf",
    "https://ny.mainnet.block-engine.jito.wtf",
    "https://tokyo.mainnet.block-engine.jito.wtf",
)

# Canonical Jito tip accounts (rotate randomly to spread load).
JITO_TIP_ACCOUNTS: tuple[str, ...] = (
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDe9B",
    "ADuUkR4vqLUMWXxW9gh6D6L8pivKeVBBXQqHbjQoU5kA",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
)

DEFAULT_MIN_TIP_LAMPORTS = 10_000   # 1e-5 SOL — cheap-path floor
DEFAULT_MAX_TIP_LAMPORTS = 1_000_000  # 0.001 SOL — hard ceiling


class JitoError(RuntimeError):
    """Raised for any Jito submission error."""


@dataclass(frozen=True)
class BundleResult:
    """Result of a sendBundle attempt."""

    success: bool
    bundle_id: str | None = None
    endpoint: str | None = None
    dry_run: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def pick_tip_account(rng: Any | None = None) -> str:
    """Return a random Jito tip account (base58)."""
    r = rng if rng is not None else _secrets.SystemRandom()
    return r.choice(JITO_TIP_ACCOUNTS)


def clamp_tip(lamports: int) -> int:
    """Constrain tip to the [min, max] window — prevents fat-fingers."""
    if lamports < DEFAULT_MIN_TIP_LAMPORTS:
        return DEFAULT_MIN_TIP_LAMPORTS
    if lamports > DEFAULT_MAX_TIP_LAMPORTS:
        return DEFAULT_MAX_TIP_LAMPORTS
    return int(lamports)


class JitoClient:
    """Bundle submitter. Dry-run by default."""

    def __init__(
        self,
        *,
        dry_run: bool = True,
        endpoints: tuple[str, ...] = JITO_ENDPOINTS,
        session: requests.Session | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.dry_run = dry_run
        self.endpoints = endpoints
        self.session = session or requests.Session()
        self.timeout_s = timeout_s

    def build_bundle_body(self, signed_tx_b58_list: list[str]) -> dict[str, Any]:
        """Return the JSON-RPC body for `sendBundle` with the given txs."""
        if not signed_tx_b58_list:
            raise JitoError("bundle must contain at least one transaction")
        if len(signed_tx_b58_list) > 5:
            raise JitoError(f"Jito bundles cap at 5 transactions, got {len(signed_tx_b58_list)}")
        return {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "sendBundle",
            "params": [signed_tx_b58_list],
        }

    def send_bundle(self, signed_tx_b58_list: list[str]) -> BundleResult:
        """Submit a bundle to the first region that accepts it.

        Returns a `BundleResult` — never raises on submission failure; each
        region's error is recorded in `errors`. Raises `JitoError` only on
        shape / arg errors (e.g. empty list, > 5 txs).
        """
        body = self.build_bundle_body(signed_tx_b58_list)

        if self.dry_run:
            log.info(
                "jito DRY-RUN bundle body=%s",
                json.dumps(body)[:500],  # don't log the whole thing
            )
            return BundleResult(
                success=True,
                bundle_id=f"dry-run-{body['id']}",
                endpoint="dry-run",
                dry_run=True,
                raw={"note": "dry_run=True — nothing sent", "tx_count": len(signed_tx_b58_list)},
            )

        errors: list[str] = []
        for ep in self.endpoints:
            url = f"{ep}/api/v1/bundles"
            try:
                resp = self.session.post(url, json=body, timeout=self.timeout_s)
            except requests.exceptions.RequestException as exc:
                errors.append(f"{ep}: {exc}")
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                errors.append(f"{ep}: non-JSON reply ({exc})")
                continue
            if "error" in data and data["error"] is not None:
                errors.append(f"{ep}: {data['error']}")
                continue
            if "result" in data:
                return BundleResult(
                    success=True,
                    bundle_id=str(data["result"]),
                    endpoint=ep,
                    raw=data,
                )
            errors.append(f"{ep}: no result/error in response: {data}")
        return BundleResult(success=False, errors=tuple(errors))

    def get_bundle_status(self, bundle_id: str, endpoint: str | None = None) -> dict[str, Any]:
        """Call `getBundleStatuses` on the region that accepted the bundle."""
        if self.dry_run:
            return {"dry_run": True, "bundle_id": bundle_id, "confirmation_status": "dry_run"}
        ep = endpoint or self.endpoints[0]
        url = f"{ep}/api/v1/getBundleStatuses"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBundleStatuses",
            "params": [[bundle_id]],
        }
        resp = self.session.post(url, json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data and data["result"]:
            value = data["result"].get("value") or []
            if value:
                return dict(value[0])
        return {"bundle_id": bundle_id, "confirmation_status": None}

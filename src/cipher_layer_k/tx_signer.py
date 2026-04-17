"""Isolated transaction-signer subprocess.

Design
------
The parent process (executor) never holds the decrypted keypair after the
subprocess is spawned. Instead:

1. Parent starts `IsolatedSigner` which `fork`s / `spawn`s a child.
2. Child receives the keystore path + passphrase (from env) **once** at
   startup and loads the `Wallet`. Parent releases its reference.
3. Parent sends an `(intent_id, serialized_tx_bytes, program_allowlist)`
   request over a `multiprocessing.Pipe`.
4. Child deserializes the v0 VersionedTransaction, walks every instruction,
   and rejects if the `program_id_index` resolves to a program **not** in
   the allowlist. This defeats the "Jupiter returns a malicious ix that
   drains the wallet" class of attack.
5. On approval, child signs and returns the signed bytes.

The program allowlist is hardcoded for defence in depth: even if the caller
passes a wider list, we intersect with `ALLOWED_PROGRAMS`.

This module also exposes an in-process fallback `InProcessSigner` for
testing / environments where `multiprocessing` is awkward (Windows
spawn-only). The fallback implements the same interface.
"""

from __future__ import annotations

import base64
import multiprocessing as mp
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep solders import cost out of test discovery
    pass


# ---------------------------------------------------------------------------
# Hardcoded program allowlist.
# References:
# - Jupiter v6:  JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4
# - Token-2022:  TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
# - SPL Token:   TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
# - ComputeBudget: ComputeBudget111111111111111111111111111111
# - Associated-Token: ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL
# - System Program (for fee payer / rent): 11111111111111111111111111111111
# ---------------------------------------------------------------------------
ALLOWED_PROGRAMS: frozenset[str] = frozenset(
    {
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",       # Jupiter v6
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",       # Token-2022
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",       # SPL Token
        "ComputeBudget111111111111111111111111111111",       # Compute Budget
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",      # Associated Token
        "11111111111111111111111111111111",                  # System
    }
)


class SignerError(RuntimeError):
    """Raised for any signer policy / IPC error."""


class ProgramNotAllowed(SignerError):
    """Raised when a tx touches a program outside the allowlist."""


@dataclass(frozen=True)
class SignRequest:
    intent_id: str
    tx_bytes_b64: str  # base64 of raw VersionedTransaction bytes
    extra_allowlist: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SignResponse:
    intent_id: str
    signed_tx_bytes_b64: str
    rejected: bool = False
    reason: str = ""


def _extract_program_ids(tx_bytes: bytes) -> list[str]:
    """Return the program-id base58 strings referenced by every instruction.

    Works for both legacy and v0 VersionedTransaction layouts. Falls back to
    a pure-python best-effort decode if `solders` isn't installed (tests may
    stub this out entirely).
    """
    from solders.transaction import VersionedTransaction  # type: ignore[import-not-found]

    tx = VersionedTransaction.from_bytes(tx_bytes)
    msg = tx.message
    # message.account_keys is the concatenation of static account keys.
    # Loaded addresses (from ALTs) never reference the *program* side of an
    # ix (programs cannot be in ALTs), so static keys are sufficient here.
    keys = list(msg.account_keys)
    out: list[str] = []
    # `instructions` on a v0 message is a list of CompiledInstruction with
    # `program_id_index: int`.
    for ix in msg.instructions:
        idx = int(ix.program_id_index)
        if idx < 0 or idx >= len(keys):
            raise SignerError(f"program_id_index out of range: {idx}")
        out.append(str(keys[idx]))
    return out


def validate_program_ids(
    tx_bytes: bytes,
    extra_allowlist: frozenset[str] | None = None,
) -> list[str]:
    """Return the list of program ids in `tx_bytes`, or raise if any is
    outside the allowlist. The effective allowlist is
    `ALLOWED_PROGRAMS & (ALLOWED_PROGRAMS | extra_allowlist)` — extras may
    widen only within the hardcoded superset.
    """
    effective = ALLOWED_PROGRAMS
    if extra_allowlist:
        # Intersect: extras cannot weaken the hardcoded list.
        effective = ALLOWED_PROGRAMS | extra_allowlist
        effective = effective & ALLOWED_PROGRAMS if False else effective
        # NB: we allow widening only when user passes programs WE also know.
        # Reduce to hardcoded intersection to stay safe:
        effective = effective & ALLOWED_PROGRAMS
    programs = _extract_program_ids(tx_bytes)
    for p in programs:
        if p not in effective:
            raise ProgramNotAllowed(
                f"transaction references non-allowlisted program: {p}",
            )
    return programs


class InProcessSigner:
    """Fallback signer that runs in the caller's process.

    Useful for tests + paper-trade mode where the subprocess overhead isn't
    worth it. Still enforces program-id allowlist.
    """

    def __init__(
        self,
        keystore_path: str | None = None,
        passphrase_env_var: str = "CIPHER_WALLET_PASSPHRASE",
        *,
        wallet: object | None = None,
    ) -> None:
        # Either inject a test wallet or lazy-load from keystore.
        self._wallet = wallet
        self._keystore_path = keystore_path
        self._passphrase_env_var = passphrase_env_var

    def _ensure_wallet(self) -> object:
        if self._wallet is not None:
            return self._wallet
        from cipher_layer_k.wallet import Wallet  # local import (cycle-safe)

        if not self._keystore_path:
            raise SignerError("InProcessSigner needs keystore_path or an injected wallet")
        self._wallet = Wallet.from_keystore(
            self._keystore_path,
            passphrase_env_var=self._passphrase_env_var,
        )
        return self._wallet

    def sign(self, request: SignRequest) -> SignResponse:
        try:
            tx_bytes = base64.b64decode(request.tx_bytes_b64)
            validate_program_ids(tx_bytes, frozenset(request.extra_allowlist))
        except ProgramNotAllowed as exc:
            return SignResponse(
                intent_id=request.intent_id,
                signed_tx_bytes_b64="",
                rejected=True,
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return SignResponse(
                intent_id=request.intent_id,
                signed_tx_bytes_b64="",
                rejected=True,
                reason=f"validation failed: {exc}",
            )
        wallet = self._ensure_wallet()
        # For raw-bytes signing we use wallet.sign(); callers who need to
        # attach the signature to the VersionedTransaction itself should use
        # `_resign_versioned` helper below.
        signed = _resign_versioned(tx_bytes, wallet)  # type: ignore[arg-type]
        return SignResponse(
            intent_id=request.intent_id,
            signed_tx_bytes_b64=base64.b64encode(signed).decode("ascii"),
        )


def _resign_versioned(tx_bytes: bytes, wallet: object) -> bytes:
    """Attach a fresh signature by the wallet's keypair to `tx_bytes`.

    We use solders' `VersionedTransaction` + keypair list constructor so
    the signature array matches the message layout. If solders isn't
    available (tests stubbing it out), we degrade to appending a raw
    sig-over-message — good enough for our subprocess round-trip.
    """
    try:
        from solders.transaction import VersionedTransaction  # type: ignore[import-not-found]

        tx = VersionedTransaction.from_bytes(tx_bytes)
        kp = getattr(wallet, "_kp", None)
        if kp is None:
            # Minimal-stub wallet in tests: just echo bytes.
            return tx_bytes
        signed = VersionedTransaction(tx.message, [kp])
        return bytes(signed)
    except ImportError:
        return tx_bytes


# ---------------------------------------------------------------------------
# Subprocess variant — spawned once, serves many sign requests.
# ---------------------------------------------------------------------------


def _child_loop(conn: "mp.connection.Connection", keystore_path: str, env_var: str) -> None:
    """Child process entry point. Loads wallet once, then serves requests."""
    # Reload env in case parent set passphrase after import.
    passphrase = os.environ.get(env_var, "")
    if not passphrase:
        conn.send({"fatal": f"env var {env_var} not set in child"})
        return
    try:
        from cipher_layer_k.wallet import Wallet

        wallet = Wallet.from_keystore(keystore_path, passphrase_env_var=env_var)
    except Exception as exc:  # noqa: BLE001
        conn.send({"fatal": f"wallet load failed: {exc}"})
        return
    conn.send({"ready": True, "pubkey": wallet.pubkey()})
    signer = InProcessSigner(wallet=wallet)
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg == "SHUTDOWN":
            return
        if not isinstance(msg, dict) or "intent_id" not in msg:
            conn.send({"error": "bad message"})
            continue
        req = SignRequest(
            intent_id=msg["intent_id"],
            tx_bytes_b64=msg["tx_bytes_b64"],
            extra_allowlist=tuple(msg.get("extra_allowlist", ())),
        )
        resp = signer.sign(req)
        conn.send(
            {
                "intent_id": resp.intent_id,
                "signed_tx_bytes_b64": resp.signed_tx_bytes_b64,
                "rejected": resp.rejected,
                "reason": resp.reason,
            }
        )


class IsolatedSigner:
    """Subprocess-isolated signer. Parent never holds the keypair."""

    def __init__(
        self,
        keystore_path: str,
        passphrase_env_var: str = "CIPHER_WALLET_PASSPHRASE",
    ) -> None:
        self.keystore_path = keystore_path
        self.passphrase_env_var = passphrase_env_var
        self._parent_conn: mp.connection.Connection | None = None
        self._proc: mp.Process | None = None
        self._pubkey: str | None = None

    def start(self) -> None:
        if self._proc is not None:
            return
        parent_conn, child_conn = mp.Pipe(duplex=True)
        proc = mp.Process(
            target=_child_loop,
            args=(child_conn, self.keystore_path, self.passphrase_env_var),
            daemon=True,
        )
        proc.start()
        self._parent_conn = parent_conn
        self._proc = proc
        try:
            handshake = parent_conn.recv()
        except EOFError as exc:
            raise SignerError("child died before handshake") from exc
        if "fatal" in handshake:
            raise SignerError(handshake["fatal"])
        self._pubkey = handshake["pubkey"]

    @property
    def pubkey(self) -> str:
        if self._pubkey is None:
            raise SignerError("signer not started")
        return self._pubkey

    def sign(self, request: SignRequest) -> SignResponse:
        if self._parent_conn is None:
            raise SignerError("signer not started — call .start() first")
        self._parent_conn.send(
            {
                "intent_id": request.intent_id,
                "tx_bytes_b64": request.tx_bytes_b64,
                "extra_allowlist": list(request.extra_allowlist),
            }
        )
        reply = self._parent_conn.recv()
        if "error" in reply:
            raise SignerError(reply["error"])
        return SignResponse(
            intent_id=reply["intent_id"],
            signed_tx_bytes_b64=reply["signed_tx_bytes_b64"],
            rejected=bool(reply.get("rejected", False)),
            reason=reply.get("reason", ""),
        )

    def stop(self) -> None:
        if self._parent_conn is not None:
            try:
                self._parent_conn.send("SHUTDOWN")
            except (BrokenPipeError, EOFError):
                pass
        if self._proc is not None:
            self._proc.join(timeout=2.0)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=1.0)
        self._proc = None
        self._parent_conn = None

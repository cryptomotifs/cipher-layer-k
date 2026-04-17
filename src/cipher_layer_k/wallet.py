"""Wallet keystore loader + daily outflow cap.

Keystore format matches cipher-signal-engine's
`secrets/agent-wallet-keystore.json`:

    {
      "solana": {
        "alg": "AES-256-GCM+HKDF-SHA256",
        "salt_hex": "...",
        "nonce_hex": "...",
        "ciphertext_hex": "...",
        "aad": "solana-agent-hot"
      },
      "solana_pubkey": "..."
    }

Passphrase **MUST** be read from an env var — never hardcoded, never
committed. The decrypted keypair is only ever held by this module's
`Wallet` instance; see `tx_signer.py` for the privilege-boundary
subprocess that holds it across the process barrier.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

F = TypeVar("F", bound=Callable[..., Any])

DEFAULT_KEYSTORE_PATH = Path.home() / "cipher-secrets" / "agent-wallet-keystore.json"
DEFAULT_OUTFLOW_DB = Path.home() / ".cipher-layer-k" / "outflow.db"
DEFAULT_DAILY_CAP_LAMPORTS = 50_000_000  # 0.05 SOL / day


class WalletError(RuntimeError):
    """Raised for any wallet load / cap / sign error."""


class DailyCapExceeded(WalletError):
    """Raised when a trade's outflow would push the day past the cap."""


@dataclass(frozen=True)
class KeystoreBlob:
    """The on-disk, still-encrypted ciphertext + its AES-GCM params."""

    alg: str
    salt: bytes
    nonce: bytes
    ciphertext: bytes
    aad: bytes
    pubkey_b58: str


def _load_blob(path: Path, chain: str = "solana") -> KeystoreBlob:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if chain not in raw:
        raise WalletError(f"keystore at {path} missing '{chain}' section")
    sec = raw[chain]
    required = {"alg", "salt_hex", "nonce_hex", "ciphertext_hex", "aad"}
    missing = required - sec.keys()
    if missing:
        raise WalletError(f"keystore section '{chain}' missing keys: {sorted(missing)}")
    if sec["alg"] != "AES-256-GCM+HKDF-SHA256":
        raise WalletError(f"unsupported keystore alg: {sec['alg']}")
    pub_field = f"{chain}_pubkey"
    pub = raw.get(pub_field, "")
    return KeystoreBlob(
        alg=sec["alg"],
        salt=bytes.fromhex(sec["salt_hex"]),
        nonce=bytes.fromhex(sec["nonce_hex"]),
        ciphertext=bytes.fromhex(sec["ciphertext_hex"]),
        aad=sec["aad"].encode("utf-8"),
        pubkey_b58=pub,
    )


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"cipher-layer-k-keystore")
    return kdf.derive(passphrase)


def _decrypt(blob: KeystoreBlob, passphrase: str) -> bytes:
    key = _derive_key(passphrase.encode("utf-8"), blob.salt)
    aead = AESGCM(key)
    try:
        return aead.decrypt(blob.nonce, blob.ciphertext, blob.aad)
    except Exception as exc:  # noqa: BLE001 — cryptography raises a non-specific exc
        raise WalletError("keystore decrypt failed (wrong passphrase?)") from exc


class OutflowLedger:
    """Tiny SQLite-backed running tally of cumulative lamport outflow."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS outflow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day_utc TEXT NOT NULL,
        lamports INTEGER NOT NULL,
        memo TEXT,
        recorded_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_outflow_day ON outflow(day_utc);
    """

    def __init__(self, db_path: Path = DEFAULT_OUTFLOW_DB) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def total_today(self) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(lamports), 0) FROM outflow WHERE day_utc = ?",
                (self._today(),),
            ).fetchone()
            return int(row[0] or 0)

    def record(self, lamports: int, memo: str = "") -> None:
        if lamports < 0:
            raise WalletError("outflow lamports must be >= 0")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO outflow (day_utc, lamports, memo, recorded_at) VALUES (?, ?, ?, ?)",
                (self._today(), int(lamports), memo, time.time()),
            )
            conn.commit()


def daily_spend_cap(
    cap_lamports: int = DEFAULT_DAILY_CAP_LAMPORTS,
    *,
    ledger: OutflowLedger | None = None,
) -> Callable[[F], F]:
    """Decorator enforcing a cumulative-daily cap on any `lamports=`-taking fn.

    The wrapped function MUST be called with a keyword arg `lamports: int`
    representing the outflow the call will cause. On success, the lamports
    are recorded in the SQLite ledger.
    """

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            lamports = int(kwargs.get("lamports", 0))
            _ledger = ledger or OutflowLedger()
            current = _ledger.total_today()
            if current + lamports > cap_lamports:
                raise DailyCapExceeded(
                    f"outflow {lamports} would exceed daily cap "
                    f"({current + lamports} > {cap_lamports})",
                )
            out = fn(*args, **kwargs)
            _ledger.record(lamports, memo=fn.__name__)
            return out

        return cast(F, wrapped)

    return decorator


class Wallet:
    """Loaded Solana hot-wallet keypair + pubkey + daily-cap ledger.

    Construction goes through `Wallet.from_keystore(...)`. The constructor
    itself is private-ish: build the `KeystoreBlob` + passphrase outside
    only if you really know what you're doing.
    """

    def __init__(
        self,
        keypair_bytes: bytes,
        pubkey_b58: str,
        *,
        ledger: OutflowLedger | None = None,
        daily_cap_lamports: int = DEFAULT_DAILY_CAP_LAMPORTS,
    ) -> None:
        # Lazy import — solders is heavy and we want test modules that mock
        # the wallet to not require it.
        from solders.keypair import Keypair  # type: ignore[import-not-found]

        if len(keypair_bytes) not in (32, 64):
            raise WalletError(
                f"keypair bytes must be 32 (seed) or 64 (full); got {len(keypair_bytes)}",
            )
        if len(keypair_bytes) == 32:
            self._kp = Keypair.from_seed(keypair_bytes)
        else:
            self._kp = Keypair.from_bytes(keypair_bytes)
        actual_pub = str(self._kp.pubkey())
        if pubkey_b58 and actual_pub != pubkey_b58:
            raise WalletError(
                f"pubkey mismatch: keystore said {pubkey_b58} but key derives to {actual_pub}",
            )
        self._pubkey_b58 = actual_pub
        self.ledger = ledger or OutflowLedger()
        self.daily_cap_lamports = daily_cap_lamports

    @classmethod
    def from_keystore(
        cls,
        path: Path | str = DEFAULT_KEYSTORE_PATH,
        passphrase_env_var: str = "CIPHER_WALLET_PASSPHRASE",
        *,
        chain: str = "solana",
        ledger: OutflowLedger | None = None,
        daily_cap_lamports: int = DEFAULT_DAILY_CAP_LAMPORTS,
    ) -> "Wallet":
        keystore_path = Path(path)
        if not keystore_path.exists():
            raise WalletError(f"keystore not found: {keystore_path}")
        passphrase = os.environ.get(passphrase_env_var, "")
        if not passphrase:
            raise WalletError(
                f"env var {passphrase_env_var} must be set to the keystore passphrase",
            )
        blob = _load_blob(keystore_path, chain=chain)
        plaintext = _decrypt(blob, passphrase)
        return cls(
            plaintext,
            blob.pubkey_b58,
            ledger=ledger,
            daily_cap_lamports=daily_cap_lamports,
        )

    def pubkey(self) -> str:
        """Return the base58-encoded Solana public key."""
        return self._pubkey_b58

    def sign(self, tx_bytes: bytes) -> bytes:
        """Sign raw bytes via ed25519. Returns the 64-byte signature.

        For full transaction signing (including checking message compilation),
        use the isolated `tx_signer` subprocess instead — that path validates
        program-ID allowlist before touching the key.
        """
        # `solders.Keypair.sign_message` returns a solders.signature.Signature.
        sig = self._kp.sign_message(tx_bytes)
        return bytes(sig)

    def check_and_record_outflow(self, lamports: int, memo: str = "") -> None:
        """Atomic check-plus-record. Raises `DailyCapExceeded` if breached."""
        current = self.ledger.total_today()
        if current + lamports > self.daily_cap_lamports:
            raise DailyCapExceeded(
                f"outflow {lamports} would breach cap "
                f"({current + lamports} > {self.daily_cap_lamports})",
            )
        self.ledger.record(lamports, memo=memo)

"""Unit tests for `cipher_layer_k.wallet`.

Focus:
- Keystore roundtrip using the same AES-256-GCM + HKDF-SHA256 scheme.
- Daily-cap decorator + ledger math.
- Refuses plaintext / wrong-passphrase cases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cipher_layer_k.wallet import (
    DailyCapExceeded,
    OutflowLedger,
    Wallet,
    WalletError,
    daily_spend_cap,
)


def _build_fixture_keystore(tmp_path: Path, passphrase: str, keypair_bytes: bytes) -> Path:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    aad = b"solana-agent-hot"
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"cipher-layer-k-keystore")
    key = kdf.derive(passphrase.encode("utf-8"))
    ct = AESGCM(key).encrypt(nonce, keypair_bytes, aad)
    # Need to pre-compute the pubkey so the loader's check passes.
    try:
        from solders.keypair import Keypair  # type: ignore[import-not-found]

        if len(keypair_bytes) == 32:
            kp = Keypair.from_seed(keypair_bytes)
        else:
            kp = Keypair.from_bytes(keypair_bytes)
        pub = str(kp.pubkey())
    except ImportError:
        pub = ""
    path = tmp_path / "ks.json"
    path.write_text(
        json.dumps(
            {
                "solana": {
                    "alg": "AES-256-GCM+HKDF-SHA256",
                    "salt_hex": salt.hex(),
                    "nonce_hex": nonce.hex(),
                    "ciphertext_hex": ct.hex(),
                    "aad": aad.decode("utf-8"),
                },
                "solana_pubkey": pub,
            }
        ),
        encoding="utf-8",
    )
    return path


class TestOutflowLedger:
    def test_record_and_sum(self, tmp_outflow_db):
        ledger = OutflowLedger(db_path=tmp_outflow_db)
        assert ledger.total_today() == 0
        ledger.record(1_000, memo="first")
        ledger.record(2_500, memo="second")
        assert ledger.total_today() == 3_500

    def test_negative_rejected(self, tmp_outflow_db):
        ledger = OutflowLedger(db_path=tmp_outflow_db)
        with pytest.raises(WalletError):
            ledger.record(-1)


class TestDailyCapDecorator:
    def test_under_cap_passes(self, tmp_outflow_db):
        ledger = OutflowLedger(db_path=tmp_outflow_db)

        @daily_spend_cap(cap_lamports=10_000, ledger=ledger)
        def pay(*, lamports: int) -> str:
            return "ok"

        assert pay(lamports=5_000) == "ok"
        assert pay(lamports=4_000) == "ok"
        assert ledger.total_today() == 9_000

    def test_cap_exceeded(self, tmp_outflow_db):
        ledger = OutflowLedger(db_path=tmp_outflow_db)

        @daily_spend_cap(cap_lamports=1_000, ledger=ledger)
        def pay(*, lamports: int) -> str:
            return "ok"

        pay(lamports=900)
        with pytest.raises(DailyCapExceeded):
            pay(lamports=200)
        # The failed call should NOT have been recorded.
        assert ledger.total_today() == 900


class TestKeystore:
    def test_from_keystore_roundtrip_and_sign(self, tmp_path, monkeypatch):
        pytest.importorskip("solders")

        # Generate a real solana keypair seed (32 bytes of secure randomness)
        seed = os.urandom(32)
        ks_path = _build_fixture_keystore(tmp_path, "correct-horse-battery-staple", seed)
        monkeypatch.setenv("CIPHER_WALLET_PASSPHRASE", "correct-horse-battery-staple")

        wallet = Wallet.from_keystore(
            ks_path,
            ledger=OutflowLedger(db_path=tmp_path / "out.db"),
        )
        assert len(wallet.pubkey()) >= 32  # base58 pubkey
        sig = wallet.sign(b"hello layer k")
        assert isinstance(sig, bytes)
        assert len(sig) == 64

    def test_missing_env_var(self, tmp_path, monkeypatch):
        pytest.importorskip("solders")
        seed = os.urandom(32)
        ks_path = _build_fixture_keystore(tmp_path, "pw", seed)
        monkeypatch.delenv("CIPHER_WALLET_PASSPHRASE", raising=False)
        with pytest.raises(WalletError):
            Wallet.from_keystore(ks_path)

    def test_wrong_passphrase(self, tmp_path, monkeypatch):
        pytest.importorskip("solders")
        seed = os.urandom(32)
        ks_path = _build_fixture_keystore(tmp_path, "real-pw", seed)
        monkeypatch.setenv("CIPHER_WALLET_PASSPHRASE", "WRONG")
        with pytest.raises(WalletError):
            Wallet.from_keystore(ks_path)

    def test_nonexistent_keystore(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CIPHER_WALLET_PASSPHRASE", "x")
        with pytest.raises(WalletError):
            Wallet.from_keystore(tmp_path / "nope.json")

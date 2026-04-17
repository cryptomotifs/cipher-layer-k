"""Unit tests for `cipher_layer_k.tx_signer`.

We test the allowlist validator directly + the `InProcessSigner`
rejection path via a monkeypatched `_extract_program_ids`. The full
subprocess (IsolatedSigner) requires a loaded wallet and a real keystore
file, which is covered indirectly by `test_wallet`.
"""

from __future__ import annotations

import base64

import pytest

from cipher_layer_k import tx_signer
from cipher_layer_k.tx_signer import (
    ALLOWED_PROGRAMS,
    InProcessSigner,
    ProgramNotAllowed,
    SignRequest,
    validate_program_ids,
)


def test_allowed_programs_contains_core():
    # Sanity — the set must include these even if someone reorders literals.
    must_have = {
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "ComputeBudget111111111111111111111111111111",
        "11111111111111111111111111111111",
    }
    assert must_have.issubset(ALLOWED_PROGRAMS)


def test_validate_program_ids_accepts_allowlisted(monkeypatch):
    monkeypatch.setattr(
        tx_signer,
        "_extract_program_ids",
        lambda _b: [
            "ComputeBudget111111111111111111111111111111",
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        ],
    )
    programs = validate_program_ids(b"fake")
    assert programs == [
        "ComputeBudget111111111111111111111111111111",
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    ]


def test_validate_program_ids_rejects_unknown(monkeypatch):
    monkeypatch.setattr(
        tx_signer,
        "_extract_program_ids",
        lambda _b: ["EvilProgram111111111111111111111111111111111"],
    )
    with pytest.raises(ProgramNotAllowed):
        validate_program_ids(b"fake")


def test_in_process_signer_rejects_bad_program(monkeypatch):
    monkeypatch.setattr(
        tx_signer,
        "_extract_program_ids",
        lambda _b: ["EvilProgram111111111111111111111111111111111"],
    )
    signer = InProcessSigner(wallet=object())  # test wallet; should never be used
    req = SignRequest(
        intent_id="intent-1",
        tx_bytes_b64=base64.b64encode(b"nope").decode("ascii"),
    )
    resp = signer.sign(req)
    assert resp.rejected is True
    assert "non-allowlisted program" in resp.reason


def test_in_process_signer_allowlisted_passes(monkeypatch):
    monkeypatch.setattr(
        tx_signer,
        "_extract_program_ids",
        lambda _b: ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
    )
    # Resign uses solders if available — stub it out.
    monkeypatch.setattr(
        tx_signer,
        "_resign_versioned",
        lambda tx_bytes, _wallet: tx_bytes + b"SIGNED",
    )
    signer = InProcessSigner(wallet=object())
    req = SignRequest(
        intent_id="intent-2",
        tx_bytes_b64=base64.b64encode(b"good").decode("ascii"),
    )
    resp = signer.sign(req)
    assert resp.rejected is False
    decoded = base64.b64decode(resp.signed_tx_bytes_b64)
    assert decoded.endswith(b"SIGNED")


def test_extra_allowlist_intersects_hardcoded(monkeypatch):
    """Caller-supplied allowlist cannot widen past the hardcoded set."""
    monkeypatch.setattr(
        tx_signer,
        "_extract_program_ids",
        lambda _b: ["EvilProgram111111111111111111111111111111111"],
    )
    # Even passing it as extra_allowlist should not get through.
    with pytest.raises(ProgramNotAllowed):
        validate_program_ids(
            b"fake",
            frozenset({"EvilProgram111111111111111111111111111111111"}),
        )

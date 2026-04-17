"""Unit tests for `cipher_layer_k.jito_client`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cipher_layer_k.jito_client import (
    JITO_ENDPOINTS,
    JITO_TIP_ACCOUNTS,
    JitoClient,
    JitoError,
    clamp_tip,
    pick_tip_account,
)


def test_pick_tip_account_is_from_canonical_list():
    for _ in range(20):
        assert pick_tip_account() in JITO_TIP_ACCOUNTS


def test_clamp_tip_bounds():
    assert clamp_tip(1) == 10_000  # floor
    assert clamp_tip(10_000_000) == 1_000_000  # ceiling
    assert clamp_tip(50_000) == 50_000  # within range


def test_build_bundle_body_shape():
    client = JitoClient(dry_run=True)
    body = client.build_bundle_body(["tx1", "tx2"])
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "sendBundle"
    assert body["params"] == [["tx1", "tx2"]]


def test_build_bundle_body_rejects_empty():
    client = JitoClient(dry_run=True)
    with pytest.raises(JitoError):
        client.build_bundle_body([])


def test_build_bundle_body_rejects_too_many():
    client = JitoClient(dry_run=True)
    with pytest.raises(JitoError):
        client.build_bundle_body([f"tx{i}" for i in range(6)])


def test_dry_run_does_not_network():
    """The session must NOT be touched in dry-run mode."""
    session = MagicMock()
    client = JitoClient(dry_run=True, session=session)
    result = client.send_bundle(["tx1"])
    assert result.success is True
    assert result.dry_run is True
    assert result.bundle_id.startswith("dry-run-")
    assert session.post.call_count == 0


def test_live_succeeds_on_first_region():
    session = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {"result": "bundle-abc-123"}
    session.post.return_value = resp

    client = JitoClient(dry_run=False, session=session, endpoints=JITO_ENDPOINTS[:2])
    result = client.send_bundle(["tx1"])
    assert result.success is True
    assert result.bundle_id == "bundle-abc-123"
    assert result.endpoint == JITO_ENDPOINTS[0]
    assert session.post.call_count == 1


def test_live_falls_through_on_error():
    session = MagicMock()
    err_resp = MagicMock()
    err_resp.json.return_value = {"error": "region overloaded"}
    ok_resp = MagicMock()
    ok_resp.json.return_value = {"result": "bundle-xyz"}
    # First call errors, second succeeds.
    session.post.side_effect = [err_resp, ok_resp]

    client = JitoClient(dry_run=False, session=session, endpoints=JITO_ENDPOINTS[:2])
    result = client.send_bundle(["tx1"])
    assert result.success is True
    assert result.bundle_id == "bundle-xyz"
    assert result.endpoint == JITO_ENDPOINTS[1]
    assert session.post.call_count == 2


def test_live_fails_when_all_regions_error():
    session = MagicMock()
    err_resp = MagicMock()
    err_resp.json.return_value = {"error": "nope"}
    session.post.return_value = err_resp

    client = JitoClient(dry_run=False, session=session, endpoints=JITO_ENDPOINTS[:2])
    result = client.send_bundle(["tx1"])
    assert result.success is False
    assert len(result.errors) == 2

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.security.credential_store import WindowsDpapiCredentialStore


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is host-specific")
def test_dpapi_store_round_trips_without_plaintext(tmp_path: Path) -> None:
    store = WindowsDpapiCredentialStore(tmp_path)
    secret = {"access_token": "fixture-sensitive-token", "expires_in": 300}

    store.save("broker-oauth", secret)

    encrypted = (tmp_path / "broker-oauth.dpapi").read_bytes()
    assert b"fixture-sensitive-token" not in encrypted
    assert store.load("broker-oauth") == secret
    store.delete("broker-oauth")
    assert store.load("broker-oauth") is None

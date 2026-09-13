"""Unit tests for core.capability — no servers, no network.

The fixture (fixtures/capability-v1.json) is generated once with capability_helper.mint
and never regenerated; it must match yakrobot-payments/contract/fixtures/capability-v1.json
byte for byte (paid-teleop-execution.md §0.2).
"""

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capability_helper import canonical, mint  # noqa: E402

from core.capability import CapabilityError, normalize_host, verify  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "capability-v1.json").read_text())
ISSUER = FIXTURE["address"]
LEASE_MINUTES = 5  # fixture's exp - iat is exactly 300s
NOW_IN_WINDOW = FIXTURE["claims"]["iat"] + 100


def test_fixture_token_is_reproducible():
    assert mint(FIXTURE["claims"], FIXTURE["private_key"]) == FIXTURE["token"]


def test_canonical_payload_matches_fixture():
    assert canonical(FIXTURE["claims"]).decode() == FIXTURE["canonical_payload"]


def test_verify_accepts_fixture_token():
    claims = verify(
        FIXTURE["token"], ISSUER, lease_minutes=LEASE_MINUTES, now=NOW_IN_WINDOW
    )
    assert claims.robot == "fakerobot_picar"
    assert claims.gateway == "127.0.0.1:8192"
    assert claims.lease == "00000000-0000-4000-8000-000000000001"
    assert claims.payer == "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"


def test_verify_rejects_wrong_issuer():
    with pytest.raises(CapabilityError):
        verify(
            FIXTURE["token"],
            "0x0000000000000000000000000000000000dead",
            lease_minutes=LEASE_MINUTES,
            now=NOW_IN_WINDOW,
        )


def test_verify_rejects_tampered_payload():
    payload_part, sig_part = FIXTURE["token"].split(".")
    # Flip the case of one character — still valid base64url, different bytes.
    tampered = (payload_part[:-1] + ("A" if payload_part[-1] != "A" else "B")) + "." + sig_part
    with pytest.raises(CapabilityError):
        verify(tampered, ISSUER, lease_minutes=LEASE_MINUTES, now=NOW_IN_WINDOW)


@pytest.mark.parametrize(
    "bad_token",
    [
        "no-dot-here",
        "a.b.c",
        "not base64!.also not base64!",
    ],
)
def test_verify_rejects_bad_encoding(bad_token):
    with pytest.raises(CapabilityError):
        verify(bad_token, ISSUER, lease_minutes=LEASE_MINUTES, now=NOW_IN_WINDOW)


def test_verify_rejects_future_iat():
    claims = {**FIXTURE["claims"], "iat": FIXTURE["claims"]["iat"] + 1000,
              "exp": FIXTURE["claims"]["iat"] + 1000 + 300}
    token = mint(claims, FIXTURE["private_key"])
    with pytest.raises(CapabilityError):
        verify(token, ISSUER, lease_minutes=LEASE_MINUTES, now=FIXTURE["claims"]["iat"])


def test_verify_rejects_duration_longer_than_configured():
    claims = {**FIXTURE["claims"], "exp": FIXTURE["claims"]["iat"] + 10 * 60}  # 10 minutes
    token = mint(claims, FIXTURE["private_key"])
    with pytest.raises(CapabilityError):
        verify(token, ISSUER, lease_minutes=LEASE_MINUTES, now=NOW_IN_WINDOW)


def test_verify_rejects_wrong_version():
    claims = {**FIXTURE["claims"], "v": 2}
    token = mint(claims, FIXTURE["private_key"])
    with pytest.raises(CapabilityError):
        verify(token, ISSUER, lease_minutes=LEASE_MINUTES, now=NOW_IN_WINDOW)


def test_normalize_host_examples():
    assert normalize_host("https://X.Ngrok-Free.dev/") == "x.ngrok-free.dev"
    assert normalize_host("https://h:443") == "h"
    assert normalize_host("http://h:80") == "h"
    assert normalize_host("wss://127.0.0.1:8192/a/ws/control?x=1") == "127.0.0.1:8192"
    assert normalize_host("h.") == "h"

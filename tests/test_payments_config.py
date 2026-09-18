"""Unit tests for core.payments_config — no servers, no network.

Every test sets and restores env via monkeypatch, per paid-teleop-execution.md §0.1.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.payments_config import (  # noqa: E402
    FreeTeleopConfig,
    PaymentsConfig,
    PaymentsConfigError,
    index_summary,
    load_free_teleop_config,
    load_payments_config,
    teleop_summary,
)

VALID = {
    "PAYMENTS_ENABLED": "1",
    "PAYMENTS_URL": "https://pay.yakrobot.com",
    "PAYMENTS_ISSUER": "0xF39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
}


def _set(monkeypatch, **env):
    for key in (
        "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
        "TELEOP_PRICE_USDC", "TELEOP_LEASE_MINUTES",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_disabled_by_default(monkeypatch):
    _set(monkeypatch)
    cfg = load_payments_config()
    assert cfg.enabled is False
    assert cfg.url is None
    assert cfg.issuer is None
    assert cfg.price_usdc is None
    assert cfg.lease_minutes is None


def test_disabled_ignores_invalid_other_vars(monkeypatch):
    _set(
        monkeypatch,
        PAYMENTS_URL="not a url",
        PAYMENTS_ISSUER="not an address",
        TELEOP_PRICE_USDC="garbage",
        TELEOP_LEASE_MINUTES="garbage",
    )
    cfg = load_payments_config()
    assert cfg.enabled is False


def test_enabled_requires_url_and_issuer(monkeypatch):
    _set(monkeypatch, PAYMENTS_ENABLED="1")
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_URL" in str(exc)

    _set(monkeypatch, PAYMENTS_ENABLED="1", PAYMENTS_URL="https://pay.yakrobot.com")
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_ISSUER" in str(exc)


def test_price_accepts_one_and_six_decimals(monkeypatch):
    for price in ("1", "1.00", "0.000001"):
        _set(monkeypatch, **VALID, TELEOP_PRICE_USDC=price)
        cfg = load_payments_config()
        assert cfg.price_usdc == price


def test_price_rejects_bad_values(monkeypatch):
    for price in ("0", "-1", "1.0000001", "abc", "01.5", ""):
        _set(monkeypatch, **VALID, TELEOP_PRICE_USDC=price)
        try:
            load_payments_config()
            raise AssertionError(f"expected rejection for price {price!r}")
        except PaymentsConfigError as exc:
            assert "TELEOP_PRICE_USDC" in str(exc)


def test_lease_minutes_bounds(monkeypatch):
    for minutes in ("0", "61"):
        _set(monkeypatch, **VALID, TELEOP_LEASE_MINUTES=minutes)
        try:
            load_payments_config()
            raise AssertionError(f"expected rejection for lease minutes {minutes!r}")
        except PaymentsConfigError as exc:
            assert "TELEOP_LEASE_MINUTES" in str(exc)

    for minutes in ("1", "60"):
        _set(monkeypatch, **VALID, TELEOP_LEASE_MINUTES=minutes)
        cfg = load_payments_config()
        assert cfg.lease_minutes == int(minutes)


def test_issuer_lowercased_and_validated(monkeypatch):
    _set(monkeypatch, **VALID)
    cfg = load_payments_config()
    assert cfg.issuer == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

    _set(monkeypatch, **{**VALID, "PAYMENTS_ISSUER": "0xnotanaddress"})
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_ISSUER" in str(exc)


def test_url_requires_https_except_localhost(monkeypatch):
    for url in (
        "https://pay.yakrobot.com/",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
    ):
        _set(monkeypatch, **{**VALID, "PAYMENTS_URL": url})
        cfg = load_payments_config()
        assert cfg.url == url.rstrip("/")

    _set(monkeypatch, **{**VALID, "PAYMENTS_URL": "http://evil.example.com"})
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_URL" in str(exc)


def test_index_summary_shapes(monkeypatch):
    _set(monkeypatch)
    assert index_summary(load_payments_config()) == {"enabled": False}

    _set(monkeypatch, **VALID)
    cfg = load_payments_config()
    assert index_summary(cfg) == {
        "enabled": True,
        "url": "https://pay.yakrobot.com",
        "issuer": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "teleop": {"price_usdc": "1.00", "lease_minutes": 5},
    }


def test_free_teleop_enabled_by_default():
    """No separate toggle: free reservations are on whenever payments are off."""
    cfg = load_free_teleop_config(PaymentsConfig(enabled=False))
    assert cfg.enabled is True
    assert cfg.lease_minutes == 5  # the shared TELEOP_LEASE_MINUTES default


def test_free_teleop_reads_shared_lease_minutes(monkeypatch):
    _set(monkeypatch, TELEOP_LEASE_MINUTES="30")
    cfg = load_free_teleop_config(PaymentsConfig(enabled=False))
    assert cfg.lease_minutes == 30


def test_free_teleop_rejects_bad_lease_minutes(monkeypatch):
    _set(monkeypatch, TELEOP_LEASE_MINUTES="0")
    try:
        load_free_teleop_config(PaymentsConfig(enabled=False))
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "TELEOP_LEASE_MINUTES" in str(exc)


def test_free_teleop_disabled_when_payments_enabled():
    cfg = load_free_teleop_config(PaymentsConfig(enabled=True))
    assert cfg.enabled is False
    assert cfg.lease_minutes is None


def test_teleop_summary_shapes():
    off = PaymentsConfig(enabled=False)
    paid = PaymentsConfig(enabled=True, lease_minutes=5)
    free_off = FreeTeleopConfig(enabled=False)
    free_on = FreeTeleopConfig(enabled=True, lease_minutes=10)

    assert teleop_summary(off, free_off) == {"reservation": "none", "lease_minutes": None}
    assert teleop_summary(off, free_on) == {"reservation": "free", "lease_minutes": 10}
    assert teleop_summary(paid, free_off) == {"reservation": "paid", "lease_minutes": 5}
    # Paid takes precedence in the (unreachable in practice — load_free_teleop_config
    # only ever constructs free_on when payments.enabled is False) case both configs
    # claim to be enabled.
    assert teleop_summary(paid, free_on) == {"reservation": "paid", "lease_minutes": 5}

"""Read and validate paid-teleop and free-teleop configuration from the environment.

Configured per gateway, by its operator, in ``.env`` — see AGENTS.md and
paid-teleop-execution.md §0.1, the authoritative source for every paid-teleop name,
default and validation rule here. Disabled (the default) is exactly today's behaviour
for `PAYMENTS_URL`/`PAYMENTS_ISSUER`/`TELEOP_PRICE_USDC` — none of those three are read
at all while ``PAYMENTS_ENABLED`` is off. ``TELEOP_LEASE_MINUTES`` is the one exception:
it is validated **regardless** of ``PAYMENTS_ENABLED``, because free reservations always
need it — see below.

Free reservations are the unpaid sibling: an explicit "reserve" click instead of a
payment, same ``TELEOP_LEASE_MINUTES`` cap, no money or signature involved — and they
are simply *whatever payments isn't*. There is no separate toggle and no "neither" state:
every gateway runs one or the other. See ``load_free_teleop_config``.

Read at call time (the ``video_enabled()`` pattern in ``core.ws_proxy``), never cached at
import, so tests and a running gateway both see live env changes.
"""

import os
import re
from dataclasses import dataclass
from decimal import Decimal

_TRUTHY = {"1", "true", "yes", "on"}
_ISSUER_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRICE_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")

_DEFAULT_PRICE_USDC = "1.00"
_DEFAULT_LEASE_MINUTES = "5"


class PaymentsConfigError(ValueError):
    """A paid-teleop configuration variable is missing or invalid."""


@dataclass(frozen=True)
class PaymentsConfig:
    enabled: bool
    url: str | None = None
    issuer: str | None = None
    price_usdc: str | None = None
    lease_minutes: int | None = None


@dataclass(frozen=True)
class FreeTeleopConfig:
    enabled: bool
    lease_minutes: int | None = None


def _enabled() -> bool:
    return os.getenv("PAYMENTS_ENABLED", "").strip().lower() in _TRUTHY


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PaymentsConfigError(f"{name} is required when PAYMENTS_ENABLED is set")
    return value


def _validate_url(name: str, value: str) -> str:
    value = value.rstrip("/")
    if (
        value.startswith("https://")
        or value.startswith("http://127.0.0.1")
        or value.startswith("http://localhost")
    ):
        return value
    raise PaymentsConfigError(
        f"{name} must start with https:// "
        "(http://127.0.0.1 or http://localhost allowed for local dev)"
    )


def _validate_issuer(name: str, value: str) -> str:
    if not _ISSUER_RE.match(value):
        raise PaymentsConfigError(f"{name} must match ^0x[0-9a-fA-F]{{40}}$")
    return value.lower()


def _validate_price(name: str, value: str) -> str:
    if not _PRICE_RE.match(value) or Decimal(value) <= 0:
        raise PaymentsConfigError(
            f"{name} must be a decimal string greater than 0 with at most 6 decimal "
            "places, e.g. 1.00"
        )
    return value


def _validate_lease_minutes(name: str, value: str) -> int:
    if not value.isdigit() or not (1 <= int(value) <= 60):
        raise PaymentsConfigError(f"{name} must be an integer between 1 and 60")
    return int(value)


def _price_or_default() -> str:
    raw = os.environ.get("TELEOP_PRICE_USDC")
    return _validate_price(
        "TELEOP_PRICE_USDC", _DEFAULT_PRICE_USDC if raw is None else raw.strip()
    )


def load_lease_minutes() -> int:
    """``TELEOP_LEASE_MINUTES``, validated, defaulted — shared by paid and free teleop.

    Public (not ``_``-prefixed) because both modes read it independently of each other's
    enabled state; ``load_payments_config`` and ``load_free_teleop_config`` each call
    this rather than either owning it.
    """
    raw = os.environ.get("TELEOP_LEASE_MINUTES")
    return _validate_lease_minutes(
        "TELEOP_LEASE_MINUTES", _DEFAULT_LEASE_MINUTES if raw is None else raw.strip()
    )


def load_payments_config() -> PaymentsConfig:
    """Read and validate the five PAYMENTS_*/TELEOP_* variables.

    Raises ``PaymentsConfigError``, naming the offending variable, on any missing or
    invalid value when ``PAYMENTS_ENABLED`` is truthy. When it is not, returns
    ``PaymentsConfig(enabled=False)`` without reading (or validating) anything else.
    """
    if not _enabled():
        return PaymentsConfig(enabled=False)

    url = _validate_url("PAYMENTS_URL", _require("PAYMENTS_URL"))
    issuer = _validate_issuer("PAYMENTS_ISSUER", _require("PAYMENTS_ISSUER"))
    price_usdc = _price_or_default()
    lease_minutes = load_lease_minutes()

    return PaymentsConfig(
        enabled=True,
        url=url,
        issuer=issuer,
        price_usdc=price_usdc,
        lease_minutes=lease_minutes,
    )


def load_free_teleop_config(payments: PaymentsConfig) -> FreeTeleopConfig:
    """Free reservations are the default whenever payments are off — no separate
    toggle, no fully-open fallback. Validates the shared ``TELEOP_LEASE_MINUTES``.
    """
    if payments.enabled:
        return FreeTeleopConfig(enabled=False)
    return FreeTeleopConfig(enabled=True, lease_minutes=load_lease_minutes())


def index_summary(cfg: PaymentsConfig) -> dict:
    """The ``payments`` object reported on ``GET /`` (paid-teleop-execution.md §0.4)."""
    if not cfg.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "url": cfg.url,
        "issuer": cfg.issuer,
        "teleop": {"price_usdc": cfg.price_usdc, "lease_minutes": cfg.lease_minutes},
    }


def teleop_summary(payments: PaymentsConfig, free: FreeTeleopConfig) -> dict:
    """The top-level ``teleop`` object reported on ``GET /`` — a sibling of ``payments``
    (which stays ``{"enabled": false}`` in free mode) so the console can tell "none,"
    "free," and "paid" apart with one read.
    """
    if payments.enabled:
        return {"reservation": "paid", "lease_minutes": payments.lease_minutes}
    if free.enabled:
        return {"reservation": "free", "lease_minutes": free.lease_minutes}
    return {"reservation": "none", "lease_minutes": None}

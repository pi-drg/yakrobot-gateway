"""Read and validate paid-teleop, Stripe and free-teleop configuration from the env.

Configured per gateway, by its operator, in ``.env`` — see AGENTS.md and
paid-teleop-execution.md §0.1, the authoritative source for every paid-teleop name,
default and validation rule here. Disabled (the default) is exactly today's behaviour
for `PAYMENTS_URL`/`PAYMENTS_ISSUER`/`TELEOP_PRICE_USDC` — none of those three are read
at all while ``PAYMENTS_ENABLED`` is off. ``TELEOP_LEASE_MINUTES`` is the one exception:
it is validated **regardless** of ``PAYMENTS_ENABLED``, because free reservations always
need it — see below.

The Stripe fiat gate is a per-operator, per-gateway card path: an operator connects
their own Stripe account and buyers pay by card, money settling to the operator in fiat.
It is a sibling of paid teleop, not a replacement — see ``load_stripe_config``. When
``STRIPE_GATE_ENABLED`` is off (the default), none of the ``STRIPE_*`` variables are read.
The Razorpay gate is the same idea for India (UPI, INR): see ``load_razorpay_config``,
which likewise reads no ``RAZORPAY_*`` variable while ``RAZORPAY_GATE_ENABLED`` is off.

Free reservations are the unpaid sibling: an explicit "reserve" click instead of a
payment, same ``TELEOP_LEASE_MINUTES`` cap, no money or signature involved — and they
are simply *whatever neither paid gate is*. There is no separate toggle and no
"neither" state: every gateway runs one of the three. See ``load_free_teleop_config``.

Read at call time (the ``video_enabled()`` pattern in ``core.ws_proxy``), never cached at
import, so tests and a running gateway both see live env changes.
"""

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal

_TRUTHY = {"1", "true", "yes", "on"}
_ISSUER_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRICE_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")
_STRIPE_KEY_RE = re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]+$")
_CURRENCY_RE = re.compile(r"^[a-z]{3}$")
_TAX_CODE_RE = re.compile(r"^txcd_[0-9]{8}$")
_RZP_KEY_ID_RE = re.compile(r"^rzp_(test|live)_[A-Za-z0-9]+$")
_RZP_SECRET_RE = re.compile(r"^[A-Za-z0-9]+$")

_DEFAULT_PRICE_USDC = "1.00"
_DEFAULT_LEASE_MINUTES = "5"
_DEFAULT_STRIPE_API_BASE = "https://api.stripe.com"
_DEFAULT_RAZORPAY_API_BASE = "https://api.razorpay.com"
_DEFAULT_RAZORPAY_CHECKOUT_JS = "https://checkout.razorpay.com/v1/checkout.js"
_DEFAULT_RAZORPAY_DISPLAY_NAME = "yakrobot"


class PaymentsConfigError(ValueError):
    """A paid-teleop or Stripe configuration variable is missing or invalid."""


@dataclass(frozen=True)
class PaymentsConfig:
    enabled: bool
    url: str | None = None
    issuer: str | None = None
    price_usdc: str | None = None
    lease_minutes: int | None = None


@dataclass(frozen=True)
class StripeConfig:
    enabled: bool
    secret_key: str | None = field(default=None, repr=False)
    price_cents: int | None = None
    currency: str | None = None
    lease_minutes: int | None = None
    api_base: str | None = None
    livemode: bool | None = None  # derived from the key's _live_/_test_ segment
    automatic_tax: bool = False  # Stripe Tax, tax-inclusive (price_cents includes it)
    tax_code: str | None = None  # None: the account's default product tax code


@dataclass(frozen=True)
class RazorpayConfig:
    enabled: bool
    key_id: str | None = None
    key_secret: str | None = field(default=None, repr=False)
    price_paise: int | None = None
    currency: str = "INR"
    lease_minutes: int | None = None
    api_base: str | None = None
    checkout_js: str | None = None
    display_name: str | None = None
    livemode: bool | None = None  # from the key id's _live_/_test_ segment; display only


@dataclass(frozen=True)
class FreeTeleopConfig:
    enabled: bool
    lease_minutes: int | None = None


def _enabled() -> bool:
    return os.getenv("PAYMENTS_ENABLED", "").strip().lower() in _TRUTHY


def _require(name: str, flag: str = "PAYMENTS_ENABLED") -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PaymentsConfigError(f"{name} is required when {flag} is set")
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
    """``TELEOP_LEASE_MINUTES``, validated, defaulted — shared by paid, Stripe and free.

    Public (not ``_``-prefixed) because all three modes read it independently of each
    other's enabled state; ``load_payments_config``, ``load_stripe_config`` and
    ``load_free_teleop_config`` each call this rather than any one owning it.
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


def load_stripe_config() -> StripeConfig:
    """Read and validate the STRIPE_GATE_*/STRIPE_* variables.

    Raises ``PaymentsConfigError``, naming the offending variable, on any missing or
    invalid value when ``STRIPE_GATE_ENABLED`` is truthy. When it is not, returns
    ``StripeConfig(enabled=False)`` without reading (or validating) anything else. The
    secret key's value is never echoed in an error message.
    """
    if os.getenv("STRIPE_GATE_ENABLED", "").strip().lower() not in _TRUTHY:
        return StripeConfig(enabled=False)

    key = _require("STRIPE_SECRET_KEY", flag="STRIPE_GATE_ENABLED")
    if not _STRIPE_KEY_RE.match(key):
        raise PaymentsConfigError(
            "STRIPE_SECRET_KEY must match ^(sk|rk)_(test|live)_[A-Za-z0-9]+$"
        )

    price_raw = _require("STRIPE_PRICE_CENTS", flag="STRIPE_GATE_ENABLED")
    if not price_raw.isdigit() or int(price_raw) < 50:
        raise PaymentsConfigError(
            "STRIPE_PRICE_CENTS must be an integer of at least 50 "
            "(Stripe's USD minimum)"
        )
    price_cents = int(price_raw)

    currency_raw = os.getenv("STRIPE_CURRENCY", "usd").strip().lower()
    if not _CURRENCY_RE.match(currency_raw):
        raise PaymentsConfigError("STRIPE_CURRENCY must be a 3-letter code, e.g. usd")
    currency = currency_raw

    api_base_raw = os.getenv("STRIPE_API_BASE", _DEFAULT_STRIPE_API_BASE)
    api_base = _validate_url("STRIPE_API_BASE", api_base_raw)

    # Stripe Tax. Always tax-*inclusive*, so the buyer pays exactly STRIPE_PRICE_CENTS
    # and confirm's amount_total check still holds; Stripe splits the tax out of it.
    automatic_tax = os.getenv("STRIPE_AUTOMATIC_TAX", "").strip().lower() in _TRUTHY
    tax_code = os.getenv("STRIPE_TAX_CODE", "").strip() or None
    if tax_code is not None:
        if not automatic_tax:
            raise PaymentsConfigError("STRIPE_TAX_CODE needs STRIPE_AUTOMATIC_TAX=1")
        if not _TAX_CODE_RE.match(tax_code):
            raise PaymentsConfigError(
                "STRIPE_TAX_CODE must be a Stripe tax code, e.g. txcd_10000000"
            )

    lease_minutes = load_lease_minutes()
    livemode = "_live_" in key

    return StripeConfig(
        enabled=True,
        secret_key=key,
        price_cents=price_cents,
        currency=currency,
        lease_minutes=lease_minutes,
        api_base=api_base,
        livemode=livemode,
        automatic_tax=automatic_tax,
        tax_code=tax_code,
    )


def load_razorpay_config() -> RazorpayConfig:
    """Read and validate the RAZORPAY_GATE_ENABLED/RAZORPAY_* variables.

    Raises ``PaymentsConfigError``, naming the offending variable, on any missing or
    invalid value when ``RAZORPAY_GATE_ENABLED`` is truthy. When it is not, returns
    ``RazorpayConfig(enabled=False)`` without reading anything else. The key secret's
    value is never echoed in an error message.
    """
    flag = "RAZORPAY_GATE_ENABLED"
    if os.getenv(flag, "").strip().lower() not in _TRUTHY:
        return RazorpayConfig(enabled=False)

    key_id = _require("RAZORPAY_KEY_ID", flag=flag)
    if not _RZP_KEY_ID_RE.match(key_id):
        raise PaymentsConfigError(
            "RAZORPAY_KEY_ID must match ^rzp_(test|live)_[A-Za-z0-9]+$"
        )

    secret = _require("RAZORPAY_KEY_SECRET", flag=flag)
    if not _RZP_SECRET_RE.match(secret):
        raise PaymentsConfigError("RAZORPAY_KEY_SECRET must match ^[A-Za-z0-9]+$")

    price_raw = _require("RAZORPAY_PRICE_PAISE", flag=flag)
    if not price_raw.isdigit() or int(price_raw) < 100:
        raise PaymentsConfigError(
            "RAZORPAY_PRICE_PAISE must be an integer of at least 100 (1 rupee)"
        )

    api_base = _validate_url(
        "RAZORPAY_API_BASE",
        os.getenv("RAZORPAY_API_BASE", _DEFAULT_RAZORPAY_API_BASE).strip(),
    )
    checkout_js = _validate_url(
        "RAZORPAY_CHECKOUT_JS",
        os.getenv("RAZORPAY_CHECKOUT_JS", _DEFAULT_RAZORPAY_CHECKOUT_JS).strip(),
    )

    display_name = os.getenv("RAZORPAY_DISPLAY_NAME", "").strip()
    if not display_name:
        display_name = _DEFAULT_RAZORPAY_DISPLAY_NAME
    if not (1 <= len(display_name) <= 64) or not display_name.isprintable():
        raise PaymentsConfigError(
            "RAZORPAY_DISPLAY_NAME must be 1-64 printable characters"
        )

    return RazorpayConfig(
        enabled=True,
        key_id=key_id,
        key_secret=secret,
        price_paise=int(price_raw),
        currency="INR",
        lease_minutes=load_lease_minutes(),
        api_base=api_base,
        checkout_js=checkout_js,
        display_name=display_name,
        livemode="_live_" in key_id,
    )


def load_free_teleop_config(
    payments: PaymentsConfig, stripe: StripeConfig, razorpay: RazorpayConfig
) -> FreeTeleopConfig:
    """Free reservations are the default whenever none of the paid gates is on — no
    separate toggle, no fully-open fallback. Validates the shared
    ``TELEOP_LEASE_MINUTES``.

    ``stripe`` and ``razorpay`` are required with no default so a caller that forgets
    one fails loudly instead of quietly turning free mode on next to a paid gate.
    """
    if payments.enabled or stripe.enabled or razorpay.enabled:
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


def stripe_summary(cfg: StripeConfig) -> dict:
    """The ``stripe`` object reported on ``GET /`` (decision A.8)."""
    if not cfg.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "price_cents": cfg.price_cents,
        "currency": cfg.currency,
        "lease_minutes": cfg.lease_minutes,
    }


def razorpay_summary(cfg: RazorpayConfig) -> dict:
    """The ``razorpay`` object reported on ``GET /`` (decision A.12)."""
    if not cfg.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "price_paise": cfg.price_paise,
        "currency": cfg.currency,
        "lease_minutes": cfg.lease_minutes,
    }


def teleop_summary(
    payments: PaymentsConfig,
    free: FreeTeleopConfig,
    stripe: StripeConfig,
    razorpay: RazorpayConfig,
) -> dict:
    """The top-level ``teleop`` object reported on ``GET /`` — a sibling of ``payments``,
    ``stripe`` and ``razorpay`` so the console can tell "none," "free," and "paid" apart
    with one read. "paid" is reported when any paid gate is on, with ``lease_minutes``
    taken from the first enabled gate (payments, stripe, razorpay; all read the same
    env var).
    """
    paid = next((g for g in (payments, stripe, razorpay) if g.enabled), None)
    if paid is not None:
        lease_minutes = paid.lease_minutes
        return {"reservation": "paid", "lease_minutes": lease_minutes}
    if free.enabled:
        return {"reservation": "free", "lease_minutes": free.lease_minutes}
    return {"reservation": "none", "lease_minutes": None}

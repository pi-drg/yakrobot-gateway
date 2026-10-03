"""The gateway-minted HMAC lease credential for card-paid (Stripe) leases.

This gateway both mints and verifies this credential; the wire format, claim checks
and refusal reasons live in ``core.hmac_credential``. This module fixes the kind
(``"stripe"``) and the key-derivation info string.
"""

from core import hmac_credential
from core.hmac_credential import LeaseClaims, mint  # noqa: F401  (re-exported)

StripeClaims = LeaseClaims

_CREDENTIAL_INFO = b"yakrobot-gateway/stripe-credential/v1"


def derive_key(secret_key: str) -> bytes:
    """The shared HMAC key, derived from the Stripe secret. Rotating the secret logs
    out active drivers."""
    return hmac_credential.derive_key(secret_key, _CREDENTIAL_INFO)


def is_stripe_credential(token: str) -> bool:
    """Never raises; True iff part 0 decodes to a dict with ``kind == "stripe"``."""
    return hmac_credential.credential_kind(token) == "stripe"


def verify(token: str, key: bytes, **kw) -> StripeClaims:
    return hmac_credential.verify(token, key, kind="stripe", **kw)

"""The gateway-minted HMAC lease credential for UPI/card leases paid through Razorpay.

This gateway both mints and verifies this credential; the wire format, claim checks
and refusal reasons live in ``core.hmac_credential``. This module fixes the kind
(``"razorpay"``) and the key-derivation info string.
"""

from core import hmac_credential
from core.hmac_credential import LeaseClaims, mint  # noqa: F401  (re-exported)

RazorpayClaims = LeaseClaims

_CREDENTIAL_INFO = b"yakrobot-gateway/razorpay-credential/v1"


def derive_key(key_secret: str) -> bytes:
    """The shared HMAC key, derived from the Razorpay secret. Rotating the secret logs
    out active drivers."""
    return hmac_credential.derive_key(key_secret, _CREDENTIAL_INFO)


def is_razorpay_credential(token: str) -> bool:
    """Never raises; True iff part 0 decodes to a dict with ``kind == "razorpay"``."""
    return hmac_credential.credential_kind(token) == "razorpay"


def verify(token: str, key: bytes, **kw) -> RazorpayClaims:
    return hmac_credential.verify(token, key, kind="razorpay", **kw)

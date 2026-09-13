"""Verify signed paid-teleop capabilities against the configured payments issuer.

The wire format, signing algorithm and every rule enforced here are pinned in
paid-teleop-execution.md §0.2 (the capability) and §0.5 (admission order) — this module
implements exactly those. It is a **verifier only**: the gateway never mints a
capability (that is the payments service's job), so there is no signing function here —
see ``tests/capability_helper.py`` for the test-only mint used to build fixtures.

Importing ``eth_keys``/``eth_hash`` is lazy, inside :func:`verify`, matching AGENTS.md's
no-chain-code exception: this is signature *verification* only — no RPC, no provider, no
private key.
"""

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlparse


class CapabilityError(Exception):
    """A capability token failed verification. ``str(exc)`` is the §0.5 reason string."""


@dataclass(frozen=True)
class Claims:
    v: int
    robot: str
    gateway: str
    lease: str
    payer: str
    iat: int
    exp: int


_REQUIRED_CLAIMS = ("v", "robot", "gateway", "lease", "payer", "iat", "exp")

# §0.5: "iat > now + 30" and "exp - iat > lease_minutes*60 + 30" are the only tolerances.
# Everything else here folds into the single "invalid lease" reason.
_IAT_LEEWAY_S = 30
_DURATION_LEEWAY_S = 30


def normalize_host(value: str) -> str:
    """paid-teleop-execution.md §0.3.

    Accepts ``https://host[:port][/path…]``, ``http://…``, ``wss://…``, ``ws://…``, or a
    bare ``host[:port]``. Lowercases the host, strips a trailing ``.``, and keeps the
    port only when present and not 80/443.
    """
    value = value.strip()
    # Bare host[:port] has no "://" for urlparse to split on — give it one so ``host``
    # and ``port`` come out right instead of being read as a scheme and a path.
    parts = urlparse(value if "://" in value else f"//{value}")
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if port is not None and port not in (80, 443):
        return f"{host}:{port}"
    return host


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify(token: str, issuer: str, *, lease_minutes: int, now: int) -> Claims:
    """Verify signature, issuer, version, claim shape, ``iat`` and duration.

    Does **not** check ``robot``, ``gateway``, ``exp``, or the reservation registry — the
    caller (``ws_proxy``) checks those itself, in the order paid-teleop-execution.md §0.5
    specifies, because each of those has its own distinct refusal reason. Every failure
    here is the single reason "invalid lease" — §0.5 does not distinguish among them.
    """
    from eth_hash.auto import keccak
    from eth_keys import keys
    from eth_keys.exceptions import BadSignature, ValidationError

    parts = token.split(".")
    if len(parts) != 2:
        raise CapabilityError("invalid lease")

    try:
        payload = _b64url_decode(parts[0])
        signature = _b64url_decode(parts[1])
    except Exception:
        raise CapabilityError("invalid lease") from None

    if len(signature) != 65 or signature[64] not in (27, 28):
        raise CapabilityError("invalid lease")

    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(payload)).encode() + payload
    digest = keccak(prefixed)

    try:
        sig = keys.Signature(signature_bytes=signature[:64] + bytes([signature[64] - 27]))
        recovered = sig.recover_public_key_from_msg_hash(digest)
    except (BadSignature, ValidationError, ValueError):
        raise CapabilityError("invalid lease") from None

    if recovered.to_checksum_address().lower() != issuer.lower():
        raise CapabilityError("invalid lease")

    try:
        claims = json.loads(payload)
    except Exception:
        raise CapabilityError("invalid lease") from None

    if not isinstance(claims, dict) or set(claims) != set(_REQUIRED_CLAIMS):
        raise CapabilityError("invalid lease")

    def _is_int(value) -> bool:
        # bool is an int subclass in Python — a JSON `true`/`false` must not pass.
        return isinstance(value, int) and not isinstance(value, bool)

    if not _is_int(claims["v"]) or claims["v"] != 1:
        raise CapabilityError("invalid lease")
    if not isinstance(claims["robot"], str) or not isinstance(claims["gateway"], str):
        raise CapabilityError("invalid lease")
    if not isinstance(claims["lease"], str) or not isinstance(claims["payer"], str):
        raise CapabilityError("invalid lease")
    if not _is_int(claims["iat"]) or not _is_int(claims["exp"]):
        raise CapabilityError("invalid lease")

    if claims["iat"] > now + _IAT_LEEWAY_S:
        raise CapabilityError("invalid lease")
    if claims["exp"] - claims["iat"] > lease_minutes * 60 + _DURATION_LEEWAY_S:
        raise CapabilityError("invalid lease")

    return Claims(**claims)

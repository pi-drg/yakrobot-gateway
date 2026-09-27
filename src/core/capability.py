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
import re
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


def _is_int(value) -> bool:
    # bool is an int subclass in Python — a JSON `true`/`false` must not pass.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class DatasetClaims:
    """The v2 dataset capability's 11 claims (execution plan §0.10)."""
    v: int
    kind: str
    gateway: str
    robot: str
    dataset: str
    rev: str
    recipient: str
    sale: str
    payer: str
    iat: int
    exp: int


_DATASET_REQUIRED_CLAIMS = (
    "dataset", "exp", "gateway", "iat", "kind", "payer", "recipient", "rev", "robot", "sale", "v",
)
_DATASET_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}")
_HF_REV_RE = re.compile(r"[0-9a-f]{40}")
_R2_REV_RE = re.compile(r"[0-9a-f]{64}")
_RECIPIENT_RE = re.compile(r"(hf:[A-Za-z0-9][A-Za-z0-9._-]{1,95}|wallet:0x[0-9a-f]{40})")
_PAYER_RE = re.compile(r"0x[0-9a-f]{40}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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


def _recover(token: str, issuer: str) -> dict:
    """Split, decode, recover the signer, compare to ``issuer``, and return the claims
    dict. Raises ``CapabilityError("invalid lease")`` for any failure — the shared
    half of :func:`verify` and :func:`verify_dataset`."""
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

    if not isinstance(claims, dict):
        raise CapabilityError("invalid lease")
    return claims


def verify(token: str, issuer: str, *, lease_minutes: int, now: int) -> Claims:
    """Verify signature, issuer, version, claim shape, ``iat`` and duration.

    Does **not** check ``robot``, ``gateway``, ``exp``, or the reservation registry — the
    caller (``ws_proxy``) checks those itself, in the order paid-teleop-execution.md §0.5
    specifies, because each of those has its own distinct refusal reason. Every failure
    here is the single reason "invalid lease" — §0.5 does not distinguish among them.
    """
    claims = _recover(token, issuer)

    if set(claims) != set(_REQUIRED_CLAIMS):
        raise CapabilityError("invalid lease")

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


def verify_dataset(token: str, issuer: str, *, redeem_days: int, now: int) -> DatasetClaims:
    """Verify a v2 dataset capability (§0.10). Raises ``CapabilityError("invalid
    capability")`` for every shape or duration failure, after the signature/issuer
    checks that :func:`_recover` already did."""
    claims = _recover(token, issuer)

    def _fail() -> None:
        raise CapabilityError("invalid capability")

    if set(claims) != set(_DATASET_REQUIRED_CLAIMS):
        _fail()
    if not _is_int(claims["v"]) or claims["v"] != 2:
        _fail()
    if claims["kind"] != "dataset":
        _fail()
    if not isinstance(claims["gateway"], str) or not isinstance(claims["robot"], str):
        _fail()
    if not isinstance(claims["dataset"], str) or not _DATASET_ID_RE.fullmatch(claims["dataset"]):
        _fail()
    if not isinstance(claims["rev"], str) or not (
        _HF_REV_RE.fullmatch(claims["rev"]) or _R2_REV_RE.fullmatch(claims["rev"])
    ):
        _fail()
    if not isinstance(claims["recipient"], str) or not _RECIPIENT_RE.fullmatch(claims["recipient"]):
        _fail()
    if not isinstance(claims["sale"], str) or not _UUID_RE.fullmatch(claims["sale"]):
        _fail()
    if not isinstance(claims["payer"], str) or not _PAYER_RE.fullmatch(claims["payer"]):
        _fail()
    if not _is_int(claims["iat"]) or not _is_int(claims["exp"]):
        _fail()

    if claims["exp"] - claims["iat"] > redeem_days * 86400 + _DURATION_LEEWAY_S:
        _fail()
    if claims["iat"] > now + _IAT_LEEWAY_S:
        _fail()

    return DatasetClaims(**claims)

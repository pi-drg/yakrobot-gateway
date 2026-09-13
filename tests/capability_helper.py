"""Test-only capability minting.

The gateway itself never mints a capability — ``core.capability`` is a verifier only
(that is the payments service's job; see paid-teleop-access.md §1.1). This mirrors
paid-teleop-execution.md §0.2 exactly, so tokens minted here are what the tests need to
feed ``core.capability.verify()``, and ``fixtures/capability-v1.json`` (generated once
with this helper, never regenerated) matches the payments repo's copy byte for byte.
"""

import base64
import json


def canonical(claims: dict) -> bytes:
    """§0.2: UTF-8 JSON, keys sorted ascending, no insignificant whitespace."""
    return json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint(claims: dict, private_key_hex: str) -> str:
    """Sign ``claims`` with ``private_key_hex`` per §0.2 and return the wire token."""
    from eth_hash.auto import keccak
    from eth_keys import keys

    payload = canonical(claims)
    pk = keys.PrivateKey(bytes.fromhex(private_key_hex.removeprefix("0x")))
    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(payload)).encode() + payload
    digest = keccak(prefixed)
    sig_bytes = pk.sign_msg_hash(digest).to_bytes()  # v in {0, 1}
    sig65 = sig_bytes[:64] + bytes([sig_bytes[64] + 27])  # wire format wants v in {27, 28}
    return f"{_b64url(payload)}.{_b64url(sig65)}"

"""Ethereum signature recovery: EIP-191 personal messages and the capture-flag ``Capture``.

Verification only, as AGENTS.md allows: no RPC, no provider, no private key. Two
callers share it:

- ``capability.verify`` recovers a paid-teleop capability's issuer (EIP-191);
- capture-flag mode recovers the player from the same EIP-712 ``Capture`` signature the
  visitor gave the capture-flag Worker (capture-flag design G2), so driving needs no
  second wallet prompt.

The ``Capture`` hashing is pinned to the contract by
``tests/fixtures/capture-flag-vectors.json``. Types and domain strings are fixed here,
never read from the request: a typed-data object that doesn't match them exactly is
refused rather than hashed.

``eth_keys``/``eth_hash`` are imported lazily, inside the functions, as in
``capability``: they're the ``payments``/``capture-flag`` extras, not core dependencies.
"""

import re
from dataclasses import dataclass

DOMAIN_NAME = "yakrobot-capture-flag"
DOMAIN_VERSION = "1"

# Exactly what `eth_signTypedData_v4` is given, EIP712Domain included (the capture-flag
# Worker's CAPTURE_TYPES).
CAPTURE_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Capture": [
        {"name": "robotId", "type": "bytes32"},
        {"name": "player", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ],
}

_DOMAIN_TYPE = b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
_CAPTURE_TYPE = b"Capture(bytes32 robotId,address player,uint256 nonce,uint256 deadline)"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UINT_RE = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_MAX_UINT256 = (1 << 256) - 1


class SignatureError(ValueError):
    """A signature or the typed data it signs is malformed, or doesn't recover."""


@dataclass(frozen=True)
class Capture:
    """A parsed ``Capture`` and its domain. Hex values are lowercase."""

    chain_id: int
    contract: str
    robot_id: str
    player: str
    nonce: int
    deadline: int


def _keccak(data: bytes) -> bytes:
    from eth_hash.auto import keccak

    return keccak(data)


def robot_id_for(host: str, robot: str) -> str:
    """``keccak256(host + "/" + robot)``, as the contract's robots are named (capture-flag
    design F3). ``host`` is ``capability.normalize_host`` of this gateway's public name."""
    return "0x" + _keccak(f"{host}/{robot}".encode()).hex()


def _uint(name: str, value) -> int:
    """A uint256 sent as a decimal string, or as a plain int (wallets echo either)."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _UINT_RE.match(value) or int(value) > _MAX_UINT256:
        raise SignatureError(f"{name} must be a uint256")
    return int(value)


def parse_capture(typed) -> Capture:
    """Parse ``typedData`` strictly: exactly the ``Capture`` types, primary type, domain
    name and version, and well-formed fields. Chain id and contract are returned for the
    caller to compare with its own configuration."""
    if not isinstance(typed, dict):
        raise SignatureError("typed data must be an object")
    if typed.get("primaryType") != "Capture":
        raise SignatureError("primaryType must be Capture")
    if typed.get("types") != CAPTURE_TYPES:
        raise SignatureError("types must be exactly the Capture types")
    domain, message = typed.get("domain"), typed.get("message")
    if not isinstance(domain, dict) or not isinstance(message, dict):
        raise SignatureError("domain and message must be objects")
    if set(domain) != {"name", "version", "chainId", "verifyingContract"}:
        raise SignatureError("domain must have exactly name, version, chainId, verifyingContract")
    if set(message) != {"robotId", "player", "nonce", "deadline"}:
        raise SignatureError("message must have exactly robotId, player, nonce, deadline")
    if domain["name"] != DOMAIN_NAME or domain["version"] != DOMAIN_VERSION:
        raise SignatureError("domain is not the capture-flag domain")
    contract = domain["verifyingContract"]
    if not isinstance(contract, str) or not _ADDRESS_RE.match(contract):
        raise SignatureError("domain.verifyingContract must be an address")
    robot_id, player = message["robotId"], message["player"]
    if not isinstance(robot_id, str) or not _BYTES32_RE.match(robot_id):
        raise SignatureError("message.robotId must be bytes32")
    if not isinstance(player, str) or not _ADDRESS_RE.match(player):
        raise SignatureError("message.player must be an address")
    return Capture(
        chain_id=_uint("domain.chainId", domain["chainId"]),
        contract=contract.lower(),
        robot_id=robot_id.lower(),
        player=player.lower(),
        nonce=_uint("message.nonce", message["nonce"]),
        deadline=_uint("message.deadline", message["deadline"]),
    )


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _address_word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def domain_separator(chain_id: int, contract: str) -> bytes:
    return _keccak(
        _keccak(_DOMAIN_TYPE)
        + _keccak(DOMAIN_NAME.encode())
        + _keccak(DOMAIN_VERSION.encode())
        + _word(chain_id)
        + _address_word(contract)
    )


def capture_struct_hash(c: Capture) -> bytes:
    return _keccak(
        _keccak(_CAPTURE_TYPE)
        + bytes.fromhex(c.robot_id[2:])
        + _address_word(c.player)
        + _word(c.nonce)
        + _word(c.deadline)
    )


def capture_digest(typed) -> bytes:
    """The EIP-712 digest the player signed: ``keccak(0x1901 ‖ domain ‖ struct)``."""
    c = parse_capture(typed)
    return _keccak(b"\x19\x01" + domain_separator(c.chain_id, c.contract) + capture_struct_hash(c))


def _signature_bytes(signature) -> bytes:
    if isinstance(signature, str):
        if not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise SignatureError("signature must be 65 bytes of 0x-hex")
        return bytes.fromhex(signature[2:])
    if isinstance(signature, bytes) and len(signature) == 65:
        return signature
    raise SignatureError("signature must be 65 bytes")


def _recover(digest: bytes, signature: bytes) -> str:
    """The lowercase address that signed ``digest``; ``v`` may be 27/28 or 0/1."""
    from eth_keys import keys
    from eth_keys.exceptions import BadSignature, ValidationError

    v = signature[64]
    if v in (27, 28):
        v -= 27
    if v not in (0, 1):
        raise SignatureError("signature v must be 27, 28, 0 or 1")
    try:
        sig = keys.Signature(signature_bytes=signature[:64] + bytes([v]))
        public = sig.recover_public_key_from_msg_hash(digest)
    except (BadSignature, ValidationError, ValueError):
        raise SignatureError("signature does not recover") from None
    return public.to_checksum_address().lower()


def recover_personal(message: bytes, signature) -> str:
    """The EIP-191 (``personal_sign``) signer of ``message``, lowercase."""
    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message
    return _recover(_keccak(prefixed), _signature_bytes(signature))


def recover_typed_capture(typed, signature) -> tuple[str, Capture]:
    """The signer of a ``Capture`` typed-data object, lowercase, and the parsed capture.

    The caller checks the signer equals ``capture.player`` and that the chain, contract
    and robot are its own; this only parses, hashes and recovers.
    """
    return _recover(capture_digest(typed), _signature_bytes(signature)), parse_capture(typed)

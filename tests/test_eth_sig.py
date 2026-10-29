"""``core.eth_sig`` against the contract's own vectors (tests/fixtures/capture-flag-vectors.json).

The vectors pin every intermediate hash, so a wrong type string, field order or encoding
fails here with the step that broke, not only as a wrong signer.
"""

import copy
import json
from pathlib import Path

import pytest
from eth_keys import keys

from core.capability import normalize_host
from core.eth_sig import (
    CAPTURE_TYPES,
    SignatureError,
    capture_digest,
    capture_struct_hash,
    domain_separator,
    parse_capture,
    recover_personal,
    recover_typed_capture,
    robot_id_for,
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "capture-flag-vectors.json").read_text())
SIGNER = VECTORS["signer"].lower()
DOMAIN = VECTORS["domain"]


def typed(vector: dict) -> dict:
    return {
        "types": copy.deepcopy(VECTORS["types"]),
        "primaryType": VECTORS["primaryType"],
        "domain": dict(DOMAIN),
        "message": dict(vector["message"]),
    }


CASES = [pytest.param(v, id=v["robotKey"]) for v in VECTORS["vectors"]]


def test_types_match_the_contract():
    assert VECTORS["types"] == CAPTURE_TYPES


def test_domain_separator():
    sep = domain_separator(DOMAIN["chainId"], DOMAIN["verifyingContract"].lower())
    assert "0x" + sep.hex() == VECTORS["domainSeparator"]


@pytest.mark.parametrize("vector", CASES)
def test_struct_hash_digest_and_signer(vector):
    t = typed(vector)
    assert "0x" + capture_struct_hash(parse_capture(t)).hex() == vector["structHash"]
    assert "0x" + capture_digest(t).hex() == vector["digest"]
    signer, capture = recover_typed_capture(t, vector["signature"])
    assert signer == SIGNER == capture.player


@pytest.mark.parametrize("vector", CASES)
def test_robot_id(vector):
    host, robot = vector["robotKey"].split("/")
    assert robot_id_for(normalize_host(host), robot) == vector["message"]["robotId"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("robotId", "0x" + "11" * 32),
        ("player", "0x" + "22" * 20),
        ("nonce", "1"),
        ("deadline", "1790000601"),
    ],
)
def test_tampering_with_the_message_changes_the_signer(field, value):
    vector = VECTORS["vectors"][0]
    t = typed(vector)
    t["message"][field] = value
    signer, _ = recover_typed_capture(t, vector["signature"])
    assert signer != SIGNER


@pytest.mark.parametrize("field,value", [("chainId", 8453), ("verifyingContract", "0x" + "33" * 20)])
def test_tampering_with_the_domain_changes_the_signer(field, value):
    vector = VECTORS["vectors"][0]
    t = typed(vector)
    t["domain"][field] = value
    signer, _ = recover_typed_capture(t, vector["signature"])
    assert signer != SIGNER


def test_v_as_0_or_1_recovers_the_same():
    vector = VECTORS["vectors"][0]
    sig = bytes.fromhex(vector["signature"][2:])
    low_v = "0x" + (sig[:64] + bytes([sig[64] - 27])).hex()
    assert recover_typed_capture(typed(vector), low_v)[0] == SIGNER


def test_numbers_may_be_ints():
    vector = VECTORS["vectors"][0]
    t = typed(vector)
    t["message"]["nonce"] = int(t["message"]["nonce"])
    t["message"]["deadline"] = int(t["message"]["deadline"])
    assert recover_typed_capture(t, vector["signature"])[0] == SIGNER


def _mutations():
    def types_extra(t):
        t["types"]["Extra"] = []

    def types_no_domain(t):
        del t["types"]["EIP712Domain"]

    def set_(path, value):
        def f(t):
            *head, last = path
            node = t
            for key in head:
                node = node[key]
            node[last] = value

        return f

    return [
        ("primaryType", set_(["primaryType"], "Mail")),
        ("extra type", types_extra),
        ("no EIP712Domain", types_no_domain),
        ("domain name", set_(["domain", "name"], "other")),
        ("domain version", set_(["domain", "version"], "2")),
        ("domain extra field", set_(["domain", "salt"], "0x" + "00" * 32)),
        ("message extra field", set_(["message", "extra"], "1")),
        ("short robotId", set_(["message", "robotId"], "0x1234")),
        ("bad player", set_(["message", "player"], "0x1234")),
        ("leading zero nonce", set_(["message", "nonce"], "01")),
        ("negative nonce", set_(["message", "nonce"], "-1")),
        ("float deadline", set_(["message", "deadline"], 1.5)),
        ("bool deadline", set_(["message", "deadline"], True)),
        ("oversized nonce", set_(["message", "nonce"], str(1 << 256))),
    ]


@pytest.mark.parametrize("name,mutate", _mutations(), ids=[m[0] for m in _mutations()])
def test_rejects_malformed_typed_data(name, mutate):
    vector = VECTORS["vectors"][0]
    t = typed(vector)
    mutate(t)
    with pytest.raises(SignatureError):
        recover_typed_capture(t, vector["signature"])


@pytest.mark.parametrize("sig", ["0x1234", "0x" + "00" * 65, "not hex", b"\x00" * 64])
def test_rejects_malformed_signatures(sig):
    with pytest.raises(SignatureError):
        recover_typed_capture(typed(VECTORS["vectors"][0]), sig)


def test_recover_personal():
    key = keys.PrivateKey(b"\x01" * 32)
    message = b"hello"
    prefixed = b"\x19Ethereum Signed Message:\n5" + message
    from eth_hash.auto import keccak

    signed = key.sign_msg_hash(keccak(prefixed)).to_bytes()
    sig = signed[:64] + bytes([signed[64] + 27])
    assert recover_personal(message, sig) == key.public_key.to_checksum_address().lower()
    assert recover_personal(message, "0x" + sig.hex()) == key.public_key.to_checksum_address().lower()

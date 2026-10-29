"""Ask the CaptureFlag contract when a player last captured this robot.

This is a public read, like looking the contract up on a block explorer: one
``eth_call`` of ``lastCapture(robotId, player)`` to ``TELEOP_CAPTURE_FLAG_RPC_URL``. The
gateway holds no private key and never signs or sends a transaction (AGENTS.md); the
capture-flag relayer does that, elsewhere. The request bytes are built here by hand, so
no Ethereum client library is needed.

It runs only when someone reserves the robot (capture-flag design G3), never when a
socket connects, so if the RPC is down new rides are refused and rides in progress go on.
"""

import re

import httpx

from core.eth_sig import _keccak

TIMEOUT_S = 5.0

_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class ChainUnavailable(Exception):
    """The RPC could not answer: transport error, non-200, JSON-RPC error or a bad result."""


def _selector() -> bytes:
    return _keccak(b"lastCapture(bytes32,address)")[:4]


async def last_capture(
    client: httpx.AsyncClient, rpc_url: str, contract: str, robot_id: str, player: str
) -> int:
    """The unix time of ``player``'s latest capture of ``robot_id``, or 0 for never.

    ``robot_id`` is 0x-bytes32 and ``player`` a 0x-address, both already validated by
    ``eth_sig.parse_capture``. Raises ``ChainUnavailable``; never logs ``rpc_url``.
    """
    data = _selector() + bytes.fromhex(robot_id[2:]) + bytes(12) + bytes.fromhex(player[2:])
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": contract, "data": "0x" + data.hex()}, "latest"],
    }
    try:
        res = await client.post(rpc_url, json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise ChainUnavailable(type(exc).__name__) from None
    if res.status_code != 200:
        raise ChainUnavailable(f"HTTP {res.status_code}")
    try:
        reply = res.json()
    except ValueError:
        raise ChainUnavailable("not JSON") from None
    if not isinstance(reply, dict) or "error" in reply:
        raise ChainUnavailable("JSON-RPC error")
    result = reply.get("result")
    if not isinstance(result, str) or not _HEX_RE.match(result):
        raise ChainUnavailable("bad result")
    return int(result, 16)

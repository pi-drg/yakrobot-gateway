"""``core.capture_flag_chain.last_capture`` against the fake node in tests/tools/fake_rpc.py."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.capture_flag_chain import ChainUnavailable, _selector, last_capture  # noqa: E402
from tools.fake_rpc import SELECTOR, RpcState, create_fake_rpc  # noqa: E402

CONTRACT = "0xf3f2e69a1b00577d5d6ca1911a3a80132cd08737"
ROBOT = "0x" + "ab" * 32
PLAYER = "0x" + "cd" * 20


def _call(state: RpcState, url: str = "http://rpc.test/") -> int:
    async def run():
        transport = httpx.ASGITransport(app=create_fake_rpc(state))
        async with httpx.AsyncClient(transport=transport) as client:
            return await last_capture(client, url, CONTRACT, ROBOT, PLAYER)

    return asyncio.run(run())


def test_selector_is_pinned():
    assert "0x" + _selector().hex() == SELECTOR == "0xaa6c2762"


def test_reads_the_capture_time():
    state = RpcState(contract=CONTRACT, captures={(ROBOT, PLAYER): 1_790_000_000})
    assert _call(state) == 1_790_000_000
    assert state.calls == 1


def test_never_captured_is_zero():
    assert _call(RpcState(contract=CONTRACT)) == 0


@pytest.mark.parametrize("mode", ["http500", "rpc_error", "garbage"])
def test_broken_node_is_unavailable(mode):
    with pytest.raises(ChainUnavailable):
        _call(RpcState(contract=CONTRACT, mode=mode))


def test_unreachable_node_is_unavailable():
    async def run():
        async with httpx.AsyncClient() as client:
            await last_capture(client, "http://127.0.0.1:9/", CONTRACT, ROBOT, PLAYER)

    with pytest.raises(ChainUnavailable):
        asyncio.run(run())

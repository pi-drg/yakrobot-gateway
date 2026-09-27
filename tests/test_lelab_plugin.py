"""Tests for the lelab_so101 plugin (§0.18), driven against the fake LeLab backend."""

import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def lelab_server():
    """Run the fake LeLab backend on a loopback port in a background thread."""
    import uvicorn
    from tools.fake_lelab import LeLabState, create_fake_lelab

    state = LeLabState()
    app = create_fake_lelab(state)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield state, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _make_mcp(base_url: str):
    from plugins.lelab_so101.mcp_tools import register
    from plugins.lelab_so101.robot_adapter import LelabAdapter

    adapter = LelabAdapter(base_url=base_url)
    mcp = FastMCP(name="lelab")
    register(mcp, adapter)
    return mcp, adapter


def _saw(state, method: str, path: str) -> bool:
    return any(m == method and p == path for m, p, _b in state.calls)


def _post_body(state, path: str):
    for m, p, body in state.calls:
        if m == "POST" and p == path:
            return body
    return None


_CAMERAS = {"front": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}}


def test_start_recording_fills_saved_rig(lelab_server):
    state, base_url = lelab_server
    state.saved_port = {"leader": "/dev/ttyUSB0", "follower": "/dev/ttyUSB1"}
    state.saved_config = {"leader": "leader.json", "follower": "follower.json"}
    mcp, _ = _make_mcp(base_url)
    res = asyncio.run(
        mcp.call_tool(
            "lelab_start_recording",
            {"dataset_repo_id": "user/ds", "single_task": "pick", "cameras": _CAMERAS},
        )
    )
    assert not res.is_error
    body = _post_body(state, "/start-recording")
    assert body["leader_port"] == "/dev/ttyUSB0"
    assert body["follower_port"] == "/dev/ttyUSB1"
    assert body["leader_config"] == "leader.json"
    assert body["follower_config"] == "follower.json"
    assert body["video"] is True
    assert body["push_to_hub"] is False


def test_start_recording_errors_without_saved_rig(lelab_server):
    state, base_url = lelab_server
    # No saved ports/configs — saved_rig() must fail before any POST is forwarded.
    state.saved_port = {}
    state.saved_config = {}
    mcp, _ = _make_mcp(base_url)
    with pytest.raises(ToolError, match="no saved"):
        asyncio.run(
            mcp.call_tool(
                "lelab_start_recording",
                {"dataset_repo_id": "user/ds", "single_task": "pick", "cameras": _CAMERAS},
            )
        )
    assert not _saw(state, "POST", "/start-recording")


def test_start_recording_requires_cameras(lelab_server):
    state, base_url = lelab_server
    state.saved_port = {"leader": "/dev/ttyUSB0", "follower": "/dev/ttyUSB1"}
    state.saved_config = {"leader": "l.json", "follower": "f.json"}
    mcp, _ = _make_mcp(base_url)

    with pytest.raises(ToolError, match="cameras is required"):
        asyncio.run(
            mcp.call_tool(
                "lelab_start_recording",
                {"dataset_repo_id": "user/ds", "single_task": "pick", "cameras": {}},
            )
        )
    with pytest.raises(ToolError, match="type == 'opencv'"):
        asyncio.run(
            mcp.call_tool(
                "lelab_start_recording",
                {"dataset_repo_id": "user/ds", "single_task": "pick",
                 "cameras": {"front": {"type": "realsense"}}},
            )
        )
    with pytest.raises(ToolError, match="integer"):
        asyncio.run(
            mcp.call_tool(
                "lelab_start_recording",
                {"dataset_repo_id": "user/ds", "single_task": "pick",
                 "cameras": {"front": {"type": "opencv", "camera_index": "0",
                                       "width": 640, "height": 480, "fps": 30}}},
            )
        )
    # None of the three invalid calls may reach the backend.
    assert not _saw(state, "POST", "/start-recording")


def test_status_merges_three_endpoints(lelab_server):
    state, base_url = lelab_server
    mcp, _ = _make_mcp(base_url)
    res = asyncio.run(mcp.call_tool("lelab_status", {}))
    assert not res.is_error
    data = res.structured_content
    assert set(data) == {"teleoperation", "recording", "inference"}
    assert _saw(state, "GET", "/teleoperation-status")
    assert _saw(state, "GET", "/recording-status")
    assert _saw(state, "GET", "/inference-status")


def test_start_inference_uses_follower_rig(lelab_server):
    state, base_url = lelab_server
    state.saved_port = {"leader": "/dev/ttyUSB0", "follower": "/dev/ttyUSB1"}
    state.saved_config = {"leader": "l.json", "follower": "f.json"}
    mcp, _ = _make_mcp(base_url)
    res = asyncio.run(
        mcp.call_tool("lelab_start_inference", {"policy_ref": "org/policy", "task": "pick"})
    )
    assert not res.is_error
    body = _post_body(state, "/start-inference")
    assert body["follower_port"] == "/dev/ttyUSB1"
    assert body["follower_config"] == "f.json"
    assert body["policy_ref"] == "org/policy"


def test_tool_names_match_registered_tools():
    from core.robot_marketplace_tools import MARKETPLACE_TOOL_NAMES
    from plugins.lelab_so101 import LelabSo101Plugin

    plugin = LelabSo101Plugin()
    mcp = FastMCP(name="lelab")
    plugin.register_tools(mcp)
    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    declared = set(plugin.tool_names()) - set(MARKETPLACE_TOOL_NAMES)
    assert declared == registered
    assert len(declared) == 10


def test_mcp_tools_blocked_while_ui_operator_holds_robot(lelab_server):
    from core.reservation import ReservationRegistry
    from core.server import create_robot_server
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from plugins.lelab_so101 import LelabSo101Plugin

    _state, base_url = lelab_server
    os.environ["LELAB_URL"] = base_url  # the plugin's register_tools reads this
    try:
        plugin = LelabSo101Plugin()
        registry = ReservationRegistry()
        # The UI operator (client_id "op") holds the robot via the relay's control route.
        assert registry.reserve("lelab_so101", "op")

        mcp = create_robot_server(plugin, registry, "lelab_so101")

        async def call_as(client_id: str, name: str):
            user = AuthenticatedUser(AccessToken(token="x", client_id=client_id, scopes=[]))
            token = auth_context_var.set(user)
            try:
                return await mcp.call_tool(name, {})
            finally:
                auth_context_var.reset(token)

        # A different client is blocked; the holder itself is not.
        with pytest.raises(ToolError, match="reserved by 'op'"):
            asyncio.run(call_as("other", "lelab_joint_positions"))
        assert not asyncio.run(call_as("op", "lelab_status")).is_error
    finally:
        os.environ.pop("LELAB_URL", None)

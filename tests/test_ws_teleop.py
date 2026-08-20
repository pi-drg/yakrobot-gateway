"""Realtime teleop path: gateway WebSocket proxy + the fake PiCar simulator.

Covers the whole chain a browser uses — client -> /{robot}/ws/* on the gateway
-> the robot's own control server — with no hardware, by driving the simulator
in ``plugins.fakerobot_picar``. The safety behaviours asserted here (deadman,
single-driver slot, clamps, stop-on-disconnect) are the ones whose absence is
invisible until a real car is moving, so they are worth pinning in CI.
"""

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SIM_PORT, GW_PORT = 8191, 8192


def _clear_auth_env():
    for key in ("MCP_TOKENS", "MCP_BEARER_TOKEN", "MCP_TOKENS_FILE"):
        os.environ.pop(key, None)


async def _serve(app, port):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


class _Stack:
    """Simulator + gateway, both on loopback, torn down together."""

    async def __aenter__(self):
        os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
        _clear_auth_env()

        import plugins.fakerobot_picar.simulator as sim
        from core.server import create_gateway
        from yakrobot_cli.commands import _load_plugins

        self.sim = sim
        sim.STATE = sim.SimState()  # fresh state per test; module globals are read at call time
        self._servers = [
            await _serve(sim.app, SIM_PORT),
            await _serve(create_gateway(_load_plugins(["fakerobot_picar"])), GW_PORT),
        ]
        self.control = f"ws://127.0.0.1:{GW_PORT}/fakerobot_picar/ws/control"
        self.video = f"ws://127.0.0.1:{GW_PORT}/fakerobot_picar/ws/video"
        return self

    async def __aexit__(self, *exc):
        for server, task in reversed(self._servers):
            server.should_exit = True
            await task


async def _recv_json(ws, timeout=5):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


def test_control_socket_proxies_to_robot():
    async def run():
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            async with connect(stack.control) as ws:
                hello = await _recv_json(ws)
                assert hello["type"] == "hello"
                assert hello["deadman_ms"] == 700
                assert hello["max_duty"] == 1400
                assert hello["holonomic"] is True

                await ws.send(json.dumps({"type": "ping", "t": 99}))
                assert await _recv_json(ws) == {"type": "pong", "t": 99}

                await ws.send(json.dumps({"type": "drive", "vx": 1200}))
                await asyncio.sleep(0.2)
                assert stack.sim.STATE.vx == 1200

                # Duties are clamped on the robot, not trusted from the client.
                await ws.send(json.dumps({"type": "drive", "vx": 99999, "vy": -99999}))
                await asyncio.sleep(0.2)
                assert (stack.sim.STATE.vx, stack.sim.STATE.vy) == (1400, -1400)

                await ws.send(json.dumps({"type": "look", "pan": 200, "tilt": 5}))
                assert await _recv_json(ws) == {"type": "look", "pan": 150, "tilt": 30}

    asyncio.run(run())


def test_deadman_stops_the_robot_without_heartbeats():
    async def run():
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            async with connect(stack.control) as ws:
                await _recv_json(ws)  # hello
                await ws.send(json.dumps({"type": "drive", "vx": 1200}))
                await asyncio.sleep(0.2)
                assert stack.sim.STATE.vx == 1200

                # Silence for longer than DEADMAN_MS: the robot must stop itself.
                await asyncio.sleep(0.9)
                assert (stack.sim.STATE.vx, stack.sim.STATE.vy, stack.sim.STATE.omega) == (0, 0, 0)

    asyncio.run(run())


def test_disconnect_stops_the_robot_and_frees_the_slot():
    async def run():
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            async with connect(stack.control) as ws:
                await _recv_json(ws)
                await ws.send(json.dumps({"type": "drive", "vx": 1000}))
                await asyncio.sleep(0.15)
                assert stack.sim.STATE.vx == 1000

            await asyncio.sleep(0.4)
            assert stack.sim.STATE.vx == 0
            assert stack.sim.STATE.controller is None

            async with connect(stack.control) as ws2:
                assert (await _recv_json(ws2))["controller"] is True

    asyncio.run(run())


def test_second_driver_is_view_only():
    async def run():
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            async with connect(stack.control) as driver:
                await _recv_json(driver)
                async with connect(stack.control) as spotter:
                    assert await _recv_json(spotter) == {"type": "error", "detail": "busy"}
                    hello = await _recv_json(spotter)
                    assert hello["controller"] is False

                    await spotter.send(json.dumps({"type": "drive", "vx": 1200}))
                    assert await _recv_json(spotter) == {"type": "error", "detail": "busy"}
                    assert stack.sim.STATE.vx == 0

    asyncio.run(run())


def test_video_stream_delivers_decodable_jpeg_frames():
    async def run():
        from PIL import Image
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            frames = []
            async with connect(stack.video) as ws:
                for _ in range(4):
                    frames.append(await asyncio.wait_for(ws.recv(), timeout=5))

            assert all(isinstance(f, bytes) and f[:2] == b"\xff\xd8" for f in frames)
            assert Image.open(io.BytesIO(frames[0])).size == (400, 300)
            # Distinct frames prove the stream is live rather than one cached image.
            assert len(set(frames)) > 1

    asyncio.run(run())


async def _refusal(url):
    """Open a socket the gateway means to refuse and return how it said no.

    The refusal is delivered *after* the handshake completes, deliberately: a
    browser reports every failed handshake as code 1006, so a code sent that way
    would never reach the console that has to act on it.
    """
    from websockets.asyncio.client import connect

    async with connect(url) as ws:
        with pytest.raises(Exception):
            await asyncio.wait_for(ws.recv(), timeout=5)
    return ws.close_code, ws.close_reason


def test_proxy_refuses_robots_without_a_control_server():
    async def run():
        async with _Stack() as stack:
            url = stack.control.replace("fakerobot_picar", "tello")
            code, reason = await _refusal(url)
            # 1008 (policy): permanent, so the console stops retrying.
            assert code == 1008
            assert "no realtime socket" in reason

    asyncio.run(run())


def test_offline_robot_is_refused_as_retryable():
    """A robot that is off gets 1013, not 1008 — it may just be rebooting."""
    async def run():
        async with _Stack() as stack:
            # Point the plugin at a port with nothing behind it, so the upstream
            # connect fails the way it does when the car is switched off.
            os.environ["FAKEROBOT_PICAR_URL"] = "http://127.0.0.1:9"
            try:
                from core.server import create_gateway
                from yakrobot_cli.commands import _load_plugins

                server, task = await _serve(
                    create_gateway(_load_plugins(["fakerobot_picar"])), GW_PORT + 1
                )
                try:
                    url = f"ws://127.0.0.1:{GW_PORT + 1}/fakerobot_picar/ws/control"
                    code, reason = await _refusal(url)
                    assert code == 1013
                    assert reason == "robot offline"

                    # The second attempt is answered from the cached verdict.
                    # Same answer, so the console cannot tell — which is the
                    # point: only the cost differs.
                    assert await _refusal(url) == (1013, "robot offline")
                finally:
                    server.should_exit = True
                    await task
            finally:
                os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
            assert stack  # keep the simulator alive for the duration

    asyncio.run(run())


def test_mcp_mount_is_not_shadowed_by_the_ws_route():
    """The /ws/* route must be registered before app.mount, but not swallow it."""
    from starlette.testclient import TestClient

    _clear_auth_env()
    os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
    from core.server import create_gateway
    from yakrobot_cli.commands import _load_plugins

    app = create_gateway(_load_plugins(["fakerobot_picar"]))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        # 406 (bad Accept) rather than 404 means the MCP mount still resolves.
        assert client.get("/fakerobot_picar/mcp").status_code != 404


def test_console_served_for_drivable_robots_only():
    """The console is served per-robot, and only where sockets could connect."""
    from starlette.testclient import TestClient

    _clear_auth_env()
    os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
    from core.server import create_gateway
    from yakrobot_cli.commands import _load_plugins

    app = create_gateway(_load_plugins(["fakerobot_picar", "fakerobot"]))
    with TestClient(app) as client:
        page = client.get("/fakerobot_picar/ui")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "ws/control" in page.text          # the console really is the console

        # fakerobot exposes no realtime control server, so no console.
        assert client.get("/fakerobot/ui").status_code == 404

        index = client.get("/").json()
        assert index["robots"]["fakerobot_picar"]["ui_endpoint"] == "/fakerobot_picar/ui"
        assert "ui_endpoint" not in index["robots"]["fakerobot"]


def test_video_can_be_disabled_gateway_wide():
    """VIDEO_ENABLED=0 refuses video for every client but keeps control working."""
    async def run():
        from websockets.asyncio.client import connect

        os.environ["VIDEO_ENABLED"] = "0"
        try:
            async with _Stack() as stack:
                # 1008 must reach the browser intact — it is the signal the
                # console keys off to stop retrying a refusal that will not
                # change until the gateway is restarted.
                code, reason = await _refusal(stack.video)
                assert code == 1008
                assert "video disabled" in reason

                # Control is untouched — the car stays drivable, just blind.
                async with connect(stack.control) as ws:
                    assert (await _recv_json(ws))["type"] == "hello"
        finally:
            os.environ.pop("VIDEO_ENABLED", None)

    asyncio.run(run())

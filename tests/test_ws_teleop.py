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
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capability_helper import mint  # noqa: E402

SIM_PORT, GW_PORT = 8191, 8192


def _new_issuer():
    """A fresh secp256k1 keypair, so each test's PAYMENTS_ISSUER is independent."""
    from eth_keys import keys

    pk = keys.PrivateKey(os.urandom(32))
    return pk, pk.public_key.to_checksum_address().lower()


def _payments_env(issuer: str, **overrides) -> dict[str, str]:
    env = {
        "PAYMENTS_ENABLED": "1",
        "PAYMENTS_URL": "http://127.0.0.1:9",  # never dialed — the gateway only verifies
        "PAYMENTS_ISSUER": issuer,
        "TELEOP_LEASE_MINUTES": "5",
    }
    env.update(overrides)
    return env


def _capability_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "v": 1,
        "robot": "fakerobot_picar",
        "gateway": f"127.0.0.1:{GW_PORT}",
        "lease": "22222222-2222-4222-8222-222222222222",
        "payer": "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


# Cleared before every _Stack, so a developer's real .env (loaded once, at whichever
# test imports core.server first) never decides what these tests assert on.
_CLEARED_ENV_VARS = (
    "MCP_TOKENS", "MCP_BEARER_TOKEN", "MCP_TOKENS_FILE",
    "NGROK_DOMAIN", "CLOUDFLARE_DOMAIN",
    "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
    "TELEOP_PRICE_USDC", "TELEOP_LEASE_MINUTES",
)


def _clear_auth_env():
    for key in _CLEARED_ENV_VARS:
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

    def __init__(self, env: dict[str, str] | None = None):
        self._env_overrides = env or {}

    async def __aenter__(self):
        # Import BEFORE clearing env: both core.server AND yakrobot_cli (imported below
        # for _load_plugins) call load_dotenv() at module level, and each only runs once
        # per process (Python caches the import). Clearing first would let whichever of
        # the two hasn't been imported yet repopulate what we just cleared from a
        # developer's real .env the moment something below imports it for the first
        # time — this bit a test that filtered to run alone (`pytest -k`), where it was
        # the very first _Stack in the process, while full-suite runs hid it: an earlier
        # test happened to import both modules first, with no assertion sensitive to it.
        import core.server  # noqa: F401
        import yakrobot_cli  # noqa: F401

        os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
        _clear_auth_env()
        for key, value in self._env_overrides.items():
            os.environ[key] = value

        import plugins.fakerobot_picar.simulator as sim
        from core.server import create_gateway
        from yakrobot_cli.commands import _load_plugins

        self.sim = sim
        sim.STATE = sim.SimState()  # fresh state per test; module globals are read at call time
        self.app = create_gateway(_load_plugins(["fakerobot_picar"]))
        self._servers = [
            await _serve(sim.app, SIM_PORT),
            await _serve(self.app, GW_PORT),
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


def test_index_reports_payments_disabled_by_default():
    async def run():
        import httpx

        async with _Stack():
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{GW_PORT}/")
                assert r.json()["payments"] == {"enabled": False}

    asyncio.run(run())


def test_wrong_static_token_refusal_reaches_client():
    """core/ws_proxy.py's unauthorized branch must go through _refuse(), not a bare
    ws.close() before accept() — otherwise the browser only ever sees 1006."""
    async def run():
        async with _Stack(env={"MCP_TOKENS": "teleop-ui=tok_def"}) as stack:
            code, reason = await _refusal(stack.control + "?token=wrongtoken")
            assert code == 1008
            assert reason == "unauthorized"

    asyncio.run(run())


async def _poll_online(client, url, robot, want, attempts=30, interval=0.1):
    """Poll the index until `online` reaches `want`, or return the last value seen.

    The probe runs once immediately on startup, but that first pass races the
    lifespan's own startup — a caller hitting `/` in the same instant can still see
    the pre-probe `null`, so tests must poll rather than assert on the first read.
    """
    online = None
    for _ in range(attempts):
        r = await client.get(url)
        online = r.json()["robots"][robot]["online"]
        if online == want:
            return online
        await asyncio.sleep(interval)
    return online


def test_index_reports_robot_online_when_simulator_up():
    async def run():
        import httpx
        from core import reachability

        reachability.PROBE_INTERVAL_S = 0.2
        try:
            async with _Stack():
                async with httpx.AsyncClient() as client:
                    online = await _poll_online(
                        client, f"http://127.0.0.1:{GW_PORT}/", "fakerobot_picar", True
                    )
                    assert online is True
        finally:
            reachability.PROBE_INTERVAL_S = 15.0

    asyncio.run(run())


def test_index_reports_robot_offline_when_unreachable():
    async def run():
        import httpx
        from core import reachability
        from core.server import create_gateway
        from yakrobot_cli.commands import _load_plugins

        reachability.PROBE_INTERVAL_S = 0.2
        # Nothing listens here — the way a robot looks when it is switched off.
        os.environ["FAKEROBOT_PICAR_URL"] = "http://127.0.0.1:9"
        _clear_auth_env()
        port = GW_PORT + 2
        try:
            server, task = await _serve(create_gateway(_load_plugins(["fakerobot_picar"])), port)
            try:
                async with httpx.AsyncClient() as client:
                    online = await _poll_online(
                        client, f"http://127.0.0.1:{port}/", "fakerobot_picar", False
                    )
                    assert online is False
            finally:
                server.should_exit = True
                await task
        finally:
            os.environ["FAKEROBOT_PICAR_URL"] = f"http://127.0.0.1:{SIM_PORT}"
            reachability.PROBE_INTERVAL_S = 15.0

    asyncio.run(run())


def test_index_online_null_for_robot_without_control_server():
    """fakerobot exposes no realtime control server, so it is never probed."""
    async def run():
        import httpx
        from core.server import create_gateway
        from yakrobot_cli.commands import _load_plugins

        _clear_auth_env()
        port = GW_PORT + 3
        server, task = await _serve(create_gateway(_load_plugins(["fakerobot"])), port)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{port}/")
                assert r.json()["robots"]["fakerobot"]["online"] is None
        finally:
            server.should_exit = True
            await task

    asyncio.run(run())


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


# --------------------------------------------------------------------------
# Paid teleop — capability admission, reservation binding, expiry.
# paid-teleop-execution.md phase 2, step 2.2.
# --------------------------------------------------------------------------

def test_payments_disabled_keeps_todays_behaviour():
    async def run():
        from websockets.asyncio.client import connect

        async with _Stack() as stack:
            async with connect(stack.control) as ws:
                assert (await _recv_json(ws))["type"] == "hello"

    asyncio.run(run())


def test_no_token_refused_when_payments_enabled():
    async def run():
        _, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            assert await _refusal(stack.control) == (1008, "payment required")

    asyncio.run(run())


def test_valid_capability_admits_and_reserves():
    async def run():
        import httpx
        from websockets.asyncio.client import connect

        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            claims = _capability_claims()
            token = mint(claims, pk.to_hex())
            async with connect(f"{stack.control}?token={token}") as ws:
                assert (await _recv_json(ws))["type"] == "hello"

                async with httpx.AsyncClient() as client:
                    r = await client.get(f"http://127.0.0.1:{GW_PORT}/")
                    reservation = r.json()["robots"]["fakerobot_picar"]["reservation"]
                    assert reservation["holder"] == f"lease:{claims['lease']}"

    asyncio.run(run())


def test_capability_for_other_robot_refused():
    async def run():
        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            token = mint(_capability_claims(robot="some_other_robot"), pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "lease is for another robot")

    asyncio.run(run())


def test_capability_for_other_gateway_refused():
    async def run():
        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            token = mint(_capability_claims(gateway="pay.example.com"), pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "lease is for another gateway")

    asyncio.run(run())


def test_expired_capability_refused():
    async def run():
        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            now = int(time.time())
            # A valid, well-formed lease that has simply run out — verify() itself only
            # checks iat/duration; ws_proxy is what checks exp against the clock.
            token = mint(_capability_claims(iat=now - 400, exp=now - 100), pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "lease expired")

    asyncio.run(run())


def test_wrong_issuer_refused():
    async def run():
        _, issuer = _new_issuer()
        wrong_pk, _ = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            # Signed by a different key than PAYMENTS_ISSUER — recovers to the wrong
            # address, so verify() folds it into the single "invalid lease" reason.
            token = mint(_capability_claims(), wrong_pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "invalid lease")

    asyncio.run(run())


def test_overlong_capability_refused():
    async def run():
        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer, TELEOP_LEASE_MINUTES="5")) as stack:
            now = int(time.time())
            # 6 minutes > 5*60 + 30s leeway.
            token = mint(_capability_claims(iat=now, exp=now + 6 * 60), pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "invalid lease")

    asyncio.run(run())


def test_capability_refused_while_agent_holds_reservation():
    async def run():
        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            stack.app.state.registry.reserve("fakerobot_picar", "marketplace")
            token = mint(_capability_claims(), pk.to_hex())
            code, reason = await _refusal(f"{stack.control}?token={token}")
            assert (code, reason) == (1008, "robot is held by another session")

    asyncio.run(run())


def test_static_mcp_token_admits_and_reserves():
    async def run():
        import httpx
        from websockets.asyncio.client import connect

        _, issuer = _new_issuer()
        env = _payments_env(issuer, MCP_TOKENS="operator=tok_def")
        async with _Stack(env=env) as stack:
            async with connect(f"{stack.control}?token=tok_def") as ws:
                assert (await _recv_json(ws))["type"] == "hello"

                async with httpx.AsyncClient() as client:
                    r = await client.get(f"http://127.0.0.1:{GW_PORT}/")
                    reservation = r.json()["robots"]["fakerobot_picar"]["reservation"]
                    assert reservation["holder"] == "operator"

    asyncio.run(run())


def test_static_token_reservation_released_on_close():
    async def run():
        import httpx
        from websockets.asyncio.client import connect

        _, issuer = _new_issuer()
        env = _payments_env(issuer, MCP_TOKENS="operator=tok_def")
        async with _Stack(env=env) as stack:
            async with connect(f"{stack.control}?token=tok_def") as ws:
                assert (await _recv_json(ws))["type"] == "hello"

            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{GW_PORT}/")
                assert r.json()["robots"]["fakerobot_picar"]["reservation"] == {"reserved": False}

    asyncio.run(run())


def test_socket_closes_at_exp():
    async def run():
        from websockets.asyncio.client import connect

        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            now = int(time.time())
            token = mint(_capability_claims(iat=now, exp=now + 2), pk.to_hex())
            async with connect(f"{stack.control}?token={token}") as ws:
                assert (await _recv_json(ws))["type"] == "hello"
                with pytest.raises(Exception):
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert ws.close_code == 1008
                assert ws.close_reason == "lease expired"

    asyncio.run(run())


def test_reservation_survives_reconnect():
    async def run():
        import httpx
        from websockets.asyncio.client import connect

        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            claims = _capability_claims()
            token = mint(claims, pk.to_hex())

            async with connect(f"{stack.control}?token={token}") as ws:
                assert (await _recv_json(ws))["type"] == "hello"
            # Let the simulator notice the disconnect and free its own driver slot
            # before reconnecting — same reasoning as test_disconnect_stops_the_robot.
            await asyncio.sleep(0.4)

            async with connect(f"{stack.control}?token={token}") as ws2:
                assert (await _recv_json(ws2))["type"] == "hello"

            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{GW_PORT}/")
                reservation = r.json()["robots"]["fakerobot_picar"]["reservation"]
                assert reservation["holder"] == f"lease:{claims['lease']}"

    asyncio.run(run())


def test_video_socket_also_requires_capability():
    async def run():
        _, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            assert await _refusal(stack.video) == (1008, "payment required")

    asyncio.run(run())


def test_capability_admits_both_sockets():
    """The golden path the console actually hits: one lease, both sockets. reserve()
    for the same lease: holder must be idempotent, not just "not held by someone else"."""
    async def run():
        from websockets.asyncio.client import connect

        pk, issuer = _new_issuer()
        async with _Stack(env=_payments_env(issuer)) as stack:
            token = mint(_capability_claims(), pk.to_hex())
            async with connect(f"{stack.control}?token={token}") as control_ws:
                assert (await _recv_json(control_ws))["type"] == "hello"
                async with connect(f"{stack.video}?token={token}") as video_ws:
                    frame = await asyncio.wait_for(video_ws.recv(), timeout=5)
                    assert isinstance(frame, bytes) and frame[:2] == b"\xff\xd8"

    asyncio.run(run())

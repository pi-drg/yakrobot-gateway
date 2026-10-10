"""picar_adeept plugin: tool registration and the HTTP shapes it sends the car.

No hardware and no simulator — the adapter's httpx client runs against a
MockTransport that records requests and answers the way picar_adeept_fastapi
does, including its refusals (400 for a strafe, 501 for /scan).
"""

import asyncio
import json

import httpx
import pytest


def _plugin():
    from plugins import discover_plugins
    return discover_plugins()["picar_adeept"]()


class FakeCar:
    """Records requests; answers like picar_adeept_fastapi."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/info":
            return httpx.Response(200, json={
                "robot": "adeept-awr", "hardware": "ok", "battery_v": 7.9,
                "battery_pct": 79, "wheels": "ordinary", "holonomic": False,
                "servos": ["tilt"]})
        if path == "/mecanum" and body and body.get("vy"):
            return httpx.Response(400, json={"detail": "cannot move sideways"})
        if path == "/look":
            return httpx.Response(200, json={"status": "ok", "pan": None,
                                             "tilt": max(70, min(150, body["tilt"]))})
        if path == "/snapshot":
            return httpx.Response(200, content=b"\xff\xd8jpeg\xff\xd9",
                                  headers={"content-type": "image/jpeg"})
        if path == "/scan":
            return httpx.Response(501, json={"detail": "no pan servo"})
        return httpx.Response(200, json={"echo": body})


@pytest.fixture
def car():
    from plugins.picar_adeept.robot_adapter import PicarAdeeptAdapter
    fake = FakeCar()
    adapter = PicarAdeeptAdapter(base_url="http://car.test", token="",
                                 transport=httpx.MockTransport(fake))
    return fake, adapter


def test_plugin_is_discovered_with_matching_prefix():
    p = _plugin()
    assert p.metadata().url_prefix == "picar_adeept"
    assert p.metadata().robot_type == "mobile_robot"


def test_tool_names_match_registered_tools(monkeypatch):
    from fastmcp import FastMCP
    monkeypatch.setenv("PICAR_ADEEPT_URL", "http://127.0.0.1:9")
    p = _plugin()
    m = FastMCP(name="probe")
    p.register_tools(m)
    registered = {t.name for t in asyncio.run(m.list_tools())}
    assert registered == set(p.tool_names())
    # Tools that could only ever fail on this hardware are not offered.
    assert "picar_adeept_scan" not in registered


def test_url_candidates_and_token(monkeypatch):
    from plugins.picar_adeept.robot_adapter import control_base_urls
    monkeypatch.setenv("PICAR_ADEEPT_URL", "http://picar.local:8080/, http://10.0.0.5:8080")
    assert control_base_urls() == ["http://picar.local:8080", "http://10.0.0.5:8080"]
    monkeypatch.setenv("PICAR_ADEEPT_TOKEN", "abc")
    assert _plugin().control_auth_token() == "abc"
    monkeypatch.delenv("PICAR_ADEEPT_TOKEN")
    assert _plugin().control_auth_token() is None


def test_bearer_header_sent_when_token_set():
    from plugins.picar_adeept.robot_adapter import PicarAdeeptAdapter
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    a = PicarAdeeptAdapter(base_url="http://car.test", token="s3cret",
                           transport=httpx.MockTransport(handler))
    asyncio.run(a.battery())
    assert seen["auth"] == "Bearer s3cret"


def test_capabilities(car):
    _, a = car
    caps = asyncio.run(a.capabilities())
    assert caps["ok"] and caps["wheels"] == "ordinary" and caps["holonomic"] is False
    assert caps["servos"] == ["tilt"]


def test_drive_clamps_duration_and_duty(car):
    fake, a = car
    asyncio.run(a.drive("forward", 99999, 60000))
    assert fake.requests[-1] == ("POST", "/drive",
                                 {"direction": "forward", "duty": 4095, "duration_ms": 3000})


def test_move_never_strafes(car):
    fake, a = car
    r = asyncio.run(a.move(1600, -600, 500))
    assert r["ok"]
    assert fake.requests[-1][2] == {"vx": 1600, "vy": 0, "omega": -600, "duration_ms": 500}


def test_stop(car):
    fake, a = car
    asyncio.run(a.stop())
    assert fake.requests[-1] == ("POST", "/drive", {"direction": "stop", "duration_ms": 0})


def test_look_sends_tilt_only(car):
    fake, a = car
    r = asyncio.run(a.look(200))
    assert fake.requests[-1][2] == {"tilt": 200}
    assert r == {"ok": True, "status": "ok", "pan": None, "tilt": 150}


def test_http_errors_are_shaped_not_raised(car):
    _, a = car
    r = asyncio.run(a.post("/mecanum", {"vy": 500}))
    assert r == {"ok": False, "status": 400, "error": "cannot move sideways"}


def test_unreachable_car_is_reported():
    from plugins.picar_adeept.robot_adapter import PicarAdeeptAdapter

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    a = PicarAdeeptAdapter(base_url="http://car.test", token="",
                           transport=httpx.MockTransport(handler))
    r = asyncio.run(a.distance())
    assert r["ok"] is False and "cannot reach the robot" in r["error"]
    with pytest.raises(RuntimeError, match="cannot reach"):
        asyncio.run(a.snapshot_bytes())


def test_snapshot_bytes(car):
    _, a = car
    assert asyncio.run(a.snapshot_bytes()).startswith(b"\xff\xd8")


def test_buzzer_caps_total(car):
    fake, a = car
    asyncio.run(a.buzzer([("C4", 2000), ("rest", 500), ("G4", 2000), ("A4", 100)]))
    assert fake.requests[-1][2] == {"notes": [
        {"note": "C4", "ms": 2000}, {"note": "rest", "ms": 500}, {"note": "G4", "ms": 500}]}


def test_led_drops_unset_fields(car):
    fake, a = car
    asyncio.run(a.led(effect="solid", r=255, g=0, b=0, brightness=None, index=None))
    assert fake.requests[-1][2] == {"effect": "solid", "r": 255, "g": 0, "b": 0}


def test_tools_end_to_end(monkeypatch):
    """Call the MCP tools themselves against the fake car."""
    from fastmcp import FastMCP
    from plugins.picar_adeept import mcp_tools
    from plugins.picar_adeept.robot_adapter import PicarAdeeptAdapter

    fake = FakeCar()
    a = PicarAdeeptAdapter(base_url="http://car.test", token="",
                           transport=httpx.MockTransport(fake))
    m = FastMCP(name="probe")
    mcp_tools.register(m, a)

    def call(name, args):
        res = asyncio.run(m.call_tool(name, args))
        return res.structured_content

    assert call("picar_adeept_is_online", {})["online"] is True
    assert call("picar_adeept_look", {"tilt": 120})["tilt"] == 120
    call("picar_adeept_beep", {"notes": [["C4", 100], ["G4", 100]]})
    assert fake.requests[-1][2] == {"notes": [{"note": "C4", "ms": 100},
                                              {"note": "G4", "ms": 100}]}
    call("picar_adeept_beep", {})
    assert fake.requests[-1][2] == {"notes": [{"note": "A4", "ms": 200}]}

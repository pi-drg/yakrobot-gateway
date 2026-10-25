"""Offline tests for the picar_adeept plugin (mock HTTP transport, no car).

Pins what differs from the Freenove car on the wire: tilt-only /look, vy forced
to 0, the buzzer payload — and that the tool set never offers pan, scan or
strafe, which this hardware cannot do.
"""

import asyncio
import json

import httpx


def _adapter_with_log():
    from plugins.picar_adeept.robot_adapter import PicarAdeeptAdapter

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"status": "ok"})

    adapter = PicarAdeeptAdapter(base_url="http://car", token="")
    adapter.client = httpx.AsyncClient(
        base_url="http://car", transport=httpx.MockTransport(handler))
    return adapter, sent


def test_plugin_is_discovered_with_matching_prefix():
    from plugins import discover_plugins

    plugin = discover_plugins()["picar_adeept"]()
    assert plugin.metadata().url_prefix == "picar_adeept"


def test_registered_tools_match_tool_names_and_omit_missing_hardware():
    from fastmcp import FastMCP
    from plugins.picar_adeept import PicarAdeeptPlugin

    plugin = PicarAdeeptPlugin()
    mcp = FastMCP(name="probe")
    plugin.register_tools(mcp)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == set(plugin.tool_names())
    assert "picar_adeept_scan" not in names


def test_look_sends_tilt_only():
    adapter, sent = _adapter_with_log()
    asyncio.run(adapter.look(120))
    assert sent == [("POST", "/look", {"tilt": 120})]


def test_move_never_strafes_and_clamps_duration():
    adapter, sent = _adapter_with_log()
    asyncio.run(adapter.move(1600, -400, 60_000))
    assert sent == [("POST", "/mecanum",
                     {"vx": 1600, "vy": 0, "omega": -400, "duration_ms": 3000})]


def test_buzzer_trims_sequence_to_cap():
    adapter, sent = _adapter_with_log()
    asyncio.run(adapter.buzzer([("C4", 2000), ("E4", 2000), ("G4", 500)]))
    assert sent == [("POST", "/buzzer",
                     {"notes": [{"note": "C4", "ms": 2000}, {"note": "E4", "ms": 1000}]})]


def test_url_and_token_come_from_adeept_env_not_freenove(monkeypatch):
    from plugins.picar_adeept import PicarAdeeptPlugin

    monkeypatch.setenv("PICAR_FREENOVE_URL", "http://wrong-car:8080")
    monkeypatch.setenv("PICAR_FREENOVE_TOKEN", "wrong")
    monkeypatch.setenv("PICAR_ADEEPT_URL", "http://127.0.0.1:8080, http://picar.local:8080")
    monkeypatch.setenv("PICAR_ADEEPT_TOKEN", "right")
    plugin = PicarAdeeptPlugin()
    assert plugin.control_base_urls() == ["http://127.0.0.1:8080", "http://picar.local:8080"]
    assert plugin.control_auth_token() == "right"

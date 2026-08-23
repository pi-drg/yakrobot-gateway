"""Offline tests for yakrobot-gateway (no network beyond local ASGI).

Covers the chain-free invariant (no agent0/web3/chain imports in gateway source),
plugin discovery, the local discovery tool, the gateway boot + index route, the fleet
tool set, and the descriptor exporter (producer side of the yakrobot-descriptor contract).
"""

import asyncio
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


# --------------------------------------------------------------------------
# Chain-free invariant — gateway source must not import chain code
# --------------------------------------------------------------------------
def test_gateway_source_is_chain_free():
    forbidden = ("agent0_sdk", "import web3", "from web3",
                 "core.chains", "core.discovery", "core.registration", "core.wallet")
    for p in (SRC).rglob("*.py"):
        text = p.read_text()
        for bad in forbidden:
            assert bad not in text, f"{bad!r} found in {p.relative_to(SRC)}"


def test_scripts_are_chain_free():
    # Scripts produce descriptors / serve robots; none reach into chain code.
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for p in scripts.rglob("*.py"):
        text = p.read_text()
        assert "core.registration" not in text
        assert "core.chains" not in text
        assert "import identity" not in text
        assert "from identity" not in text


# --------------------------------------------------------------------------
# Plugin discovery + local discovery tool
# --------------------------------------------------------------------------
def _fakerobot_plugins():
    from plugins import discover_plugins
    return {n: c() for n, c in discover_plugins().items() if n == "fakerobot"}


def test_plugin_discovery_finds_fakerobot():
    assert "fakerobot" in _fakerobot_plugins()


def test_local_discovery_tool_lists_connected_robot():
    from fastmcp import FastMCP
    from core.local_discovery import register_local_discovery_tools

    plugins = _fakerobot_plugins()
    m = FastMCP(name="probe")
    register_local_discovery_tools(m, plugins, mounted_robots={"fakerobot": "/fakerobot/mcp"})
    res = asyncio.run(m.call_tool("list_connected", {}))
    data = res.structured_content if hasattr(res, "structured_content") else res
    assert data["count"] == 1
    robot = data["robots"][0]
    assert robot["local_endpoint"] == "/fakerobot/mcp"
    assert robot["tools"]


# --------------------------------------------------------------------------
# Fleet server is discovery-only — no auction tools (those live in yakrobot-marketplace),
# no on-chain discovery
# --------------------------------------------------------------------------
def test_fleet_server_is_discovery_only():
    from core.server import create_fleet_server

    plugins = _fakerobot_plugins()
    fleet = create_fleet_server(plugins, mounted_robots={"fakerobot": "/fakerobot/mcp"})
    names = {t.name for t in asyncio.run(fleet.list_tools())}
    assert "list_connected" in names
    assert "discover_robot_agents" not in names        # no on-chain discovery
    assert not any(n.startswith("fleet_") for n in names)  # auctions moved out


# --------------------------------------------------------------------------
# Gateway boots and serves the index
# --------------------------------------------------------------------------
def test_gateway_boots_and_serves_index():
    from starlette.testclient import TestClient
    from core.server import create_gateway

    app = create_gateway(_fakerobot_plugins())
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["fleet_endpoint"] == "/fleet/mcp"
        assert "fakerobot" in body["robots"]


# --------------------------------------------------------------------------
# Descriptor exporter — producer side of the yakrobot-descriptor contract
# (needs the export/dev extra installed)
# --------------------------------------------------------------------------
def test_build_descriptor_produces_valid_contract():
    # Needs the yakrobot-descriptor package (export/dev extra); skip if absent so the
    # serve-only CI checkout stays self-contained.
    pytest.importorskip("yakrobot_descriptor")
    from core.descriptor import build_descriptor
    from yakrobot_descriptor import RobotDescriptor

    p = _fakerobot_plugins()["fakerobot"]
    d = build_descriptor(p, "demo.ngrok.app")
    assert isinstance(d, RobotDescriptor)
    assert d.mcp_endpoint == "https://demo.ngrok.app/fakerobot/mcp"
    assert d.fleet_endpoint == "https://demo.ngrok.app/fleet/mcp"
    # Round-trips through JSON as the contract requires.
    assert RobotDescriptor.model_validate_json(d.model_dump_json()) == d


def test_build_descriptor_without_domain_omits_urls():
    pytest.importorskip("yakrobot_descriptor")
    from core.descriptor import build_descriptor

    d = build_descriptor(_fakerobot_plugins()["fakerobot"])
    assert d.mcp_endpoint == ""
    assert d.fleet_endpoint == ""


# --------------------------------------------------------------------------
# GET /{robot}/descriptor — the same document, served live
#
# Every test drives the route through create_gateway rather than a bare FastAPI
# app: the failure it is guarding against is registration *order*, and that only
# shows up once app.mount("/{robot}") is claiming the prefix.
# --------------------------------------------------------------------------
def _gateway_client(monkeypatch):
    from starlette.testclient import TestClient
    from core.server import create_gateway

    # A developer's real tunnel domain must not decide what these assert on.
    monkeypatch.delenv("NGROK_DOMAIN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_DOMAIN", raising=False)
    return TestClient(create_gateway(_fakerobot_plugins()))


def _assert_cors(response):
    assert response.headers["access-control-allow-origin"] == "*"
    assert "GET" in response.headers["access-control-allow-methods"]
    # The page must be able to send this past free-tier ngrok's interstitial.
    assert "ngrok-skip-browser-warning" in response.headers["access-control-allow-headers"]


def test_descriptor_route_survives_the_mounts(monkeypatch):
    pytest.importorskip("yakrobot_descriptor")
    with _gateway_client(monkeypatch) as client:
        r = client.get("/fakerobot/descriptor")
        assert r.status_code == 200, "shadowed by app.mount — registered after the mounts?"
        body = r.json()
        assert body["name"]
        # No env domain set, so the Host header resolved the public endpoints.
        assert body["mcp_endpoint"] == "https://testserver/fakerobot/mcp"
        assert body["fleet_endpoint"] == "https://testserver/fleet/mcp"
        _assert_cors(r)


def test_descriptor_route_prefers_env_domain(monkeypatch):
    pytest.importorskip("yakrobot_descriptor")
    with _gateway_client(monkeypatch) as client:
        monkeypatch.setenv("NGROK_DOMAIN", "demo.ngrok.app")
        r = client.get("/fakerobot/descriptor")
        assert r.json()["mcp_endpoint"] == "https://demo.ngrok.app/fakerobot/mcp"


def test_descriptor_route_answers_preflight(monkeypatch):
    with _gateway_client(monkeypatch) as client:
        r = client.options("/fakerobot/descriptor")
        assert r.status_code == 204
        _assert_cors(r)


def test_descriptor_route_unknown_robot_is_404_and_still_readable(monkeypatch):
    with _gateway_client(monkeypatch) as client:
        r = client.get("/nosuchrobot/descriptor")
        assert r.status_code == 404
        # Errors carry CORS too, or the browser sees an opaque failure instead of why.
        _assert_cors(r)


def test_descriptor_route_501_without_the_export_extra(monkeypatch):
    import sys

    # Make `import yakrobot_descriptor` raise, and force core.descriptor to be
    # imported afresh so it actually goes looking.
    monkeypatch.setitem(sys.modules, "yakrobot_descriptor", None)
    monkeypatch.delitem(sys.modules, "core.descriptor", raising=False)
    with _gateway_client(monkeypatch) as client:
        r = client.get("/fakerobot/descriptor")
        assert r.status_code == 501
        assert "--extra export" in r.json()["detail"]
        _assert_cors(r)


def test_descriptor_route_503_when_no_public_host(monkeypatch):
    pytest.importorskip("yakrobot_descriptor")
    from core import descriptor_route

    # No env domain and no Host header. Refusing beats emitting a descriptor whose
    # mcp_endpoint is "" — that registers cleanly and resolves to nothing.
    monkeypatch.setattr(descriptor_route, "_public_domain", lambda request: "")
    with _gateway_client(monkeypatch) as client:
        r = client.get("/fakerobot/descriptor")
        assert r.status_code == 503
        _assert_cors(r)


def test_public_domain_precedence(monkeypatch):
    from starlette.requests import Request
    from core.descriptor_route import _public_domain

    def request_with_host(host):
        headers = [(b"host", host.encode())] if host else []
        return Request({"type": "http", "headers": headers})

    monkeypatch.delenv("NGROK_DOMAIN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_DOMAIN", raising=False)
    assert _public_domain(request_with_host("tunnel.example:8000")) == "tunnel.example:8000"
    assert _public_domain(request_with_host("")) == ""

    monkeypatch.setenv("CLOUDFLARE_DOMAIN", "cf.example")
    assert _public_domain(request_with_host("tunnel.example")) == "cf.example"
    monkeypatch.setenv("NGROK_DOMAIN", "ngrok.example")
    assert _public_domain(request_with_host("tunnel.example")) == "ngrok.example"


def test_index_advertises_the_descriptor_endpoint(monkeypatch):
    with _gateway_client(monkeypatch) as client:
        r = client.get("/")
        assert r.json()["robots"]["fakerobot"]["descriptor_endpoint"] == "/fakerobot/descriptor"
        # The page is handed a bare tunnel root and reads this cross-origin first.
        _assert_cors(r)
        _assert_cors(client.options("/"))

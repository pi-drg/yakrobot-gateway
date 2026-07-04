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

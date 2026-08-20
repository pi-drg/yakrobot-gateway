"""Implementation of the ``yakrobot`` management commands.

Dependency-light wrappers over the gateway's existing building blocks so the
``yakrobot-py`` console script and the legacy ``scripts/`` entrypoints share one
implementation. Nothing robot-specific lives here — it only orchestrates plugin
discovery, the FastAPI gateway, the tunnel, and the descriptor exporter.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Make the flat src/ modules (core, plugins) importable whether we're run as an
# installed console script (editable install → this file lives in src/yakrobot_cli/)
# or via the repo's scripts/ shims. Mirrors the sys.path hack the scripts use.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_plugins(robots=None):
    """Discover plugins and return {name: instance}, optionally filtered by name."""
    from plugins import discover_plugins

    all_plugins = discover_plugins()
    if robots:
        unknown = set(robots) - set(all_plugins)
        if unknown:
            raise SystemExit(
                f"Unknown robot(s): {sorted(unknown)}. Available: {sorted(all_plugins)}"
            )
        all_plugins = {k: v for k, v in all_plugins.items() if k in robots}
    return {name: cls() for name, cls in all_plugins.items()}


def list_robots():
    """Print the robot plugins available to this gateway."""
    plugins = _load_plugins()
    if not plugins:
        print("No robot plugins found. Check src/plugins/ for plugin packages.")
        return
    print(f"{len(plugins)} robot(s) available:")
    for name, plugin in plugins.items():
        meta = plugin.metadata()
        print(f"  {name:<12} {meta.name} ({len(plugin.tool_names())} tools)")


def serve_gateway(robots=None, port=8000, tunnel=None):
    """Run the gateway MCP server for the selected robots (blocking)."""
    import uvicorn

    from core.server import create_gateway

    plugins = _load_plugins(robots)
    if not plugins:
        raise SystemExit("No robot plugins found. Check src/plugins/ for plugin packages.")

    print(f"Loading {len(plugins)} robot(s): {', '.join(plugins)}")
    for name, plugin in plugins.items():
        meta = plugin.metadata()
        print(f"  /{name}/mcp — {meta.name} ({len(plugin.tool_names())} tools)")
    print("  /fleet/mcp  — Fleet orchestrator (local discovery)")

    app = create_gateway(plugins)

    if tunnel:
        from core.tunnel import start_tunnel

        public_url = start_tunnel(port, provider=tunnel)
        print(f"\n{tunnel} tunnel: {public_url}")
        print(f"  {public_url}/fleet/mcp")
        for name in plugins:
            print(f"  {public_url}/{name}/mcp")

    try:
        uvicorn.run(app, host="0.0.0.0", port=port)
    except KeyboardInterrupt:
        print("\nShutting down.")


def gateway_status(url="http://localhost:8000"):
    """Fetch and print a running gateway's index (mounted robots + reservations)."""
    endpoint = url.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Gateway not reachable at {url}: {exc}")
    print(json.dumps(data, indent=2))


def export_descriptor(robot, public_domain="", out=None, stdout=False):
    """Export a robot plugin's metadata as a JSON RobotDescriptor."""
    from core.descriptor import build_descriptor

    plugins = _load_plugins([robot])
    descriptor = build_descriptor(plugins[robot], public_domain)
    payload = descriptor.model_dump_json(indent=2)

    if stdout:
        print(payload)
        return

    out = out or str(_SRC.parent / "robot-descriptors" / f"{robot}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(payload + "\n")
    print(f"Wrote {os.path.relpath(out)}", file=sys.stderr)


SIMULATORS = {
    "fakerobot": ("plugins.fakerobot.simulator", 8080),
    "fakerobot_picar": ("plugins.fakerobot_picar.simulator", 8081),
}


def run_simulator(port=None, robot="fakerobot"):
    """Start a hardware-free robot simulator (blocking)."""
    import importlib

    import uvicorn

    if robot not in SIMULATORS:
        raise SystemExit(
            f"Unknown simulator {robot!r}. Available: {sorted(SIMULATORS)}"
        )
    module_path, default_port = SIMULATORS[robot]
    app = importlib.import_module(module_path).app
    port = default_port if port is None else port

    print(f"{robot} simulator on http://0.0.0.0:{port}")
    if robot == "fakerobot_picar":
        print(f"  ws://0.0.0.0:{port}/ws/control   realtime control")
        print(f"  ws://0.0.0.0:{port}/ws/video     JPEG frames")
    uvicorn.run(app, host="0.0.0.0", port=port)

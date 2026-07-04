"""
Start the MCP gateway for one or more robot plugins.

Usage:
    # Serve all discovered robot plugins
    uv run python scripts/serve.py

    # Serve only specific robots
    uv run python scripts/serve.py --robots fakerobot

    # With an ngrok tunnel (requires NGROK_AUTHTOKEN + NGROK_DOMAIN in .env)
    uv run python scripts/serve.py --robots fakerobot --tunnel ngrok

    # With a Cloudflare Tunnel (needs the `cloudflared` binary on PATH; set
    # CLOUDFLARE_TUNNEL_TOKEN + CLOUDFLARE_DOMAIN for a stable domain, else a
    # temporary *.trycloudflare.com URL)
    uv run python scripts/serve.py --robots fakerobot --tunnel cloudflare

    # Custom port
    uv run python scripts/serve.py --robots fakerobot --port 8001

Endpoints created:
    /fleet/mcp     — Fleet orchestrator (auction + local discovery)
    /{robot}/mcp   — Per-robot MCP server
    /              — Gateway info (lists all mounted robots)
"""

import argparse
import sys
import os

# Ensure src/ is on the path when run from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import uvicorn

from plugins import discover_plugins
from core.server import create_gateway

parser = argparse.ArgumentParser(description="Start the robot fleet MCP gateway")
parser.add_argument("--robots", nargs="*", help="Robot plugins to load (default: all)")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--tunnel", choices=["ngrok", "cloudflare"],
                    help="Expose the gateway over a public tunnel (default provider: TUNNEL_PROVIDER env)")
parser.add_argument("--ngrok", action="store_true", help="Deprecated alias for --tunnel ngrok")
args = parser.parse_args()

# Discover and filter plugins
all_plugins = discover_plugins()
if args.robots:
    unknown = set(args.robots) - set(all_plugins.keys())
    if unknown:
        print(f"Unknown robot(s): {unknown}. Available: {list(all_plugins.keys())}")
        sys.exit(1)
    selected = {k: v for k, v in all_plugins.items() if k in args.robots}
else:
    selected = all_plugins

if not selected:
    print("No robot plugins found. Check src/plugins/ for plugin packages.")
    sys.exit(1)

plugins = {name: cls() for name, cls in selected.items()}

print(f"Loading {len(plugins)} robot(s): {', '.join(plugins.keys())}")
for name, plugin in plugins.items():
    meta = plugin.metadata()
    print(f"  /{name}/mcp — {meta.name} ({len(plugin.tool_names())} tools)")
print(f"  /fleet/mcp  — Fleet orchestrator (auction + local discovery)")

app = create_gateway(plugins)

tunnel_provider = args.tunnel or ("ngrok" if args.ngrok else None)
if tunnel_provider:
    from core.tunnel import start_tunnel

    public_url = start_tunnel(args.port, provider=tunnel_provider)
    print(f"\n{tunnel_provider} tunnel: {public_url}")
    print(f"  {public_url}/fleet/mcp")
    for name in plugins:
        print(f"  {public_url}/{name}/mcp")

try:
    uvicorn.run(app, host="0.0.0.0", port=args.port)
except KeyboardInterrupt:
    print("\nShutting down.")

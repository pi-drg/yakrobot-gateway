"""Export a robot plugin's metadata as a JSON RobotDescriptor.

The descriptor is the cross-repo contract (yakrobot-descriptor). This gateway only
*produces* it; on-chain registration is performed in yakrobot-identity, fed this JSON.

Usage:
    # Write to robot-descriptors/<robot>.json (the default), resolving public endpoints
    # (--public-domain works for any tunnel: ngrok, Cloudflare, or your own host)
    uv run python scripts/export_descriptor.py tumbller --public-domain demo.ngrok.app

    # Override the output path, or print to stdout instead of writing a file
    uv run python scripts/export_descriptor.py tumbller --out /tmp/tumbller.json
    uv run python scripts/export_descriptor.py tumbller --stdout

Then, in yakrobot-identity:
    uv run python scripts/register.py --descriptor <path> --chain base-sepolia

Descriptors are generated artifacts (they bake in the public domain), so the default
output dir robot-descriptors/ is gitignored. The source of truth is the plugin's
metadata(); regenerate the JSON whenever you register.

Requires the `export` extra:  uv sync --extra export
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins import discover_plugins
from core.descriptor import build_descriptor

parser = argparse.ArgumentParser(description="Export a robot's JSON RobotDescriptor")
parser.add_argument("robot", help="Robot plugin name (e.g. tumbller)")
parser.add_argument("--public-domain", default="",
                    help="Public domain to resolve MCP/fleet endpoints, from any tunnel "
                         "(e.g. demo.ngrok.app, xyz.trycloudflare.com)")
parser.add_argument("--out", help="Output path (default: robot-descriptors/<robot>.json)")
parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of writing a file")
args = parser.parse_args()

plugins = discover_plugins()
if args.robot not in plugins:
    print(f"Unknown robot: {args.robot!r}. Available: {sorted(plugins)}", file=sys.stderr)
    sys.exit(1)

descriptor = build_descriptor(plugins[args.robot](), args.public_domain)
payload = descriptor.model_dump_json(indent=2)

if args.stdout:
    print(payload)
else:
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    out = args.out or os.path.join(repo_root, "robot-descriptors", f"{args.robot}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(payload + "\n")
    print(f"Wrote {os.path.relpath(out)}", file=sys.stderr)

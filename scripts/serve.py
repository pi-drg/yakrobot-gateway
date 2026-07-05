"""Start the MCP gateway for one or more robot plugins.

Kept for backward compatibility — prefer the `yakrobot-py` CLI, which shares this exact
implementation:

    yakrobot-py serve --robots fakerobot                # serve one robot
    yakrobot-py serve --robots tumbller --tunnel ngrok  # with an ngrok tunnel
    yakrobot-py serve --port 8001                       # custom port

This script forwards its arguments to `yakrobot-py serve`.

Endpoints created:
    /fleet/mcp     — Fleet orchestrator (local discovery)
    /{robot}/mcp   — Per-robot MCP server
    /              — Gateway info (lists all mounted robots)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yakrobot_cli import main

main(["serve", *sys.argv[1:]])

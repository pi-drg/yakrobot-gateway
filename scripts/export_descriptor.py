"""Export a robot plugin's metadata as a JSON RobotDescriptor.

Kept for backward compatibility — prefer the `yakrobot-py` CLI, which shares this exact
implementation:

    yakrobot-py export tumbller --public-domain demo.ngrok.app
    yakrobot-py export tumbller --out /tmp/tumbller.json
    yakrobot-py export tumbller --stdout

The descriptor is the cross-repo contract (yakrobot-descriptor). This gateway only
*produces* it; on-chain registration is performed in yakrobot-identity, fed this JSON.
Requires the `export` extra:  uv sync --extra export

This script forwards its arguments to `yakrobot export`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yakrobot_cli import main

main(["export", *sys.argv[1:]])

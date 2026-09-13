#!/usr/bin/env python3
"""Mint a paid-teleop capability locally, to exercise a running gateway without a real
payments service.

**Test/dev tool only.** The payments service is the sole mint authority in production —
the gateway only ever verifies (paid-teleop-access.md §1.1) — so this exists purely to
let a developer drive `/{robot}/ws/*` with `PAYMENTS_ENABLED=1` before that service
exists or is reachable.

Usage:

    uv run python tests/tools/mint_capability.py \\
        --robot fakerobot_picar --gateway 127.0.0.1:8192 --minutes 5 \\
        --key-file /path/to/issuer.key

Prints the token, then a ready-to-open console URL. The private key is read from a
**file**, never a command-line argument — an argument sits in shell history, `ps`
output and this process's argv for any local user to read.
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent
_SRC_DIR = _TESTS_DIR.parent / "src"
for path in (_SRC_DIR, _TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from capability_helper import mint  # noqa: E402
from core.capability import normalize_host  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--robot", required=True, help="Robot name, e.g. fakerobot_picar")
    parser.add_argument(
        "--gateway", required=True,
        help="Gateway host as the browser reaches it, e.g. 127.0.0.1:8192 or x.ngrok-free.dev",
    )
    parser.add_argument("--minutes", type=int, default=5, help="Lease length (default: 5)")
    parser.add_argument(
        "--key-file", required=True,
        help="File containing the issuer's private key (0x + 64 hex), matching PAYMENTS_ISSUER",
    )
    parser.add_argument(
        "--payer", default=None,
        help="0x address to record as payer (default: the issuer's own address)",
    )
    args = parser.parse_args()

    private_key = Path(args.key_file).read_text().strip()

    from eth_keys import keys

    pk = keys.PrivateKey(bytes.fromhex(private_key.removeprefix("0x")))
    payer = (args.payer or pk.public_key.to_checksum_address()).lower()
    gateway = normalize_host(args.gateway)

    now = int(time.time())
    claims = {
        "v": 1,
        "robot": args.robot,
        "gateway": gateway,
        "lease": str(uuid.uuid4()),
        "payer": payer,
        "iat": now,
        "exp": now + args.minutes * 60,
    }
    token = mint(claims, private_key)

    print(token)
    print(f"http://{gateway}/{args.robot}/ui#token={token}")


if __name__ == "__main__":
    main()

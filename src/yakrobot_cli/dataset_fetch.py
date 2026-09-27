"""``yakrobot-py dataset fetch`` — download a dataset from presigned links.

``--links URL`` GETs a §0.11 r2 success body and downloads every file, checking ``bytes``
and ``sha256`` (deleting a file on any mismatch). ``--redeem URL`` first POSTs a v2
capability plus an EIP-191 proof signed by the buyer's wallet key (read from an env var,
never a CLI argument), then continues as ``--links``.

Imports httpx/hashlib, and eth_keys only on the ``--redeem`` path — nothing from
``core.server`` or lerobot, so this runs under ``uvx --from 'yakrobot-gateway[fetch]'``
(or ``[fetch,payments]`` for redeem).
"""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _token_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 2:
        raise SystemExit("token is not a capability")
    payload = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
    try:
        claims = json.loads(payload)
    except ValueError:
        raise SystemExit("token payload is not JSON") from None
    if not isinstance(claims, dict):
        raise SystemExit("token payload is not an object")
    return claims


def _redeem_links(redeem_url: str, token: str, wallet_key_env: str) -> dict:
    import httpx
    from eth_hash.auto import keccak
    from eth_keys import keys

    key_hex = os.environ.get(wallet_key_env, "").strip()
    if not key_hex:
        raise SystemExit(f"{wallet_key_env} is not set")
    claims = _token_claims(token)
    ts = int(time.time())
    message = f"yakrobot-redeem:{claims['sale']}:{claims['gateway']}:{ts}".encode()
    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message
    private = keys.PrivateKey(bytes.fromhex(key_hex.replace("0x", "")))
    signature = private.sign_msg_hash(keccak(prefixed))
    proof = "0x" + (signature.to_bytes() + bytes([signature.v + 27])).hex()

    resp = httpx.post(redeem_url, json={"token": token, "proof": proof, "ts": ts})
    if resp.status_code != 200:
        raise SystemExit(f"redeem failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def fetch(links=None, redeem=None, token=None, wallet_key_env=None, out: str = ""):
    """Download a dataset. ``links`` or ``redeem`` must be given."""
    import httpx

    if links:
        data = httpx.get(links).json()
    elif redeem:
        if not token or not wallet_key_env:
            raise SystemExit("--redeem needs --token and --wallet-key-env")
        data = _redeem_links(redeem, token, wallet_key_env)
    else:
        raise SystemExit("give --links URL or --redeem URL --token T --wallet-key-env VAR")

    if data.get("store") == "hf":
        print(data.get("load", ""))
        return

    files = data.get("files") or []
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in files:
        resp = httpx.get(entry["url"])
        if resp.status_code != 200:
            raise SystemExit(f"download of {entry['path']} failed: HTTP {resp.status_code}")
        body = resp.content
        if len(body) != entry["bytes"]:
            raise SystemExit(f"size mismatch for {entry['path']}: {len(body)} != {entry['bytes']}")
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise SystemExit(f"sha256 mismatch for {entry['path']}")
        target = out_dir / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    print(f"LeRobotDataset(repo_id={out_dir.name}, root={out_dir})")

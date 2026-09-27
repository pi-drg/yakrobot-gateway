"""Tests for the redeem route (execution plan §0.11, step 5.3)."""

import asyncio
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capability_helper import mint  # noqa: E402
from yakrobot_cli.dataset_publish import build_manifest  # noqa: E402

ISSUER = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
ISSUER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PAYER = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
PAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

_CLEAR = (
    "DATASETS_FILE", "DATASET_REDEEM_DAYS", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_URL_TTL_S", "RECORDING_ENABLED", "RECORDINGS_DIR",
    "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
    "STRIPE_GATE_ENABLED", "STRIPE_SECRET_KEY", "TELEOP_LEASE_MINUTES",
    "TELEOP_PRICE_USDC", "VIDEO_ENABLED", "MCP_TOKENS", "NGROK_DOMAIN", "CLOUDFLARE_DOMAIN",
)


def _serve(app) -> tuple[int, object]:
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

    def run() -> None:
        asyncio.run(server.serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return port, server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def _sign(message: bytes, private_key_hex: str) -> str:
    from eth_hash.auto import keccak
    from eth_keys import keys

    pk = keys.PrivateKey(bytes.fromhex(private_key_hex.replace("0x", "")))
    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message
    sig_bytes = pk.sign_msg_hash(keccak(prefixed)).to_bytes()
    return "0x" + (sig_bytes[:64] + bytes([sig_bytes[64] + 27])).hex()


def _proof(sale: str, gateway: str, ts: int, private_key_hex: str = PAYER_KEY) -> str:
    return _sign(f"yakrobot-redeem:{sale}:{gateway}:{ts}".encode(), private_key_hex)


def _claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "v": 2, "kind": "dataset", "gateway": "testserver", "robot": "fakerobot_picar",
        "dataset": "picar_hf_v1", "rev": "a" * 40,
        "recipient": "hf:alice", "sale": "00000000-0000-4000-8000-000000000002",
        "payer": PAYER, "iat": now - 100, "exp": now + 86400,
    }
    base.update(overrides)
    return base


_HF_TOML = (
    '[[dataset]]\n'
    'id = "picar_hf_v1"\n'
    'robot = "fakerobot_picar"\n'
    'store = "hf"\n'
    'hub_repo = "myname/picar_hf_v1"\n'
    'rev = "' + "a" * 40 + '"\n'
    'episodes = 1\n'
    'frames = 1\n'
    'license = "cc-by-nc-4.0"\n'
    'source = "gateway"\n'
)


@pytest.fixture(autouse=True)
def _clear_caches():
    # The manifest cache is process-lifetime (correct in production), but tests reuse
    # the same rev across runs — clear it so a corrupted object is actually re-fetched.
    import core.datasets_routes as dr

    dr._manifest_cache.clear()
    yield


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    from tools.fake_r2 import FakeR2State, create_fake_r2

    r2_state = FakeR2State()
    r2_state.keys["read"] = "rsecret"
    r2_state.keys["write"] = "wsecret"
    r2_state.read_only.add("write")
    r2_port, _ = _serve(create_fake_r2(r2_state))

    def build(toml_text: str, *, hf_grant=None, payments: bool = True, **env):
        from core.server import create_gateway
        from yakrobot_cli.commands import _load_plugins

        for var in _CLEAR:
            monkeypatch.delenv(var, raising=False)
        datasets_file = tmp_path / "datasets.toml"
        datasets_file.write_text(toml_text)
        monkeypatch.setenv("DATASETS_FILE", str(datasets_file))
        monkeypatch.setenv("FAKEROBOT_PICAR_URL", "http://127.0.0.1:8191")
        monkeypatch.setenv("NGROK_DOMAIN", "")
        monkeypatch.setenv("CLOUDFLARE_DOMAIN", "")
        monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
        monkeypatch.setenv("R2_BUCKET", "bucket")
        monkeypatch.setenv("R2_ENDPOINT", f"http://127.0.0.1:{r2_port}")
        monkeypatch.setenv("R2_ACCESS_KEY_ID", "read")
        monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "rsecret")
        if payments:
            monkeypatch.setenv("PAYMENTS_ENABLED", "1")
            monkeypatch.setenv("PAYMENTS_URL", "http://127.0.0.1:8001")
            monkeypatch.setenv("PAYMENTS_ISSUER", ISSUER)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        app = create_gateway(_load_plugins(["fakerobot_picar"]))
        if hf_grant is not None:
            app.state.hf_grant = hf_grant
        return app

    return {"build": build, "r2": r2_state}


def _seed_r2(r2_state, dataset_id: str) -> str:
    files = [("meta/info.json", b'{"codebase_version":"v3.0"}'),
             ("videos/chunk-000/file-000.mp4", b"\x00\x00\x00\x18ftypmp42")]
    rev, manifest = build_manifest(dataset_id, files)
    for path, data in files:
        r2_state.objects[f"datasets/{dataset_id}/{rev}/{path}"] = data
    r2_state.objects[f"datasets/{dataset_id}/{rev}/manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    return rev


def _redeem(client, robot, dataset_id, **body):
    return client.post(f"/{robot}/datasets/{dataset_id}/redeem", json=body)


def test_redeem_not_enabled(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x", payments=False)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token="x")
    assert r.status_code == 404
    assert r.json()["code"] == "not_enabled"


def test_redeem_invalid_capability(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token="not-a-token")
    assert r.status_code == 403
    assert r.json()["code"] == "invalid_capability"


def test_redeem_wrong_gateway(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    token = mint(_claims(gateway="evil.example"), ISSUER_KEY)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
    assert r.status_code == 403
    assert r.json()["code"] == "wrong_gateway"


def test_redeem_no_such_dataset(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    token = mint(_claims(dataset="nope_v1"), ISSUER_KEY)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
    assert r.status_code == 404
    assert r.json()["code"] == "no_such_dataset"


def test_redeem_relisted(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    token = mint(_claims(rev="b" * 40), ISSUER_KEY)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
    assert r.status_code == 409
    assert r.json()["code"] == "relisted"


def test_redeem_expired(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    now = int(time.time())
    token = mint(_claims(iat=now - 200, exp=now - 100), ISSUER_KEY)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
    assert r.status_code == 403
    assert r.json()["code"] == "expired"


def test_hf_redeem_grants_recipient(ctx):
    from starlette.testclient import TestClient

    granted = []

    async def grant(repo, user):
        granted.append((repo, user))

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x", hf_grant=grant)
    token = mint(_claims(), ISSUER_KEY)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
    assert r.status_code == 200
    body = r.json()
    assert body["store"] == "hf" and body["granted"] is True
    assert granted == [("myname/picar_hf_v1", "alice")]


def test_redeem_preflight_has_cors(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        r = client.options("/fakerobot_picar/datasets/picar_hf_v1/redeem")
    assert r.status_code == 204
    assert r.headers["access-control-allow-methods"] == "POST, OPTIONS"


def test_redeem_never_reserves_robot(ctx):
    from starlette.testclient import TestClient

    granted = []
    async def grant(repo, user):
        granted.append(user)

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x", hf_grant=grant)
    token = mint(_claims(), ISSUER_KEY)
    with TestClient(app) as client:
        before = client.get("/").json()["robots"]["fakerobot_picar"]["reservation"]
        r = _redeem(client, "fakerobot_picar", "picar_hf_v1", token=token)
        after = client.get("/").json()["robots"]["fakerobot_picar"]["reservation"]
    assert r.status_code == 200
    assert before == after


# --- r2 redeem ----------------------------------------------------------------

_R2_TOML = (
    '[[dataset]]\n'
    'id = "picar_r2_v1"\n'
    'robot = "fakerobot_picar"\n'
    'store = "r2"\n'
    'rev = "{rev}"\n'
    'episodes = 1\n'
    'frames = 1\n'
    'license = "cc-by-nc-4.0"\n'
    'source = "gateway"\n'
)


def test_r2_redeem_returns_links(ctx):
    from starlette.testclient import TestClient

    rev = _seed_r2(ctx["r2"], "picar_r2_v1")
    app = ctx["build"](_R2_TOML.format(rev=rev))
    now = int(time.time())
    claims = _claims(dataset="picar_r2_v1", rev=rev, recipient=f"wallet:{PAYER}")
    token = mint(claims, ISSUER_KEY)
    proof = _proof(claims["sale"], claims["gateway"], now)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_r2_v1", token=token, proof=proof, ts=now)
    assert r.status_code == 200
    body = r.json()
    assert body["store"] == "r2"
    assert len(body["files"]) == 2


def test_r2_redeem_rejects_stale_proof(ctx):
    from starlette.testclient import TestClient

    rev = _seed_r2(ctx["r2"], "picar_r2_v1")
    app = ctx["build"](_R2_TOML.format(rev=rev))
    now = int(time.time())
    claims = _claims(dataset="picar_r2_v1", rev=rev, recipient=f"wallet:{PAYER}")
    token = mint(claims, ISSUER_KEY)
    proof = _proof(claims["sale"], claims["gateway"], now - 1000)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_r2_v1", token=token, proof=proof, ts=now - 1000)
    assert r.status_code == 403
    assert r.json()["code"] == "proof_required"


def test_r2_redeem_rejects_proof_from_other_wallet(ctx):
    from starlette.testclient import TestClient

    rev = _seed_r2(ctx["r2"], "picar_r2_v1")
    app = ctx["build"](_R2_TOML.format(rev=rev))
    now = int(time.time())
    claims = _claims(dataset="picar_r2_v1", rev=rev, recipient=f"wallet:{PAYER}")
    token = mint(claims, ISSUER_KEY)
    # A different well-known Hardhat key (account #2) signs the proof.
    other_key = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
    proof = _proof(claims["sale"], claims["gateway"], now, private_key_hex=other_key)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_r2_v1", token=token, proof=proof, ts=now)
    assert r.status_code == 403
    assert r.json()["code"] == "proof_required"


def test_r2_manifest_hash_mismatch_is_502(ctx):
    from starlette.testclient import TestClient

    rev = _seed_r2(ctx["r2"], "picar_r2_v1")
    # Corrupt the stored manifest so its sha256 no longer equals rev.
    for key in list(ctx["r2"].objects):
        if key.endswith("/manifest.json"):
            ctx["r2"].objects[key] = b"corrupted"
    app = ctx["build"](_R2_TOML.format(rev=rev))
    now = int(time.time())
    claims = _claims(dataset="picar_r2_v1", rev=rev, recipient=f"wallet:{PAYER}")
    token = mint(claims, ISSUER_KEY)
    proof = _proof(claims["sale"], claims["gateway"], now)
    with TestClient(app) as client:
        r = _redeem(client, "fakerobot_picar", "picar_r2_v1", token=token, proof=proof, ts=now)
    assert r.status_code == 502
    assert r.json()["code"] == "store_unavailable"

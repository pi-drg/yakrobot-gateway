"""Tests for the dataset card sale (execution plan §0.12, step 4.1)."""

import asyncio
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yakrobot_cli.dataset_publish import build_manifest  # noqa: E402

_CLEAR = (
    "DATASETS_FILE", "DATASET_REDEEM_DAYS", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_URL_TTL_S", "RECORDING_ENABLED", "RECORDINGS_DIR",
    "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
    "STRIPE_GATE_ENABLED", "STRIPE_SECRET_KEY", "STRIPE_PRICE_CENTS", "STRIPE_CURRENCY",
    "STRIPE_API_BASE", "STRIPE_AUTOMATIC_TAX", "STRIPE_TAX_CODE", "TELEOP_LEASE_MINUTES",
    "TELEOP_PRICE_USDC", "VIDEO_ENABLED", "MCP_TOKENS",
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


_HF_TOML = (
    '[[dataset]]\n'
    'id = "picar_hf_v1"\n'
    'robot = "fakerobot_picar"\n'
    'store = "hf"\n'
    'hub_repo = "myname/picar_hf_v1"\n'
    'rev = "' + "a" * 40 + '"\n'
    'episodes = 4\n'
    'frames = 100\n'
    'price_usdc = "5.00"\n'
    'price_cents = 500\n'
    'currency = "usd"\n'
    'license = "cc-by-nc-4.0"\n'
    'source = "gateway"\n'
)


@pytest.fixture(autouse=True)
def _clear_caches():
    # The fake Stripe reuses cs_test_<n> ids across tests, while the routes' caches are
    # process-lifetime (correct in production, colliding here) — clear them each test.
    import core.datasets_routes as dr

    dr._confirm_cache.clear()
    dr._manifest_cache.clear()
    yield


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    from tools.fake_r2 import FakeR2State, create_fake_r2
    from tools.fake_stripe import StripeState, create_fake_stripe

    stripe_state = StripeState()
    stripe_port, _ = _serve(create_fake_stripe(stripe_state))
    r2_state = FakeR2State()
    r2_state.keys["read"] = "rsecret"
    r2_state.keys["write"] = "wsecret"
    r2_state.read_only.add("write")
    r2_port, _ = _serve(create_fake_r2(r2_state))

    def build(toml_text: str, **env):
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
        monkeypatch.setenv("STRIPE_GATE_ENABLED", "1")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
        monkeypatch.setenv("STRIPE_PRICE_CENTS", "100")
        monkeypatch.setenv("TELEOP_LEASE_MINUTES", "5")
        monkeypatch.setenv("STRIPE_API_BASE", f"http://127.0.0.1:{stripe_port}")
        monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
        monkeypatch.setenv("R2_BUCKET", "bucket")
        monkeypatch.setenv("R2_ENDPOINT", f"http://127.0.0.1:{r2_port}")
        monkeypatch.setenv("R2_ACCESS_KEY_ID", "read")
        monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "rsecret")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return create_gateway(_load_plugins(["fakerobot_picar"]))

    return {"build": build, "stripe": stripe_state, "r2": r2_state}


def _seed_r2(r2_state, dataset_id: str, files) -> str:
    rev, manifest = build_manifest(dataset_id, files)
    for path, data in files:
        r2_state.objects[f"datasets/{dataset_id}/{rev}/{path}"] = data
    r2_state.objects[f"datasets/{dataset_id}/{rev}/manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    return rev


_R2_TOML = (
    '[[dataset]]\n'
    'id = "picar_r2_v1"\n'
    'robot = "fakerobot_picar"\n'
    'store = "r2"\n'
    'rev = "{rev}"\n'
    'episodes = 1\n'
    'frames = 1\n'
    'price_cents = 500\n'
    'currency = "usd"\n'
    'license = "cc-by-nc-4.0"\n'
    'source = "gateway"\n'
)


def _sid(location: str) -> str:
    return parse_qs(urlparse(location).query)["session_id"][0]


def test_start_hf_asks_for_username(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        r = client.get("/fakerobot_picar/datasets/picar_hf_v1/stripe/start")
    assert r.status_code == 200
    assert 'name="hf_user"' in r.text


def test_start_creates_session_with_dataset_metadata(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        r = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/start?hf_user=alice",
            follow_redirects=False,
        )
    assert r.status_code == 303
    form = ctx["stripe"].created_forms[0]
    assert form["metadata[dataset]"] == "picar_hf_v1"
    assert form["metadata[recipient]"] == "hf:alice"
    assert form["line_items[0][price_data][unit_amount]"] == "500"


def test_confirm_hf_grants_named_user(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    granted = []

    async def grant(repo, user):
        granted.append((repo, user))

    app.state.hf_grant = grant
    with TestClient(app) as client:
        start = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/start?hf_user=alice",
            follow_redirects=False,
        )
        confirm = client.get(
            f"/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id={_sid(start.headers['location'])}"
        )
    assert confirm.status_code == 200
    assert granted == [("myname/picar_hf_v1", "alice")]


def test_confirm_r2_returns_presigned_links_json(ctx):
    from starlette.testclient import TestClient

    rev = _seed_r2(ctx["r2"], "picar_r2_v1", [
        ("meta/info.json", b'{"codebase_version":"v3.0"}'),
        ("videos/chunk-000/file-000.mp4", b"\x00\x00\x00\x18ftypmp42"),
    ])
    app = ctx["build"](_R2_TOML.format(rev=rev))
    with TestClient(app) as client:
        start = client.get(
            "/fakerobot_picar/datasets/picar_r2_v1/stripe/start", follow_redirects=False
        )
        confirm = client.get(
            f"/fakerobot_picar/datasets/picar_r2_v1/stripe/confirm"
            f"?session_id={_sid(start.headers['location'])}&format=json"
        )
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["store"] == "r2"
    assert len(body["files"]) == 2
    assert all(f["url"].startswith(f"http://127.0.0.1") for f in body["files"])


def test_confirm_refuses_wrong_amount(ctx):
    from starlette.testclient import TestClient

    from tools.fake_stripe import paid_session

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    ctx["stripe"].sessions["cs_test_1"] = paid_session(
        "cs_test_1", "fakerobot_picar", amount_total=1, currency="usd",
        gateway="testserver",
        metadata={"robot": "fakerobot_picar", "gateway": "testserver",
                  "dataset": "picar_hf_v1", "rev": "a" * 40, "recipient": "hf:alice"},
    )
    with TestClient(app) as client:
        r = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id=cs_test_1"
        )
    assert r.status_code == 409


def test_confirm_refuses_wrong_gateway(ctx):
    from starlette.testclient import TestClient

    from tools.fake_stripe import paid_session

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    ctx["stripe"].sessions["cs_test_1"] = paid_session(
        "cs_test_1", "fakerobot_picar", amount_total=500, currency="usd",
        gateway="evil.example",
        metadata={"robot": "fakerobot_picar", "gateway": "evil.example",
                  "dataset": "picar_hf_v1", "rev": "a" * 40, "recipient": "hf:alice"},
    )
    with TestClient(app) as client:
        r = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id=cs_test_1"
        )
    assert r.status_code == 400


def test_confirm_refuses_wrong_rev(ctx):
    from starlette.testclient import TestClient

    from tools.fake_stripe import paid_session

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    ctx["stripe"].sessions["cs_test_1"] = paid_session(
        "cs_test_1", "fakerobot_picar", amount_total=500, currency="usd",
        gateway="testserver",
        metadata={"robot": "fakerobot_picar", "gateway": "testserver",
                  "dataset": "picar_hf_v1", "rev": "b" * 40, "recipient": "hf:alice"},
    )
    with TestClient(app) as client:
        r = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id=cs_test_1"
        )
    assert r.status_code == 409


def test_confirm_after_window_is_410(ctx):
    from starlette.testclient import TestClient

    from tools.fake_stripe import paid_session

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    async def grant(repo, user):
        pass

    app.state.hf_grant = grant
    ctx["stripe"].sessions["cs_test_1"] = paid_session(
        "cs_test_1", "fakerobot_picar", amount_total=500, currency="usd",
        gateway="testserver", created=int(time.time()) - 31 * 86400,
        metadata={"robot": "fakerobot_picar", "gateway": "testserver",
                  "dataset": "picar_hf_v1", "rev": "a" * 40, "recipient": "hf:alice"},
    )
    with TestClient(app) as client:
        first = client.get("/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id=cs_test_1")
        second = client.get("/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id=cs_test_1")
    assert first.status_code == 200
    assert second.status_code == 410


def test_confirm_revisit_reissues_links(ctx):
    from starlette.testclient import TestClient

    app = ctx["build"](_HF_TOML, HF_TOKEN="hf_x")
    calls = []
    async def grant(repo, user):
        calls.append(user)

    app.state.hf_grant = grant
    with TestClient(app) as client:
        start = client.get(
            "/fakerobot_picar/datasets/picar_hf_v1/stripe/start?hf_user=alice",
            follow_redirects=False,
        )
        sid = _sid(start.headers["location"])
        first = client.get(f"/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id={sid}")
        second = client.get(f"/fakerobot_picar/datasets/picar_hf_v1/stripe/confirm?session_id={sid}")
    assert first.status_code == 200 and second.status_code == 200
    assert calls == ["alice", "alice"]
    # Revisited through the cache: Stripe was retrieved exactly once.
    assert ctx["stripe"].retrieve_calls[sid] == 1


def test_disabled_without_price_cents(ctx):
    from starlette.testclient import TestClient

    toml = _HF_TOML.replace('price_cents = 500\n', "").replace('price_usdc = "5.00"\n', "")
    app = ctx["build"](toml, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        r = client.get("/fakerobot_picar/datasets/picar_hf_v1/stripe/start")
    assert r.status_code == 404

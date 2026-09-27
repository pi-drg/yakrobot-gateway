"""Tests for the dataset preview route (execution plan step 3.4)."""

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_DATASET_ENV = (
    "DATASETS_FILE", "DATASET_REDEEM_DAYS", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_URL_TTL_S",
    "RECORDING_ENABLED", "RECORDINGS_DIR", "PAYMENTS_ENABLED", "STRIPE_ENABLED",
)


def _gateway(tmp_path: Path, monkeypatch, toml_text: str, **env):
    for var in _DATASET_ENV:
        monkeypatch.delenv(var, raising=False)
    datasets_file = tmp_path / "datasets.toml"
    datasets_file.write_text(toml_text)
    monkeypatch.setenv("DATASETS_FILE", str(datasets_file))
    monkeypatch.setenv("FAKEROBOT_PICAR_URL", "http://127.0.0.1:8191")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from core.server import create_gateway
    from yakrobot_cli.commands import _load_plugins

    return create_gateway(_load_plugins(["fakerobot_picar"]))


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

_R2_TOML = (
    '[[dataset]]\n'
    'id = "picar_r2_v1"\n'
    'robot = "fakerobot_picar"\n'
    'store = "r2"\n'
    'rev = "' + "b" * 64 + '"\n'
    'episodes = 1\n'
    'frames = 1\n'
    'license = "cc-by-nc-4.0"\n'
    'source = "gateway"\n'
)


def test_preview_hf_redirects_to_card(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app = _gateway(tmp_path, monkeypatch, _HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        resp = client.get("/fakerobot_picar/datasets/picar_hf_v1/preview", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://huggingface.co/datasets/myname/picar_hf_v1"


def test_preview_r2_redirects_to_presigned(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app = _gateway(
        tmp_path, monkeypatch, _R2_TOML,
        R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
        R2_ENDPOINT="https://acct.r2.cloudflarestorage.com",
        R2_ACCESS_KEY_ID="read", R2_SECRET_ACCESS_KEY="rsecret",
    )
    with TestClient(app) as client:
        resp = client.get("/fakerobot_picar/datasets/picar_r2_v1/preview", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://acct.r2.cloudflarestorage.com/bucket/datasets/picar_r2_v1/")
    assert "/preview/README.md" in location
    assert "X-Amz-Signature=" in location


def test_preview_rejects_unknown_file(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app = _gateway(tmp_path, monkeypatch, _HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        resp = client.get("/fakerobot_picar/datasets/picar_hf_v1/preview/thumb_9.jpg")
    assert resp.status_code == 404


def test_preview_rejects_unknown_dataset(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app = _gateway(tmp_path, monkeypatch, _HF_TOML, HF_TOKEN="hf_x")
    with TestClient(app) as client:
        resp = client.get("/fakerobot_picar/datasets/nope/preview")
    assert resp.status_code == 404

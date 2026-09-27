"""Unit tests for core.datasets_listing — the DATASETS_FILE catalogue (§0.6)."""

import os
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.datasets_listing import DatasetListings  # noqa: E402

PLUGINS = {"fakerobot_picar", "fakerobot"}

HF_ENTRY = {
    "id": "picar_red_box_v1",
    "robot": "fakerobot_picar",
    "store": "hf",
    "hub_repo": "myname/picar_red_box_v1",
    "rev": "a" * 40,
    "episodes": 42,
    "frames": 18900,
    "price_usdc": "5.00",
    "price_cents": 500,
    "currency": "usd",
    "license": "cc-by-nc-4.0",
    "source": "gateway",
}

R2_ENTRY = {
    "id": "picar_r2_v1",
    "robot": "fakerobot_picar",
    "store": "r2",
    "rev": "b" * 64,
    "episodes": 10,
    "frames": 1000,
    "license": "cc-by-nc-4.0",
    "source": "lelab",
}

_ENV = (
    "DATASETS_FILE", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_URL_TTL_S", "R2_ENDPOINT",
)


def _clear_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


def _write_toml(tmp_path, *entries):
    lines = []
    for e in entries:
        lines.append("[[dataset]]")
        for k, v in e.items():
            lines.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
    path = tmp_path / "datasets.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")


def test_valid_listing_loads(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    path = _write_toml(tmp_path, HF_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    got = listings.get("fakerobot_picar", "picar_red_box_v1")
    assert got is not None
    assert got.hub_repo == "myname/picar_red_box_v1"
    assert got.currency == "usd"
    assert got.price_cents == 500


def test_bad_entry_skipped_others_kept(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    bad = {**HF_ENTRY, "id": "bad-id!", "rev": "zzz"}
    path = _write_toml(tmp_path, bad, HF_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    assert listings.get("fakerobot_picar", "bad-id!") is None
    assert listings.get("fakerobot_picar", "picar_red_box_v1") is not None


def test_bad_toml_means_empty(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = tmp_path / "datasets.toml"
    path.write_text("this is not [valid toml\n")
    listings = DatasetListings(str(path), PLUGINS)
    assert listings.for_robot("fakerobot_picar") == []


def test_hot_reload_on_mtime(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    path = _write_toml(tmp_path, HF_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    assert [l.id for l in listings.for_robot("fakerobot_picar")] == ["picar_red_box_v1"]

    other = {**HF_ENTRY, "id": "picar_red_box_v2"}
    path.write_text("\n".join(["[[dataset]]"] + [
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in other.items()
    ]) + "\n")
    os.utime(path, (time.time() + 5, time.time() + 5))  # force an mtime change
    assert [l.id for l in listings.for_robot("fakerobot_picar")] == ["picar_red_box_v2"]


def test_missing_file_means_no_listings(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    listings = DatasetListings(str(tmp_path / "nope.toml"), PLUGINS)
    assert listings.for_robot("fakerobot_picar") == []


def test_hf_listing_skipped_without_token(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _write_toml(tmp_path, HF_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    assert listings.get("fakerobot_picar", "picar_red_box_v1") is None


def test_r2_listing_skipped_without_r2(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _write_toml(tmp_path, R2_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    assert listings.get("fakerobot_picar", "picar_r2_v1") is None


def test_r2_listing_loads_with_r2_configured(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _r2_env(monkeypatch)
    path = _write_toml(tmp_path, R2_ENTRY)
    listings = DatasetListings(str(path), PLUGINS)
    got = listings.get("fakerobot_picar", "picar_r2_v1")
    assert got is not None
    assert got.store == "r2"
    assert got.index_entry()["preview"] == "/fakerobot_picar/datasets/picar_r2_v1/preview"


def test_unknown_robot_skipped(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    entry = {**HF_ENTRY, "robot": "tello"}
    path = _write_toml(tmp_path, entry)
    listings = DatasetListings(str(path), PLUGINS)
    assert listings.for_robot("tello") == []


def test_index_lists_datasets(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    path = _write_toml(tmp_path, HF_ENTRY)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DATASETS_FILE", str(path))
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("FAKEROBOT_PICAR_URL", "http://127.0.0.1:8191")

    from core.server import create_gateway
    from yakrobot_cli.commands import _load_plugins

    app = create_gateway(_load_plugins(["fakerobot_picar"]))
    with TestClient(app) as client:
        index = client.get("/").json()

    datasets = index["robots"]["fakerobot_picar"]["datasets"]
    assert len(datasets) == 1
    entry = datasets[0]
    assert entry["id"] == "picar_red_box_v1"
    assert entry["preview"] == "https://huggingface.co/datasets/myname/picar_red_box_v1"
    assert index["datasets"] == {"redeem_days": 30}

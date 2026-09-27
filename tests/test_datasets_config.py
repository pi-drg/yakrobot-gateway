"""Unit tests for core.datasets_config — no servers, no network.

Every test sets and restores env via monkeypatch, per the execution plan §0.1/step 1.1.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.datasets_config import (  # noqa: E402
    DatasetsConfigError,
    hf_token,
    load_recording_config,
    load_redeem_days,
    load_r2_config,
)

_RECORDING_VARS = (
    "RECORDING_ENABLED", "RECORDINGS_DIR", "RECORDINGS_MAX_GB",
    "RECORDING_QUEUE_FRAMES", "VIDEO_ENABLED",
    "DATASET_REDEEM_DAYS", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_URL_TTL_S", "R2_ENDPOINT",
)


def _set(monkeypatch, **env):
    for key in _RECORDING_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# --- RECORDING_* ---------------------------------------------------------------


def test_recording_off_by_default(monkeypatch):
    _set(monkeypatch)
    cfg = load_recording_config()
    assert cfg.enabled is False
    assert cfg.disabled_reason is None
    assert cfg.max_bytes == 20 * 1024 ** 3
    assert cfg.queue_frames == 256
    assert cfg.dir == Path.home() / ".cache" / "yakrobot" / "recordings"


def test_recording_disabled_when_video_off(monkeypatch):
    _set(monkeypatch, RECORDING_ENABLED="1", VIDEO_ENABLED="0")
    cfg = load_recording_config()
    assert cfg.enabled is False
    assert cfg.disabled_reason == "video disabled"


def test_recording_enabled_with_defaults(monkeypatch):
    _set(monkeypatch, RECORDING_ENABLED="true")
    cfg = load_recording_config()
    assert cfg.enabled is True
    assert cfg.disabled_reason is None
    assert cfg.max_bytes == 20 * 1024 ** 3
    assert cfg.queue_frames == 256


def test_recording_dir_expands_tilde(monkeypatch):
    _set(monkeypatch, RECORDING_ENABLED="1", RECORDINGS_DIR="~/recordings")
    assert load_recording_config().dir == Path.home() / "recordings"


def test_max_gb_rejects_non_positive(monkeypatch):
    for value in ("0", "-1", "abc", ""):
        _set(monkeypatch, RECORDING_ENABLED="1", RECORDINGS_MAX_GB=value)
        try:
            load_recording_config()
            raise AssertionError(f"expected rejection for max_gb {value!r}")
        except DatasetsConfigError as exc:
            assert "RECORDINGS_MAX_GB" in str(exc)


def test_max_gb_accepts_float(monkeypatch):
    _set(monkeypatch, RECORDING_ENABLED="1", RECORDINGS_MAX_GB="0.5")
    assert load_recording_config().max_bytes == 512 * 1024 ** 2


def test_queue_frames_bounds(monkeypatch):
    for value in ("15", "65537", "0", "abc"):
        _set(monkeypatch, RECORDING_ENABLED="1", RECORDING_QUEUE_FRAMES=value)
        try:
            load_recording_config()
            raise AssertionError(f"expected rejection for queue_frames {value!r}")
        except DatasetsConfigError as exc:
            assert "RECORDING_QUEUE_FRAMES" in str(exc)

    for value in ("16", "65536"):
        _set(monkeypatch, RECORDING_ENABLED="1", RECORDING_QUEUE_FRAMES=value)
        assert load_recording_config().queue_frames == int(value)


# --- DATASET_REDEEM_DAYS -------------------------------------------------------


def test_redeem_days_bounds(monkeypatch):
    for value in ("0", "366", "abc"):
        _set(monkeypatch, DATASET_REDEEM_DAYS=value)
        try:
            load_redeem_days()
            raise AssertionError(f"expected rejection for redeem_days {value!r}")
        except DatasetsConfigError as exc:
            assert "DATASET_REDEEM_DAYS" in str(exc)

    for value in ("1", "365"):
        _set(monkeypatch, DATASET_REDEEM_DAYS=value)
        assert load_redeem_days() == int(value)


def test_redeem_days_default(monkeypatch):
    _set(monkeypatch)
    assert load_redeem_days() == 30


# --- HF_TOKEN / R2 -------------------------------------------------------------


def test_hf_token_none_by_default(monkeypatch):
    _set(monkeypatch)
    assert hf_token() is None


def test_hf_token_read(monkeypatch):
    _set(monkeypatch, HF_TOKEN="hf_abc123")
    assert hf_token() == "hf_abc123"


def test_r2_config_none_when_unset(monkeypatch):
    _set(monkeypatch)
    assert load_r2_config() is None


def test_r2_config_requires_all_four(monkeypatch):
    _set(monkeypatch, R2_ACCOUNT_ID="acct", R2_BUCKET="bucket", R2_ACCESS_KEY_ID="key")
    try:
        load_r2_config()
        raise AssertionError("expected DatasetsConfigError")
    except DatasetsConfigError as exc:
        assert "R2_SECRET_ACCESS_KEY" in str(exc)


def test_r2_endpoint_default_and_override(monkeypatch):
    _set(
        monkeypatch,
        R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
        R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret",
    )
    cfg = load_r2_config()
    assert cfg is not None
    assert cfg.endpoint == "https://acct.r2.cloudflarestorage.com"
    assert cfg.url_ttl_s == 3600

    _set(
        monkeypatch,
        R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
        R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret",
        R2_ENDPOINT="http://127.0.0.1:8194/",
    )
    cfg = load_r2_config()
    assert cfg is not None
    assert cfg.endpoint == "http://127.0.0.1:8194"


def test_r2_secret_not_in_repr(monkeypatch):
    _set(
        monkeypatch,
        R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
        R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret",
    )
    assert "secret" not in repr(load_r2_config())


def test_url_ttl_bounds(monkeypatch):
    for value in ("59", "604801", "abc"):
        _set(
            monkeypatch,
            R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
            R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret",
            R2_URL_TTL_S=value,
        )
        try:
            load_r2_config()
            raise AssertionError(f"expected rejection for ttl {value!r}")
        except DatasetsConfigError as exc:
            assert "R2_URL_TTL_S" in str(exc)

    for value in ("60", "604800"):
        _set(
            monkeypatch,
            R2_ACCOUNT_ID="acct", R2_BUCKET="bucket",
            R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret",
            R2_URL_TTL_S=value,
        )
        cfg = load_r2_config()
        assert cfg is not None
        assert cfg.url_ttl_s == int(value)

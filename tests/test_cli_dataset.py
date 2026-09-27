"""CLI tests for `yakrobot-py dataset sessions` (execution plan step 2.2)."""

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from typer.testing import CliRunner  # noqa: E402

from yakrobot_cli import app  # noqa: E402


def _stage(tmp_path, session_id, holder_kind="lease"):
    d = tmp_path / "fakerobot_picar" / session_id
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({
        "format": 1, "robot": "fakerobot_picar", "gateway": "127.0.0.1:8192",
        "holder_kind": holder_kind,
        "started_utc": "2026-01-01T00:00:00Z", "ended_utc": "2026-01-01T00:00:05Z",
        "started_monotonic_ns": 1_000_000_000, "features_spec_version": 1, "hello": None,
        "frames": {"control_up": 1, "control_down": 1, "video": 42},
        "dropped": 13, "spectator": False, "closed_cleanly": True,
    }))
    (d / "control.jsonl").write_text(
        json.dumps({"t": 1_000_000_000, "dir": "up", "msg": {"type": "ping", "t": 99, "rtt_ms": 25}}) + "\n"
        + json.dumps({"t": 1_000_000_100, "dir": "down", "msg": {"type": "pong", "t": 99}}) + "\n"
    )
    (d / "video.jsonl").write_text("")
    (d / "video.bin").write_bytes(b"")
    return d


def test_cli_dataset_sessions_lists_staged(tmp_path, monkeypatch):
    _stage(tmp_path, "20260101T000000Z-abc12345", holder_kind="lease")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))

    result = CliRunner().invoke(app, ["dataset", "sessions", "fakerobot_picar"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "20260101T000000Z-abc12345" in out
    assert "lease" in out
    assert "42" in out      # frames.video
    assert "13" in out      # dropped
    assert "25.0" in out    # rtt_browser_ms


def test_cli_dataset_sessions_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["dataset", "sessions", "fakerobot_picar"])
    assert result.exit_code == 0
    assert "No staged sessions" in result.stdout

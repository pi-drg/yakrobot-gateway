"""Tests for `dataset validate` (execution plan step 2.4).

The structural checks run in the normal suite (no lerobot); the re-open check is skipped
unless the dataset-export extra is installed.
"""

import io
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yakrobot_cli.dataset_export import validate  # noqa: E402


def _jpeg():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (128, 128, 128)).save(buf, "JPEG")
    return buf.getvalue()


def _stage_session(root: Path, robot: str, session_id: str, n_frames: int) -> Path:
    d = root / robot / session_id
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({
        "format": 1, "robot": robot, "gateway": "127.0.0.1:8192", "holder_kind": "lease",
        "started_utc": "2026-01-01T00:00:00Z", "ended_utc": "2026-01-01T00:00:06Z",
        "started_monotonic_ns": 0, "features_spec_version": 1,
        "hello": {"deadman_ms": 700},
        "frames": {"control_up": 1, "control_down": 1, "video": n_frames},
        "dropped": 0, "spectator": False, "closed_cleanly": True,
    }))
    (d / "control.jsonl").write_text(
        json.dumps({"t": 0, "dir": "up", "msg": {"type": "drive", "vx": 1200}}) + "\n"
        + json.dumps({"t": 0, "dir": "down", "msg": {"type": "hello", "battery_v": 7.4}}) + "\n"
    )
    video = [(i * 1e8, _jpeg()) for i in range(n_frames)]
    (d / "video.bin").write_bytes(b"".join(j for _, j in video))
    meta, offset = [], 0
    for seq, (t, jpeg) in enumerate(video):
        meta.append({"t": t, "seq": seq, "offset": offset, "bytes": len(jpeg)})
        offset += len(jpeg)
    (d / "video.jsonl").write_text("\n".join(json.dumps(m) for m in meta) + "\n")
    return d


def _fake_v3(tmp_path):
    d = tmp_path / "ds"
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v3.0"}))
    (d / "videos").mkdir()
    (d / "videos" / "chunk-000" / "file-000.mp4").mkdir(parents=True)
    (d / "videos" / "chunk-000" / "file-000.mp4" / "x").write_bytes(b"x")
    (d / "README.md").write_text("card")
    return d


def test_validate_rejects_missing_episode_index(tmp_path):
    d = _fake_v3(tmp_path)
    with pytest.raises(SystemExit) as exc:
        validate(str(d))
    assert "meta/episodes" in str(exc.value)


def test_validate_rejects_bad_codebase(tmp_path):
    d = _fake_v3(tmp_path)
    (d / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v2.1"}))
    with pytest.raises(SystemExit) as exc:
        validate(str(d))
    assert "v3" in str(exc.value)


def test_validate_accepts_exported(tmp_path, monkeypatch, capsys):
    pytest.importorskip("lerobot")

    from yakrobot_cli.dataset_export import export

    _stage_session(tmp_path, "fakerobot_picar", "20260101T000000Z-deadbeef", n_frames=60)
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))
    out = tmp_path / "out"
    export(
        "fakerobot_picar", sessions="20260101T000000Z-deadbeef", segments_file=None, task="t",
        fps=10, repo_id="test/val", out=str(out), shift_rtt=False,
    )

    validate(str(out))
    assert "valid" in capsys.readouterr().out

"""Round-trip tests for `dataset export` (execution plan step 2.3).

Skipped unless the ``dataset-export`` extra is installed — lerobot/torch never live in
the serving gateway's environment, so the normal suite must skip these.
"""

import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("lerobot")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PIL import Image  # noqa: E402


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (128, 128, 128)).save(buf, "JPEG")
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


def test_export_round_trip(tmp_path, monkeypatch):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    from yakrobot_cli.dataset_export import export

    session_id = "20260101T000000Z-deadbeef"
    _stage_session(tmp_path, "fakerobot_picar", session_id, n_frames=60)
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))

    out = tmp_path / "out"
    export(
        "fakerobot_picar", sessions=session_id, segments_file=None, task="test task",
        fps=10, repo_id="test/export_round_trip", out=str(out), shift_rtt=False,
    )

    ds = LeRobotDataset("test/export_round_trip", root=out)
    assert ds.num_episodes == 1
    assert ds.num_frames == 60
    assert (out / "README.md").exists()


def test_export_rejects_no_episodes(tmp_path, monkeypatch):
    from yakrobot_cli.dataset_export import export

    # Fewer frames than min_episode_s * fps → the resampler drops the episode.
    session_id = "20260101T000000Z-short"
    _stage_session(tmp_path, "fakerobot_picar", session_id, n_frames=3)
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))

    with pytest.raises(SystemExit) as exc:
        export(
            "fakerobot_picar", sessions=session_id, segments_file=None, task="t",
            fps=10, repo_id="test/nope", out=str(tmp_path / "out2"), shift_rtt=False,
        )
    assert "no episodes" in str(exc.value)

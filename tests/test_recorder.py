"""Unit tests for core.recorder — the drop-on-full staging writer.

No sockets, no network: sessions are created against ``tmp_path`` and driven with
synthetic taps, then closed and read back (execution plan step 1.3).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.datasets_config import RecordingConfig  # noqa: E402
from core.plugin import DatasetFeatures  # noqa: E402
from core.recorder import Recordings, Session, open_session  # noqa: E402
from plugins.fakerobot_picar.dataset import FEATURES  # noqa: E402

ROBOT = "fakerobot_picar"
HOLDER = "op"                      # a static-token client id → holder_kind "static"
GATEWAY = "127.0.0.1:8192"


def _cfg(tmp_path: Path, *, max_bytes: int = 1024 ** 3, queue_frames: int = 256) -> RecordingConfig:
    return RecordingConfig(
        enabled=True, dir=tmp_path, max_bytes=max_bytes, queue_frames=queue_frames
    )


def _run(coro):
    return asyncio.run(coro)


def _control_lines(directory: Path) -> list[dict]:
    return [json.loads(l) for l in (directory / "control.jsonl").read_text().splitlines()]


def _video_lines(directory: Path) -> list[dict]:
    return [json.loads(l) for l in (directory / "video.jsonl").read_text().splitlines()]


def test_writes_control_and_video_layout(tmp_path):
    async def run():
        rec = Recordings(_cfg(tmp_path))
        s = rec.open(ROBOT, HOLDER, GATEWAY, FEATURES)
        assert s is not None

        s.tap("down", json.dumps({"type": "hello", "battery_v": 7.4, "deadman_ms": 700}))
        s.tap("up", json.dumps({"type": "drive", "vx": 1200}))
        s.tap("up", "not json at all")
        s.tap("down", json.dumps({"type": "pong", "t": 99}))
        s.tap("video", b"\xff\xd8fakejpeg")

        await rec.close(ROBOT, HOLDER)
        return s.directory

    directory = _run(run())

    control = _control_lines(directory)
    assert [c["dir"] for c in control] == ["down", "up", "up", "down"]
    assert control[0]["msg"] == {"type": "hello", "battery_v": 7.4, "deadman_ms": 700}
    assert control[1]["msg"] == {"type": "drive", "vx": 1200}
    assert control[2] == {k: control[2][k] for k in ("t", "dir", "raw")}
    assert control[2]["raw"] == "not json at all"
    assert all(isinstance(c["t"], int) for c in control)

    video = _video_lines(directory)
    assert len(video) == 1
    assert video[0]["seq"] == 0
    assert video[0]["offset"] == 0
    assert video[0]["bytes"] == len(b"\xff\xd8fakejpeg")
    assert (directory / "video.bin").read_bytes() == b"\xff\xd8fakejpeg"

    session = json.loads((directory / "session.json").read_text())
    assert session["format"] == 1
    assert session["robot"] == ROBOT
    assert session["holder_kind"] == "static"
    assert session["features_spec_version"] == 1
    assert session["hello"] == {"type": "hello", "battery_v": 7.4, "deadman_ms": 700}
    assert session["frames"] == {"control_up": 2, "control_down": 2, "video": 1}
    assert session["spectator"] is False
    assert session["closed_cleanly"] is True
    assert session["ended_utc"].endswith("Z")


def test_video_offsets_are_contiguous(tmp_path):
    async def run():
        rec = Recordings(_cfg(tmp_path))
        s = rec.open(ROBOT, HOLDER, GATEWAY, FEATURES)
        assert s is not None
        frames = [b"\xff\xd8" + b"a" * i for i in range(1, 4)]
        for f in frames:
            s.tap("video", f)
        await rec.close(ROBOT, HOLDER)
        return s.directory, frames

    directory, frames = _run(run())
    video = _video_lines(directory)
    offsets = [v["offset"] for v in video]
    assert offsets == [0, len(frames[0]), len(frames[0]) + len(frames[1])]
    assert [v["bytes"] for v in video] == [len(f) for f in frames]
    assert [v["seq"] for v in video] == [0, 1, 2]
    assert (directory / "video.bin").read_bytes() == b"".join(frames)


def test_busy_marks_spectator_and_deletes_on_close(tmp_path):
    async def run():
        rec = Recordings(_cfg(tmp_path))
        s = rec.open(ROBOT, HOLDER, GATEWAY, FEATURES)
        assert s is not None
        s.tap("down", json.dumps({"type": "hello"}))
        s.tap("down", json.dumps({"type": "error", "detail": "busy"}))
        directory = s.directory
        await rec.close(ROBOT, HOLDER)
        return directory

    directory = _run(run())
    assert not directory.exists()  # a spectator's staging is discarded


def test_queue_full_counts_drops_without_raising(tmp_path):
    async def run():
        rec = Recordings(_cfg(tmp_path, queue_frames=4))
        s = rec.open(ROBOT, HOLDER, GATEWAY, FEATURES)
        assert s is not None
        # Tap far more than the queue holds, synchronously: the writer task never gets a
        # chance to drain in this tight loop, so put_nowait fills the queue and drops.
        for i in range(200):
            s.tap("up", json.dumps({"type": "ping", "t": i}))
        assert s.dropped > 0
        await rec.close(ROBOT, HOLDER)
        return s

    s = _run(run())
    assert s.dropped > 0
    assert s.queue.empty()


def test_writer_exception_keeps_draining(tmp_path, monkeypatch):
    def _raise(self, batch):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Session, "_write_batch", _raise)

    async def run():
        rec = Recordings(_cfg(tmp_path))
        s = rec.open(ROBOT, HOLDER, GATEWAY, FEATURES)
        assert s is not None
        for i in range(1000):
            s.tap("up", json.dumps({"type": "ping", "t": i}))
        await rec.close(ROBOT, HOLDER)
        return s

    s = _run(run())  # no exception escapes
    assert s.broken is True
    assert s.queue.empty()


def test_disk_cap_refuses_new_session(tmp_path):
    (tmp_path / "existing.bin").write_bytes(b"x" * 4096)
    cfg = _cfg(tmp_path, max_bytes=100)
    session = open_session(cfg, ROBOT, HOLDER, GATEWAY, FEATURES)
    assert session is None


def test_session_json_has_no_holder_or_token(tmp_path):
    async def run():
        rec = Recordings(_cfg(tmp_path))
        holder = "lease:deadbeef-0000-0000-0000-000000000000"
        s = rec.open(ROBOT, holder, GATEWAY, FEATURES)
        assert s is not None
        s.tap("up", json.dumps({"type": "drive", "vx": 1200}))
        await rec.close(ROBOT, holder)
        return s.directory

    directory = _run(run())
    blob = "\n".join(p.read_text(errors="replace") for p in directory.iterdir() if p.is_file())
    assert "deadbeef" not in blob
    assert "lease:" not in blob
    session = json.loads((directory / "session.json").read_text())
    assert session["holder_kind"] == "lease"


def test_features_none_refuses(tmp_path):
    assert open_session(_cfg(tmp_path), ROBOT, HOLDER, GATEWAY, None) is None


def test_disabled_config_refuses(tmp_path):
    cfg = RecordingConfig(enabled=False, dir=tmp_path, max_bytes=1024 ** 3, queue_frames=256)
    assert open_session(cfg, ROBOT, HOLDER, GATEWAY, FEATURES) is None

"""Unit tests for core.dataset_timeline — the fixed-rate resampler (execution plan §0.5).

Pure functions over synthetic sessions written by a helper; no lerobot, no sockets.
"""

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.dataset_timeline import build_episodes, load_session  # noqa: E402
from plugins.fakerobot_picar.dataset import FEATURES  # noqa: E402

JPEG = b"\xff\xd8\xff\xd9"


def _vid(*ns):
    return [(t, JPEG) for t in ns]


def _write_session(tmp_path, *, started_ns=0, hello=None, up=(), down=(), video=()):
    d = tmp_path / "sess"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({
        "format": 1, "robot": "r", "gateway": "g", "holder_kind": "static",
        "started_utc": "2026-01-01T00:00:00Z", "ended_utc": "2026-01-01T00:00:01Z",
        "started_monotonic_ns": started_ns, "features_spec_version": 1,
        "hello": hello,
        "frames": {"control_up": 0, "control_down": 0, "video": 0},
        "dropped": 0, "spectator": False, "closed_cleanly": True,
    }))
    lines = [json.dumps({"t": t, "dir": "up", "msg": m}) for t, m in up]
    lines += [json.dumps({"t": t, "dir": "down", "msg": m}) for t, m in down]
    (d / "control.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    (d / "video.bin").write_bytes(b"".join(j for _, j in video))
    meta, offset = [], 0
    for seq, (t, jpeg) in enumerate(video):
        meta.append({"t": t, "seq": seq, "offset": offset, "bytes": len(jpeg)})
        offset += len(jpeg)
    (d / "video.jsonl").write_text("\n".join(json.dumps(m) for m in meta) + ("\n" if meta else ""))
    return d


def _action(frame, name):
    return frame.action[FEATURES.action_names.index(name)]


def _state(frame, name):
    return frame.state[FEATURES.state_names.index(name)]


def _build(tmp_path, fps, *, min_episode_s=5.0, window=None, shift_rtt=False, **session_kwargs):
    session = load_session(_write_session(tmp_path, **session_kwargs))
    return build_episodes(
        session, FEATURES, fps,
        window=window, shift_rtt=shift_rtt, min_episode_s=min_episode_s,
    )


# --- resampling / deadman -------------------------------------------------------


def test_sample_and_hold_between_commands(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(5e7, {"type": "drive", "vx": 1200})],
        video=_vid(0, 1e8, 2e8, 3e8),
    )
    assert [_action(f, "vx") for f in eps[0]] == [0.0, 1200.0, 1200.0, 1200.0]


def test_deadman_zeroes_velocity_not_angles(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 150},
        up=[(5e7, {"type": "drive", "vx": 1200}), (6e7, {"type": "look", "pan": 100})],
        video=_vid(0, 1e8, 2e8, 3e8, 4e8),
    )
    frames = eps[0]
    assert [_action(f, "vx") for f in frames] == [0.0, 1200.0, 1200.0, 0.0, 0.0]
    assert [_action(f, "pan") for f in frames] == [0.0, 100.0, 100.0, 100.0, 100.0]


def test_stop_zeroes_velocity_immediately(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(5e7, {"type": "drive", "vx": 1200}), (1.5e8, {"type": "stop"})],
        video=_vid(0, 1e8, 2e8, 3e8),
    )
    assert [_action(f, "vx") for f in eps[0]] == [0.0, 1200.0, 0.0, 0.0]


def test_ping_does_not_reset_deadman(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100},
        up=[(5e7, {"type": "drive", "vx": 1200}), (1.2e8, {"type": "ping", "t": 99})],
        video=_vid(0, 1e8, 2e8, 3e8),
    )
    vx = [_action(f, "vx") for f in eps[0]]
    assert vx[2] == 0.0  # the ping did not keep the velocity alive


def test_first_command_in_interval_applies_to_tick(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(1e7, {"type": "drive", "vx": 100}), (8e7, {"type": "drive", "vx": 200})],
        video=_vid(0, 1e8, 2e8),
    )
    assert [_action(f, "vx") for f in eps[0]] == [0.0, 200.0, 200.0]


def test_state_uses_clamped_look_echo(tmp_path):
    eps, _ = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(6e7, {"type": "look", "pan": 200})],
        down=[(5e7, {"type": "look", "pan": 150, "tilt": 30})],
        video=_vid(0, 1e8, 2e8),
    )
    frame = eps[0][1]
    assert _state(frame, "pan") == 150.0   # the clamped echo, not the 200 asked for
    assert _action(frame, "pan") == 200.0  # the action is the command, as sent


# --- staleness / episodes -------------------------------------------------------


def test_stale_run_splits_episode(tmp_path):
    eps, stats = _build(
        tmp_path, 2, min_episode_s=1.0,
        hello={"deadman_ms": 100000},
        video=_vid(0, 5e8, 1e9, 1.5e9, 5e9, 5.5e9, 6e9),
    )
    assert len(eps) == 2
    assert stats.stale == 4
    assert eps[0][-1].t_ns < eps[1][0].t_ns


def test_short_episode_dropped(tmp_path):
    eps, stats = _build(
        tmp_path, 2, min_episode_s=5.0,
        hello={"deadman_ms": 100000},
        video=_vid(0, 5e8, 1e9),
    )
    assert eps == []
    assert stats.episodes_dropped_short == 1


# --- latency / shift ------------------------------------------------------------


def test_rtt_lan_from_ping_pong(tmp_path):
    _, stats = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(1e7, {"type": "ping", "t": 99})],
        down=[(3e7, {"type": "pong", "t": 99})],
        video=_vid(0, 1e8),
    )
    assert stats.rtt_lan_ms == 20.0


def test_rtt_browser_from_ping_field(tmp_path):
    _, stats = _build(
        tmp_path, 10, min_episode_s=0.1,
        hello={"deadman_ms": 100000},
        up=[(1e7, {"type": "ping", "t": 1, "rtt_ms": 10}),
            (2e7, {"type": "ping", "t": 2, "rtt_ms": 20}),
            (3e7, {"type": "ping", "t": 3, "rtt_ms": 30})],
        video=_vid(0, 1e8),
    )
    assert stats.rtt_browser_ms == 20.0


def test_shift_uses_browser_minus_lan(tmp_path):
    eps, stats = _build(
        tmp_path, 10, min_episode_s=0.1, shift_rtt=True,
        hello={"deadman_ms": 100000},
        up=[(1e7, {"type": "ping", "t": 99, "rtt_ms": 30}),
            (1.05e8, {"type": "drive", "vx": 1200})],
        down=[(2e7, {"type": "pong", "t": 99})],
        video=_vid(0, 1e8, 2e8),
    )
    assert stats.rtt_lan_ms == 10.0
    assert stats.rtt_browser_ms == 30.0
    # Shift = 30 - 10 = 20 ms: the 1.05e8 drive lands on 8.5e7 → tick 1, not tick 2.
    assert [_action(f, "vx") for f in eps[0]] == [0.0, 1200.0, 1200.0]


def test_no_shift_without_browser_rtt(tmp_path):
    eps, stats = _build(
        tmp_path, 10, min_episode_s=0.1, shift_rtt=True,
        hello={"deadman_ms": 100000},
        up=[(1e7, {"type": "ping", "t": 99}),
            (1.05e8, {"type": "drive", "vx": 1200})],
        down=[(2e7, {"type": "pong", "t": 99})],
        video=_vid(0, 1e8, 2e8),
    )
    assert stats.rtt_browser_ms is None
    assert [_action(f, "vx") for f in eps[0]] == [0.0, 0.0, 1200.0]


# --- window ---------------------------------------------------------------------


def test_window_limits_ticks(tmp_path):
    _, stats = _build(
        tmp_path, 10, min_episode_s=0.1, window=(0.0, 0.2),
        started_ns=1e9,
        hello={"deadman_ms": 100000},
        video=_vid(1e9, 1.1e9, 1.2e9, 1.3e9, 1.4e9),
    )
    assert stats.ticks == 3

"""Build fixed-rate LeRobot episodes from a staged teleop session.

Pure and offline: no lerobot, no torch, no sockets. The export CLI
(``dataset_export``) feeds this the session a recorder wrote and gets back a list of
episodes, each a list of ``Frame`` carrying a resampled action, observation.state and
the JPEG the driver actually saw.

This implements execution plan §0.5 exactly. The rules that matter most:

- Commands are *absolute* state, so resampling is sample-and-hold: the action at tick
  ``i`` is every up-message with ``t_{i-1} < t <= t_i`` applied in order, carried
  forward otherwise.
- Velocities zero out ``deadman_ms`` after the last ``drive``; ``stop`` zeroes them
  immediately; ``look`` and ``ping`` never touch the deadman clock; pan/tilt are servo
  positions and hold their value.
- Observation state is what the robot *reported* downward (the clamped look echo,
  telemetry), never what the browser asked for.
- A tick whose image is older than ``2/fps`` is stale; a run of ``fps`` stale ticks
  splits the episode, and all stale ticks are dropped (a frozen frame paired with moving
  actions teaches a policy the wrong thing).
"""

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from core.plugin import DatasetFeatures


@dataclass(frozen=True)
class Frame:
    action: list[float]
    state: list[float]
    jpeg: bytes
    t_ns: int


@dataclass(frozen=True)
class TimelineStats:
    rtt_lan_ms: float | None
    rtt_browser_ms: float | None
    ticks: int
    stale: int
    episodes_dropped_short: int


@dataclass
class LoadedSession:
    up: list[tuple[int, dict]]
    down: list[tuple[int, dict]]
    video: list[tuple[int, bytes]]
    started_monotonic_ns: int
    hello: dict | None
    features_spec_version: int
    robot: str
    gateway: str


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def load_session(path) -> LoadedSession:
    """Read a staged session directory into the in-memory shape §0.5 step 1 wants."""
    path = Path(path)
    meta = json.loads((path / "session.json").read_text())

    up: list[tuple[int, dict]] = []
    down: list[tuple[int, dict]] = []
    for line in (path / "control.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        msg = rec.get("msg")
        if not isinstance(msg, dict):
            continue  # a raw (non-JSON-object) frame is not a command/telemetry
        (up if rec["dir"] == "up" else down).append((rec["t"], msg))

    video: list[tuple[int, bytes]] = []
    video_lines = [json.loads(l) for l in (path / "video.jsonl").read_text().splitlines() if l.strip()]
    blob = (path / "video.bin").read_bytes()
    for rec in video_lines:
        video.append((rec["t"], blob[rec["offset"] : rec["offset"] + rec["bytes"]]))

    return LoadedSession(
        up=up,
        down=down,
        video=video,
        started_monotonic_ns=meta.get("started_monotonic_ns", 0),
        hello=meta.get("hello"),
        features_spec_version=meta.get("features_spec_version"),
        robot=meta.get("robot", ""),
        gateway=meta.get("gateway", ""),
    )


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rtt_lan(up: list[tuple[int, dict]], down: list[tuple[int, dict]]) -> float | None:
    """The gateway↔robot hop: pair each up ping with the next down pong of the same X."""
    from collections import defaultdict, deque

    pongs: dict = defaultdict(deque)
    for t, msg in down:
        if msg.get("type") == "pong" and _is_number(msg.get("t")):
            pongs[msg["t"]].append(t)

    rtts: list[float] = []
    for t, msg in up:
        if msg.get("type") == "ping" and _is_number(msg.get("t")):
            queue = pongs.get(msg["t"])
            if queue:
                pong_t = queue.popleft()
                rtts.append((pong_t - t) / 1e6)
    return _median(rtts)


def _rtt_browser(up: list[tuple[int, dict]]) -> float | None:
    """The whole browser↔robot round trip: median of the console's own rtt_ms stamps."""
    rtts = [float(msg["rtt_ms"]) for _, msg in up if msg.get("type") == "ping" and _is_number(msg.get("rtt_ms"))]
    return _median(rtts)


def _init_pan_tilt(action: dict, down: list[tuple[int, dict]], features: DatasetFeatures) -> None:
    """Action pan/tilt start from the first downward state, not zero (they are servos)."""
    for name in ("pan", "tilt"):
        for _, msg in down:
            upd = features.state_update(msg)
            if upd and name in upd:
                action[name] = float(upd[name])
                break


def _window_bounds(session: LoadedSession, window) -> tuple[float, float]:
    if not window:
        return float("-inf"), float("inf")
    start_s, end_s = window
    base = session.started_monotonic_ns
    start_ns = base + start_s * 1e9 if start_s is not None else float("-inf")
    end_ns = base + end_s * 1e9 if end_s is not None else float("inf")
    return start_ns, end_ns


def _ticks(session: LoadedSession, fps: int, window) -> tuple[list[int], list[tuple[int, bytes]]]:
    start_ns, end_ns = _window_bounds(session, window)
    video_in = [(t, jpeg) for t, jpeg in session.video if start_ns <= t <= end_ns]
    if not video_in:
        return [], []
    interval = 1e9 / fps
    t0 = video_in[0][0]
    last_t = video_in[-1][0]
    ticks: list[int] = []
    i = 0
    while True:
        t = t0 + round(i * interval)
        if t > last_t:
            break
        ticks.append(t)
        i += 1
    return ticks, video_in


def _split_episodes(frames: list, fps: int, min_episode_s: float) -> tuple[list, int]:
    """Split at runs of >= fps consecutive stale ticks, drop all stale ticks, then drop
    episodes shorter than min_episode_s * fps ticks (§0.5 step 8)."""
    n = len(frames)
    min_ticks = int(min_episode_s * fps)
    stale = [f is None for f, _ in frames]

    segments: list[tuple[int, int]] = []
    seg_start = 0
    i = 0
    while i < n:
        if stale[i]:
            j = i
            while j < n and stale[j]:
                j += 1
            if j - i >= fps:  # a one-second (or longer) freeze splits the episode
                if seg_start < i:
                    segments.append((seg_start, i))
                seg_start = j
            i = j
        else:
            i += 1
    if seg_start < n:
        segments.append((seg_start, n))

    episodes: list = []
    dropped = 0
    for start, end in segments:
        kept = [f for f, _ in frames[start:end] if f is not None]
        if len(kept) < min_ticks:
            dropped += 1
        else:
            episodes.append(kept)
    return episodes, dropped


def build_episodes(
    session: LoadedSession,
    features: DatasetFeatures,
    fps: int,
    window=None,
    shift_rtt: bool = False,
    min_episode_s: float = 5.0,
) -> tuple[list[list[Frame]], TimelineStats]:
    """Turn one loaded session into a list of episodes plus latency/health stats (§0.5)."""
    # 2. Latency — two round trips, not to be confused.
    rtt_lan_ms = _rtt_lan(session.up, session.down)
    rtt_browser_ms = _rtt_browser(session.up)

    up = session.up
    if shift_rtt and rtt_lan_ms is not None and rtt_browser_ms is not None:
        # The tunnel round trip: one down-leg to reach the browser, one up-leg back.
        # Shift by the full difference, never by rtt_lan_ms.
        shift_ns = (rtt_browser_ms - rtt_lan_ms) * 1e6
        up = [(t - shift_ns, msg) for t, msg in up]

    # 3. Deadman.
    hello = session.hello or {}
    deadman_raw = hello.get("deadman_ms")
    if isinstance(deadman_raw, (int, float)) and not isinstance(deadman_raw, bool):
        deadman_ms = float(deadman_raw)
    else:
        deadman_ms = float(features.deadman_ms_default)
    deadman_ns = deadman_ms * 1e6

    # 4. Ticks.
    ticks, video_in = _ticks(session, fps, window)
    if not ticks:
        stats = TimelineStats(rtt_lan_ms, rtt_browser_ms, 0, 0, 0)
        return [], stats

    interval = 1e9 / fps
    stale_threshold = 2 * interval

    velocity = set(features.velocity_names)
    action = {name: 0.0 for name in features.action_names}
    _init_pan_tilt(action, session.down, features)
    state = {name: 0.0 for name in features.state_names}
    last_drive_t = float("-inf")

    up_idx = 0
    down_idx = 0
    video_idx = 0

    frames: list = []  # (Frame | None, t_ns); None marks a stale tick
    stale_count = 0
    t_prev = ticks[0] - interval

    for t_i in ticks:
        # 5. Action: apply up-messages in (t_{i-1}, t_i] in order.
        while up_idx < len(up) and up[up_idx][0] <= t_i:
            t, msg = up[up_idx]
            if t > t_prev:
                if features.is_stop(msg):
                    for name in velocity:
                        action[name] = 0.0
                else:
                    upd = features.action_update(msg)
                    if upd:
                        for key, value in upd.items():
                            if key in action:
                                action[key] = float(value)
                        if velocity & set(upd):
                            last_drive_t = t
            up_idx += 1

        # Deadman zeroes velocities only, and only when the last drive is too old.
        if t_i - last_drive_t > deadman_ns:
            for name in velocity:
                action[name] = 0.0

        # 6. State: carried from down-messages with t <= t_i.
        while down_idx < len(session.down) and session.down[down_idx][0] <= t_i:
            t, msg = session.down[down_idx]
            if t > t_prev:
                upd = features.state_update(msg)
                if upd:
                    for key, value in upd.items():
                        if key in state:
                            state[key] = float(value)
            down_idx += 1

        # 7. Image: the latest video frame at or before t_i.
        while video_idx < len(video_in) and video_in[video_idx][0] <= t_i:
            video_idx += 1
        if video_idx == 0:
            stale = True
            jpeg = b""
        else:
            frame_t, jpeg = video_in[video_idx - 1]
            stale = (t_i - frame_t) > stale_threshold

        if stale:
            stale_count += 1
            frames.append((None, t_i))
        else:
            frames.append((
                Frame(
                    action=[action[name] for name in features.action_names],
                    state=[state[name] for name in features.state_names],
                    jpeg=jpeg,
                    t_ns=t_i,
                ),
                t_i,
            ))
        t_prev = t_i

    # 8. Episodes.
    episodes, dropped = _split_episodes(frames, fps, min_episode_s)

    stats = TimelineStats(
        rtt_lan_ms=rtt_lan_ms,
        rtt_browser_ms=rtt_browser_ms,
        ticks=len(ticks),
        stale=stale_count,
        episodes_dropped_short=dropped,
    )
    return episodes, stats

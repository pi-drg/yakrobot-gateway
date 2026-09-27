"""Drop-on-full teleop recorder: tap frames off the proxy and write a staging layout.

This module observes traffic and never decides it (design §0.1). The proxy calls
``Session.tap`` synchronously right after a frame is received, before relaying it; the
tap timestamps the frame and drops it onto a bounded queue with ``put_nowait``, so a
full queue costs one sample and never waits on — or slows — the relay. A single writer
task drains the queue and does all file I/O in a thread; disk latency is the recorder's
problem and never reaches the socket. If the writer fails it logs once, marks the session
broken and keeps draining (without writing) so the queue never backs up.

Staging layout (execution plan §0.3):

    {RECORDINGS_DIR}/{robot}/{session_id}/
        session.json    format/robot/gateway/holder_kind/timestamps/hello/counters
        control.jsonl   one line per control frame: {"t", "dir", "msg"} or {"t", "dir", "raw"}
        video.bin       JPEG bytes appended back to back
        video.jsonl     one line per frame: {"t", "seq", "offset", "bytes"}
"""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.datasets_config import RecordingConfig
from core.plugin import DatasetFeatures

logger = logging.getLogger(__name__)

_SENTINEL = object()
_BATCH_SIZE = 64          # max items drained to the writer in one to_thread call
_SIZE_CACHE_S = 30.0      # how long a recursive dir-size reading is reused

# {root: (monotonic_time, bytes)} — the disk cap reads the whole staging tree
# recursively, which is expensive to redo on every session open, so it is cached
# briefly. Approximate by design: the cap only stops *new* sessions, and 30 s of
# growth is a rounding error next to RECORDINGS_MAX_GB.
_size_cache: dict[Path, tuple[float, int]] = {}


def _scan_size(root: Path) -> int:
    total = 0
    try:
        it = os.scandir(root)
    except OSError:
        return 0
    with it:
        for entry in it:
            try:
                if entry.is_dir(follow_symlinks=False):
                    total += _scan_size(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
            except OSError:
                continue
    return total


def _total_size(root: Path) -> int:
    now = time.monotonic()
    cached = _size_cache.get(root)
    if cached is not None and now - cached[0] < _SIZE_CACHE_S:
        return cached[1]
    size = _scan_size(root)
    _size_cache[root] = (now, size)
    return size


def _holder_kind(holder: str) -> str:
    """The holder's prefix — never the holder string itself (§0.3)."""
    for prefix in ("lease", "stripe", "free"):
        if holder.startswith(prefix + ":"):
            return prefix
    return "static"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Session:
    """One recording session: a directory, a bounded queue, counters and a writer task."""

    def __init__(
        self,
        cfg: RecordingConfig,
        directory: Path,
        robot: str,
        holder: str,
        gateway_host: str,
        features: DatasetFeatures,
    ) -> None:
        self.cfg = cfg
        self.directory = directory
        self.robot = robot
        self.holder = holder
        self.gateway_host = gateway_host
        self.features = features
        self.session_id = directory.name

        self.queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_frames)

        # Filled by the writer task (in a thread) as it parses frames.
        self.hello: dict | None = None
        self.spectator = False
        self.control_up = 0
        self.control_down = 0
        self.video = 0
        self.dropped = 0
        self.broken = False

        self.started_utc = _utc_now()
        self.ended_utc: str | None = None
        self.started_monotonic_ns = time.monotonic_ns()
        self.closed_cleanly = False
        self.control_sockets = 0
        self._closed = False

        self._video_offset = 0
        self._video_seq = 0

        self._control_f = open(directory / "control.jsonl", "w", encoding="utf-8")
        self._video_f = open(directory / "video.bin", "wb")
        self._video_meta_f = open(directory / "video.jsonl", "w", encoding="utf-8")

        self._write_session_json()
        self._writer_task = asyncio.get_running_loop().create_task(self._writer())

    # -- tap (synchronous, called by the proxy pumps) --------------------------

    def tap(self, kind: str, payload) -> None:
        """Timestamp and enqueue one frame, dropping it when the queue is full.

        Never awaits: a full queue costs one dropped frame, never a delayed relay
        (§0.4). ``payload`` is a ``str`` for ``"up"``/``"down"`` and ``bytes`` for
        ``"video"`` — the pumps wrap their sockets so bytes/text reach here already
        separated (a text video frame or a binary control frame never arrives).
        """
        t = time.monotonic_ns()
        try:
            self.queue.put_nowait((t, kind, payload))
        except asyncio.QueueFull:
            self.dropped += 1

    # -- socket accounting -----------------------------------------------------

    def control_opened(self) -> None:
        self.control_sockets += 1

    def control_closed(self) -> bool:
        """One control socket is gone; True when the session should now close."""
        self.control_sockets -= 1
        return self.control_sockets <= 0

    # -- writer ----------------------------------------------------------------

    async def _writer(self) -> None:
        batch: list = []
        while True:
            item = await self.queue.get()
            if item is _SENTINEL:
                break
            if self.broken:
                continue
            batch.append(item)
            if len(batch) >= _BATCH_SIZE:
                await self._flush(batch)
                batch = []
        if batch and not self.broken:
            await self._flush(batch)

    async def _flush(self, batch: list) -> None:
        try:
            await asyncio.to_thread(self._write_batch, batch)
        except Exception:
            if not self.broken:
                self.broken = True
                logger.warning(
                    "recorder: writer failed for %s; dropping further frames", self.session_id
                )

    def _write_batch(self, batch: list) -> None:
        for t, kind, payload in batch:
            if kind == "video":
                self._write_video(t, payload)
            else:
                self._write_control(t, kind, payload)

    def _write_control(self, t: int, direction: str, payload: str) -> None:
        if direction == "up":
            self.control_up += 1
        else:
            self.control_down += 1
        try:
            msg = json.loads(payload)
        except (ValueError, TypeError):
            line = {"t": t, "dir": direction, "raw": payload}
        else:
            if not isinstance(msg, dict):
                line = {"t": t, "dir": direction, "raw": payload}
            else:
                line = {"t": t, "dir": direction, "msg": msg}
                if direction == "down":
                    if msg.get("type") == "hello" and self.hello is None:
                        self.hello = msg
                    if msg.get("type") == "error" and msg.get("detail") == "busy":
                        self.spectator = True
        self._control_f.write(json.dumps(line) + "\n")

    def _write_video(self, t: int, payload: bytes) -> None:
        offset = self._video_offset
        self._video_f.write(payload)
        meta = {"t": t, "seq": self._video_seq, "offset": offset, "bytes": len(payload)}
        self._video_meta_f.write(json.dumps(meta) + "\n")
        self._video_seq += 1
        self._video_offset += len(payload)
        self.video += 1

    # -- session.json ----------------------------------------------------------

    def _session_dict(self) -> dict:
        return {
            "format": 1,
            "robot": self.robot,
            "gateway": self.gateway_host,
            "holder_kind": _holder_kind(self.holder),
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "started_monotonic_ns": self.started_monotonic_ns,
            "features_spec_version": self.features.spec_version,
            "hello": self.hello,
            "frames": {
                "control_up": self.control_up,
                "control_down": self.control_down,
                "video": self.video,
            },
            "dropped": self.dropped,
            "spectator": self.spectator,
            "closed_cleanly": self.closed_cleanly,
        }

    def _write_session_json(self) -> None:
        with open(self.directory / "session.json", "w", encoding="utf-8") as f:
            json.dump(self._session_dict(), f)

    # -- close -----------------------------------------------------------------

    async def close(self) -> None:
        """Stop the writer, finalize the files, and (if a spectator) delete the dir."""
        if self._closed:
            return
        self._closed = True
        await self.queue.put(_SENTINEL)
        await self._writer_task

        self._control_f.close()
        self._video_f.close()
        self._video_meta_f.close()

        self.ended_utc = _utc_now()
        self.closed_cleanly = not self.broken
        self._write_session_json()

        if self.spectator:
            shutil.rmtree(self.directory, ignore_errors=True)


def open_session(
    cfg: RecordingConfig,
    robot: str,
    holder: str,
    gateway_host: str,
    features: DatasetFeatures | None,
) -> Session | None:
    """Create a session for ``(robot, holder)``, or ``None`` when it must not record.

    Refuses when recording is disabled, when the robot is not recordable (no
    ``DatasetFeatures``), or when the staging root is at or above the disk cap. Creating
    the directory (0700) and writing the initial ``session.json`` happens here, so a
    session is on disk the moment it opens.
    """
    if not cfg.enabled or features is None:
        return None
    if _total_size(cfg.dir) >= cfg.max_bytes:
        logger.warning("recorder: %s is at the disk cap; not recording", cfg.dir)
        return None

    session_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    directory = cfg.dir / robot / session_id
    directory.mkdir(parents=True, exist_ok=False)
    # Staging may hold street video; keep it out of other local users' reach.
    os.chmod(cfg.dir, 0o700)
    os.chmod(directory, 0o700)

    return Session(cfg, directory, robot, holder, gateway_host, features)


class Recordings:
    """The set of currently-open sessions, keyed by ``(robot, holder)``.

    ``open`` creates-or-joins for a control socket (counting it); ``join`` returns the
    session for a video socket without counting it; ``close`` drops one control socket
    and finalizes the session when the last one closes (§0.3).
    """

    def __init__(self, cfg: RecordingConfig) -> None:
        self._cfg = cfg
        self._sessions: dict[tuple[str, str], Session] = {}

    def open(
        self, robot: str, holder: str, gateway_host: str, features: DatasetFeatures | None
    ) -> Session | None:
        key = (robot, holder)
        session = self._sessions.get(key)
        if session is not None:
            session.control_opened()
            return session
        session = open_session(self._cfg, robot, holder, gateway_host, features)
        if session is not None:
            session.control_opened()
            self._sessions[key] = session
        return session

    def join(self, robot: str, holder: str) -> Session | None:
        return self._sessions.get((robot, holder))

    async def close(self, robot: str, holder: str) -> None:
        key = (robot, holder)
        session = self._sessions.get(key)
        if session is None:
            return
        if session.control_closed():
            del self._sessions[key]
            await session.close()

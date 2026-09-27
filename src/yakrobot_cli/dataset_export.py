"""``yakrobot-py dataset export`` — convert staged sessions to LeRobotDataset v3.

This is the **only** file in the gateway that imports lerobot, and it does so inside
functions only, so the serving process never touches torch (design §0.4). It lives in
the ``dataset-export`` extra; run it as ``uv sync --extra dataset-export`` then
``uv run yakrobot-py dataset export …``.

The conversion delegates the resampling rules to ``core.dataset_timeline`` (§0.5) and
writes through the LeRobot v3 API recorded in the plan's §0.9 — ``create``, ``add_frame``
(one ``task`` per frame), ``save_episode`` and ``finalize`` — rather than hand-writing
the parquet/mp4 tree.
"""

import io
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _sanitize_repo_id(repo_id: str) -> str:
    """LeLab's rule: split on the first ``/``, then replace ``[^A-Za-z0-9._-]`` with
    ``_`` in each part, so ``push_to_hub`` never rejects a finished export."""
    if "/" in repo_id:
        owner, name = repo_id.split("/", 1)
        return f"{_slug(owner)}/{_slug(name)}"
    return _slug(repo_id)


def _slug(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", part)


def _load_segments(sessions: str | None, segments_file: str | None, task: str) -> list[tuple]:
    """Return ``(session_id, window, task)`` triples — one per segment, or one per
    session with the default task when no segments file is given."""
    if segments_file:
        import tomllib

        with open(segments_file, "rb") as f:
            data = tomllib.load(f)
        segments = []
        for seg in data.get("segment", []):
            window = (seg.get("start_s"), seg.get("end_s"))
            segments.append((seg["session"], window, seg.get("task") or task or "task"))
        if not segments:
            raise SystemExit(f"no [[segment]] entries in {segments_file}")
        return segments

    if not sessions:
        raise SystemExit("give --sessions <ids> or --segments <file>")
    ids = [s.strip() for s in sessions.split(",") if s.strip()]
    return [(sid, None, task or "task") for sid in ids]


def _write_card(root: Path, *, robot: str, repo_id: str, fps: int, episode_count: int,
                frame_count: int, rtt_lan: float | None, rtt_browser: float | None,
                shifted: bool, gateway: str, started_utc: str | None,
                ended_utc: str | None) -> None:
    card = [
        "---",
        "license: cc-by-nc-4.0",
        "tags:",
        "- yakrobot",
        "---",
        "",
        f"# {repo_id}",
        "",
        f"Teleop demonstrations recorded through the yakrobot gateway for **{robot}**.",
        "",
        f"- gateway: {gateway or '(unknown)'}",
        f"- recorded: {started_utc or '?'} → {ended_utc or '?'}",
        f"- fps: {fps}",
        f"- episodes: {episode_count}",
        f"- frames: {frame_count}",
        f"- rtt_lan_ms: {_fmt(rtt_lan)}",
        f"- rtt_browser_ms: {_fmt(rtt_browser)}",
        f"- actions shifted by tunnel RTT: {'yes' if shifted else 'no'}",
        "",
        "> This dataset was recorded from a live teleop session and may be published.",
    ]
    (root / "README.md").write_text("\n".join(card) + "\n")


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def export(robot, sessions, segments_file, task, fps, repo_id, out, shift_rtt):
    """Export staged sessions to a LeRobotDataset v3 (execution plan step 2.3)."""
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError:
        raise SystemExit(
            "lerobot is not installed — run `uv sync --extra dataset-export` first"
        ) from None

    from PIL import Image
    import numpy as np

    from core.dataset_timeline import build_episodes, load_session
    from core.datasets_config import load_recording_config
    from plugins import discover_plugins

    plugin_cls = discover_plugins().get(robot)
    if plugin_cls is None:
        raise SystemExit(f"unknown robot {robot!r}")
    features = plugin_cls().dataset_features()
    if features is None:
        raise SystemExit(f"robot {robot!r} is not recordable (no dataset_features())")

    recordings_dir = load_recording_config().dir
    repo_id = _sanitize_repo_id(repo_id)
    segments = _load_segments(sessions, segments_file, task)

    # Build every episode first, so "no episodes remain" can refuse before any write.
    collected: list[tuple[str, list]] = []
    rtt_lan = rtt_browser = None
    gateway: str = ""
    started_utc: str | None = None
    ended_utc: str | None = None
    for session_id, window, seg_task in segments:
        session = load_session(recordings_dir / robot / session_id)
        if session.features_spec_version != features.spec_version:
            raise SystemExit(
                f"session {session_id} has features spec version "
                f"{session.features_spec_version}, but {robot} declares "
                f"{features.spec_version} — re-record or re-check the plugin"
            )
        episodes, stats = build_episodes(session, features, fps, window=window, shift_rtt=shift_rtt)
        rtt_lan, rtt_browser = stats.rtt_lan_ms, stats.rtt_browser_ms
        gateway, started_utc, ended_utc = session.gateway, session.started_utc, session.ended_utc
        for episode in episodes:
            collected.append((seg_task, episode))

    if not collected:
        raise SystemExit("no episodes remain after resampling — nothing to export")

    # Image dimensions for the video feature come from the first frame.
    first_img = np.asarray(Image.open(io.BytesIO(collected[0][1][0].jpeg)).convert("RGB"))
    height, width, _ = first_img.shape

    features_dict = {
        "action": {"dtype": "float32", "shape": (len(features.action_names),),
                   "names": list(features.action_names)},
        "observation.state": {"dtype": "float32", "shape": (len(features.state_names),),
                              "names": list(features.state_names)},
        features.camera_key: {"dtype": "video", "shape": (3, height, width),
                              "names": ["channel", "height", "width"]},
    }

    root = Path(out) if out else Path("out") / repo_id
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"output directory {root} already exists and is not empty")

    dataset = LeRobotDataset.create(
        repo_id=repo_id, fps=fps, features=features_dict, root=root, use_videos=True
    )
    frame_count = 0
    for seg_task, episode in collected:
        for frame in episode:
            image = np.asarray(Image.open(io.BytesIO(frame.jpeg)).convert("RGB"))
            dataset.add_frame({
                "action": np.asarray(frame.action, dtype=np.float32),
                "observation.state": np.asarray(frame.state, dtype=np.float32),
                features.camera_key: image,
                "task": seg_task,
            })
            frame_count += 1
        dataset.save_episode()
    dataset.finalize()

    _write_card(
        root, robot=robot, repo_id=repo_id, fps=fps,
        episode_count=len(collected), frame_count=frame_count,
        rtt_lan=rtt_lan, rtt_browser=rtt_browser, shifted=shift_rtt,
        gateway=gateway, started_utc=started_utc, ended_utc=ended_utc,
    )
    print(f"Exported {len(collected)} episode(s), {frame_count} frame(s) to {root}")


def validate(path):
    """Validate a LeRobot v3 dataset by structure, then re-open it when lerobot is
    importable. Works for a gateway export and a LeLab recording alike (Source B)."""
    import json

    root = Path(path)

    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        raise SystemExit(f"not a LeRobot v3 dataset: {info_path} is missing")
    info = json.loads(info_path.read_text())

    codebase = str(info.get("codebase_version", ""))
    if not codebase.startswith("v3"):
        raise SystemExit(f"codebase_version {codebase!r} is not v3")

    episodes_dir = root / "meta" / "episodes"
    if not episodes_dir.is_dir() or not any(episodes_dir.iterdir()):
        raise SystemExit("meta/episodes is missing or empty")

    videos_dir = root / "videos"
    if not videos_dir.is_dir() or not any(videos_dir.iterdir()):
        raise SystemExit("videos is missing or empty")

    if not (root / "README.md").is_file():
        raise SystemExit("README.md is missing")

    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError:
        print(f"valid: {root} (lerobot not installed — skipped re-open)")
        return

    dataset = LeRobotDataset(root.name, root=root)
    print(f"valid: {root} ({dataset.num_episodes} episodes, {dataset.num_frames} frames)")

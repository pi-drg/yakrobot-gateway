"""The ``yakrobot-py dataset`` command group.

Commands for recording/export/publish/fetch of teleop datasets (execution plan phase 2
and later). This module holds the Typer surface and its argument wiring; the heavy
lifting lives in ``core.dataset_timeline`` and ``core.dataset_export`` (lerobot, extra
``dataset-export``).
"""

import json
import sys
from pathlib import Path
from typing import Optional

import typer

# Same importability shim as commands.py: the flat src/ modules (core, plugins) must be
# importable whether we run as an installed console script or from the repo.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

dataset_app = typer.Typer(help="Record, export, publish and fetch teleop datasets.")


def _recordings_dir() -> Path:
    from core.datasets_config import load_recording_config

    return load_recording_config().dir


def _session_rows(robot: str) -> list[dict]:
    from core.dataset_timeline import latency_stats, load_control

    root = _recordings_dir() / robot
    rows: list[dict] = []
    if not root.is_dir():
        return rows
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "session.json").read_text())
        except (OSError, ValueError):
            continue
        up, down = load_control(d)
        rtt_lan, rtt_browser = latency_stats(up, down)
        last_t = max((t for t, _ in up + down), default=meta.get("started_monotonic_ns", 0))
        duration = (last_t - meta.get("started_monotonic_ns", 0)) / 1e9
        frames = meta.get("frames") or {}
        rows.append({
            "session_id": d.name,
            "started_utc": meta.get("started_utc", ""),
            "duration": duration,
            "video": frames.get("video", 0),
            "dropped": meta.get("dropped", 0),
            "holder_kind": meta.get("holder_kind", ""),
            "rtt_lan_ms": rtt_lan,
            "rtt_browser_ms": rtt_browser,
        })
    return rows


def _fmt(v, ndigits=1, none="—"):
    if v is None:
        return none
    return f"{v:.{ndigits}f}"


@dataset_app.command()
def sessions(robot: str = typer.Argument(..., help="Robot plugin name (e.g. fakerobot_picar)")):
    """List staged recording sessions for a robot."""
    rows = _session_rows(robot)
    if not rows:
        print(f"No staged sessions for {robot} under {_recordings_dir()}")
        raise typer.Exit(0)

    header = (
        f"{'SESSION':<26} {'STARTED':<20} {'DUR(s)':>7} {'VIDEO':>6} "
        f"{'DROP':>5} {'HOLDER':<7} {'LAN(ms)':>8} {'BROWSER(ms)':>11}"
    )
    print(header)
    for r in rows:
        print(
            f"{r['session_id']:<26} {r['started_utc']:<20} {_fmt(r['duration']):>7} "
            f"{r['video']:>6} {r['dropped']:>5} {r['holder_kind']:<7} "
            f"{_fmt(r['rtt_lan_ms']):>8} {_fmt(r['rtt_browser_ms']):>11}"
        )


@dataset_app.command()
def export(
    robot: str = typer.Argument(..., help="Robot plugin name (e.g. fakerobot_picar)"),
    sessions: Optional[str] = typer.Option(
        None, "--sessions", help="Comma-separated session ids to export"
    ),
    segments: Optional[str] = typer.Option(
        None, "--segments", help="TOML file of [[segment]] session/start_s/end_s/task entries"
    ),
    task: str = typer.Option("", "--task", help="Default task label for every episode"),
    fps: int = typer.Option(15, "--fps", help="Export frame rate"),
    repo_id: str = typer.Option(..., "--repo-id", help="LeRobot repo id (e.g. myname/dataset)"),
    out: Optional[str] = typer.Option(None, "--out", help="Output root (default ./out/<repo-id>)"),
    shift_rtt: bool = typer.Option(
        False, "--shift-rtt", help="Shift actions earlier by the tunnel round trip"
    ),
):
    """Export staged sessions to a LeRobotDataset v3 (needs `uv sync --extra dataset-export`)."""
    from . import dataset_export

    dataset_export.export(robot, sessions, segments, task, fps, repo_id, out, shift_rtt)


@dataset_app.command()
def validate(path: str = typer.Argument(..., help="Path to a LeRobot v3 dataset")):
    """Validate a LeRobot v3 dataset (a gateway export or a LeLab recording)."""
    from . import dataset_export

    dataset_export.validate(path)

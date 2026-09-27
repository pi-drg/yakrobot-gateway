"""The PiCar's ``DatasetFeatures`` — the same object for ``fakerobot_picar`` and
``picar_freenove``, so a recording made against the simulator exports exactly like one
made against hardware.

Defined here rather than in ``picar_freenove`` so the simulator can import it without
pulling in the real plugin (whose ``robot_adapter`` imports hardware-facing code), and
because the fake exists to mirror the real car's teleop surface exactly (design §2.2,
execution plan §0.2).
"""

from core.plugin import DatasetFeatures


def action_update(msg: dict) -> dict | None:
    """An upward control frame → a partial action dict, or ``None`` to ignore it.

    A ``drive`` carries the three velocities (defaulting any absent one to 0, so a
    partial drive still sets the others explicitly); a ``look`` carries whichever of
    pan/tilt the driver sent. ``stop``, ``ping`` and anything else set nothing — they
    are not commands, and ``is_stop`` handles the explicit stop.
    """
    kind = msg.get("type")
    if kind == "drive":
        return {
            "vx": msg.get("vx", 0),
            "vy": msg.get("vy", 0),
            "omega": msg.get("omega", 0),
        }
    if kind == "look":
        return {k: msg[k] for k in ("pan", "tilt") if k in msg}
    return None


def state_update(msg: dict) -> dict | None:
    """A downward frame → a partial observation.state dict, or ``None``.

    State is what the robot *reported*, never what the browser asked for: the clamped
    ``look`` echo, the ``telemetry`` readings, and the ``hello`` battery level.
    """
    kind = msg.get("type")
    if kind == "look":
        return {k: msg[k] for k in ("pan", "tilt") if k in msg}
    if kind == "telemetry":
        return {k: msg[k] for k in ("distance_cm", "battery_v") if k in msg}
    if kind == "hello":
        if "battery_v" in msg:
            return {"battery_v": msg["battery_v"]}
        return None
    return None


def is_stop(msg: dict) -> bool:
    """True for an explicit ``{"type": "stop"}`` — zeroes velocities immediately."""
    return msg.get("type") == "stop"


FEATURES = DatasetFeatures(
    spec_version=1,
    action_names=("vx", "vy", "omega", "pan", "tilt"),
    state_names=("pan", "tilt", "distance_cm", "battery_v"),
    camera_key="observation.images.front",
    default_fps=15,
    deadman_ms_default=700,
    velocity_names=("vx", "vy", "omega"),
    action_update=action_update,
    state_update=state_update,
    is_stop=is_stop,
)

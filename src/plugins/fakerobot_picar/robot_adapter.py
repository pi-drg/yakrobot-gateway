"""Adapter for the fake PiCar: the real PiCar adapter, pointed at the simulator.

Subclassing rather than reimplementing is the point — the simulator serves the
same HTTP surface, so any drift between what the adapter sends and what the car
accepts shows up here too, instead of only on hardware.
"""

import os

DEFAULT_BASE_URL = "http://127.0.0.1:8081"


def control_base_urls() -> list[str]:
    """Candidate URLs for the simulator; see RobotPlugin.control_base_urls."""
    raw = os.getenv("FAKEROBOT_PICAR_URL", DEFAULT_BASE_URL)
    urls = [u.strip().rstrip("/") for u in raw.split(",")]
    return [u for u in urls if u] or [DEFAULT_BASE_URL]


def make_adapter(timeout: float = 8.0):
    from plugins.picar_freenove.robot_adapter import PicarFreenoveAdapter

    # token="" not None: None would fall through to PICAR_FREENOVE_TOKEN and
    # send the real car's credential to the simulator.
    return PicarFreenoveAdapter(
        base_url=control_base_urls()[0], token="", timeout=timeout
    )

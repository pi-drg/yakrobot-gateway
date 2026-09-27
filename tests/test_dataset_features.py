"""Unit tests for DatasetFeatures and the fakerobot_picar/picar_freenove mapping.

No servers, no network — the three callables are pure functions over frame dicts.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.plugin import DatasetFeatures  # noqa: E402
from plugins.fakerobot_picar.dataset import (  # noqa: E402
    FEATURES,
    action_update,
    is_stop,
    state_update,
)


def test_default_is_none():
    """Robots that don't opt in are not recordable — dataset_features() defaults None."""
    from plugins.fakerobot import FakeRobotPlugin

    assert FakeRobotPlugin().dataset_features() is None


def test_picar_action_update_drive_and_look():
    assert action_update({"type": "drive", "vx": 1200, "vy": -400, "omega": 0}) == {
        "vx": 1200, "vy": -400, "omega": 0,
    }
    # Absent velocity fields default to 0.
    assert action_update({"type": "drive", "vx": 1200}) == {"vx": 1200, "vy": 0, "omega": 0}
    # A look carries whichever of pan/tilt are present.
    assert action_update({"type": "look", "pan": 96}) == {"pan": 96}
    assert action_update({"type": "look", "pan": 96, "tilt": 84}) == {"pan": 96, "tilt": 84}


def test_picar_ignores_ping_and_stop_in_action_update():
    assert action_update({"type": "ping", "t": 99}) is None
    assert action_update({"type": "stop"}) is None
    assert action_update({"type": "hello"}) is None


def test_picar_is_stop():
    assert is_stop({"type": "stop"}) is True
    assert is_stop({"type": "drive", "vx": 0}) is False
    assert is_stop({"type": "ping"}) is False


def test_picar_state_update_look_telemetry_hello():
    assert state_update({"type": "look", "pan": 150, "tilt": 30}) == {"pan": 150, "tilt": 30}
    assert state_update({"type": "telemetry", "distance_cm": 12}) == {"distance_cm": 12}
    assert state_update({"type": "telemetry", "distance_cm": 12, "battery_v": 7.4}) == {
        "distance_cm": 12, "battery_v": 7.4,
    }
    assert state_update({"type": "hello", "battery_v": 7.4}) == {"battery_v": 7.4}
    assert state_update({"type": "hello", "deadman_ms": 700}) is None
    assert state_update({"type": "drive", "vx": 1200}) is None


def test_picar_feature_fields():
    assert isinstance(FEATURES, DatasetFeatures)
    assert FEATURES.spec_version == 1
    assert FEATURES.action_names == ("vx", "vy", "omega", "pan", "tilt")
    assert FEATURES.state_names == ("pan", "tilt", "distance_cm", "battery_v")
    assert FEATURES.camera_key == "observation.images.front"
    assert FEATURES.default_fps == 15
    assert FEATURES.deadman_ms_default == 700
    assert FEATURES.velocity_names == ("vx", "vy", "omega")


def test_picar_plugins_share_one_features_object():
    from plugins.fakerobot_picar import FakePicarPlugin
    from plugins.picar_freenove import PicarFreenovePlugin

    assert FakePicarPlugin().dataset_features() is FEATURES
    assert PicarFreenovePlugin().dataset_features() is FEATURES

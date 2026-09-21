"""The PiCar's marketplace behaviour, with no car and no network.

A stub adapter stands in for the robot, so these tests pin the decisions the
plugin makes — what it bids on, what it refuses, what a delivery carries, and
that executing a task never drives the chassis — rather than the HTTP shapes,
which the adapter and the teleop suite already cover.
"""

import asyncio
import base64
import inspect
import time

import pytest

from plugins.picar_freenove import PicarFreenovePlugin

JPEG = b"\xff\xd8\xff\xe0" + b"pretend-jpeg" * 4


class StubAdapter:
    """Records what the plugin asked the robot to do."""

    def __init__(self, online: bool = True, snapshot_error: Exception | None = None):
        self.online = online
        self.snapshot_error = snapshot_error
        self.calls: list[tuple] = []

    async def capabilities(self, refresh: bool = False) -> dict:
        self.calls.append(("capabilities",))
        if not self.online:
            return {"ok": False, "error": "cannot reach the robot"}
        return {"ok": True, "online": True, "wheels": "ordinary", "holonomic": False}

    async def distance(self) -> dict:
        self.calls.append(("distance",))
        return {"ok": True, "cm": 124.7}

    async def battery(self) -> dict:
        self.calls.append(("battery",))
        return {"ok": True, "volts": 8.02}

    async def look(self, pan: int, tilt: int) -> dict:
        self.calls.append(("look", pan, tilt))
        return {"ok": True, "pan": pan, "tilt": tilt}

    async def snapshot_bytes(self) -> bytes:
        self.calls.append(("snapshot",))
        if self.snapshot_error:
            raise self.snapshot_error
        return JPEG


def run(coro):
    return asyncio.run(coro)


def plugin_with(adapter: StubAdapter) -> PicarFreenovePlugin:
    plugin = PicarFreenovePlugin()
    plugin.adapter = adapter
    return plugin


SENSING_TASK = {
    "task_description": "Read the distance to the far wall in the server room.",
    "task_category": "env_sensing",
    "budget_ceiling": 2.00,
    "sla_seconds": 90,
    "capability_requirements": {"sensors_required": ["distance"]},
}

INSPECTION_TASK = {
    "task_description": "Photograph the rack door and note the clearance.",
    "task_category": "visual_inspection",
    "budget_ceiling": 5.00,
    "sla_seconds": 120,
    "capability_requirements": {"modalities_required": ["still_image"]},
}


# --------------------------------------------------------------------------
# Bidding
# --------------------------------------------------------------------------
def test_bids_on_sensing_and_inspection():
    for task in (SENSING_TASK, INSPECTION_TASK):
        bid = run(plugin_with(StubAdapter()).bid(task))
        assert bid is not None, task["task_category"]
        assert bid["price"] == 0.75
        assert bid["currency"] == "usd"
        assert bid["sla_commitment_seconds"] == min(task["sla_seconds"], 120)


def test_declines_work_that_needs_the_car_to_drive():
    task = dict(SENSING_TASK,
                task_description="Navigate to waypoint 3 and measure the doorway.",
                capability_requirements={"sensors_required": ["distance"],
                                         "requires": ["navigation"]})
    assert run(plugin_with(StubAdapter()).bid(task)) is None


def test_declines_sensors_and_categories_it_does_not_have():
    adapter = StubAdapter()
    temperature = dict(SENSING_TASK, capability_requirements={"sensors_required": ["temperature"]})
    assert run(plugin_with(adapter).bid(temperature)) is None

    lidar = dict(INSPECTION_TASK, capability_requirements={"modalities_required": ["lidar"]})
    assert run(plugin_with(adapter).bid(lidar)) is None

    assert run(plugin_with(adapter).bid(dict(SENSING_TASK, task_category="aerial_survey"))) is None
    # None of those should have reached the robot: a decline costs nothing.
    assert adapter.calls == []


def test_declines_under_budget_and_when_offline():
    assert run(plugin_with(StubAdapter()).bid(dict(SENSING_TASK, budget_ceiling=0.50))) is None
    assert run(plugin_with(StubAdapter(online=False)).bid(SENSING_TASK)) is None


def test_sla_commitment_is_capped_by_the_plugin_not_the_caller():
    bid = run(plugin_with(StubAdapter()).bid(dict(SENSING_TASK, sla_seconds=900)))
    assert bid["sla_commitment_seconds"] == 120


# --------------------------------------------------------------------------
# Executing
# --------------------------------------------------------------------------
def test_sensing_delivery_carries_readings_and_identity():
    adapter = StubAdapter()
    result = run(plugin_with(adapter).execute("task-1", "Doorway clearance", {}))

    assert result["success"] is True
    delivery = result["delivery_data"]
    assert {"type": "distance", "value": 124.7, "unit": "cm"} in delivery["readings"]
    assert {"type": "battery_voltage", "value": 8.02, "unit": "volts"} in delivery["readings"]
    assert delivery["robot_name"] == "PiCar-Seattle-01"
    assert delivery["stationary"] is True
    assert "capture" not in delivery
    assert [call[0] for call in adapter.calls] == ["distance", "battery"]


def test_inspection_delivery_carries_a_jpeg_and_the_aim():
    adapter = StubAdapter()
    result = run(plugin_with(adapter).execute(
        "task-2", "Photograph the rack door",
        {"task_category": "visual_inspection", "pan": 120, "tilt": 95}))

    assert result["success"] is True
    capture = result["delivery_data"]["capture"]
    assert base64.b64decode(capture["image_base64"]) == JPEG
    assert capture["mime_type"] == "image/jpeg"
    assert (capture["pan"], capture["tilt"]) == (120, 95)
    assert ("look", 120, 95) in adapter.calls


def test_camera_aim_is_clamped_to_servo_travel():
    adapter = StubAdapter()
    run(plugin_with(adapter).execute("task-3", "Look at the ceiling",
                                     {"include_image": True, "pan": 400, "tilt": -20}))
    assert ("look", 150, 30) in adapter.calls


def test_half_an_aim_is_ignored_rather_than_guessed():
    adapter = StubAdapter()
    run(plugin_with(adapter).execute("task-4", "Photograph the door",
                                     {"include_image": True, "pan": 120}))
    assert not [call for call in adapter.calls if call[0] == "look"]


def test_a_failed_capture_fails_the_task_with_partial_data():
    adapter = StubAdapter(snapshot_error=RuntimeError("camera busy"))
    result = run(plugin_with(adapter).execute("task-5", "Photograph the door",
                                              {"include_image": True}))
    assert result["success"] is False
    assert "camera busy" in result["error"]
    assert result["partial_data"]["distance"]["ok"] is True


def test_executing_a_task_never_commands_the_wheels():
    """The safety rule this plugin is built around, asserted rather than assumed.

    A marketplace buyer is not the operator watching the car, so a task may move
    the camera mount but never the chassis — no drive, no mecanum, no stop.
    """
    source = inspect.getsource(PicarFreenovePlugin.execute)
    for forbidden in ("drive", "mecanum", "stop"):
        assert f".{forbidden}(" not in source

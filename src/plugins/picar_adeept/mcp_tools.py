"""MCP-facing adapter: tool names, signatures and validation.

Every handler delegates to PicarAdeeptAdapter. Tools are the picar_freenove set
minus what this car cannot physically do — so an agent is never handed a tool
that can only fail:

* no `vy` on `move`: ordinary wheels, no strafing;
* no `pan` on `look`: one servo, tilt only;
* no `scan`: the ultrasonic sensor is fixed to the bumper.

`Literal` types are load-bearing: `/drive` takes `"back"`, not `"backward"`.
"""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from .robot_adapter import PicarAdeeptAdapter


def register(mcp: FastMCP, adapter: PicarAdeeptAdapter) -> None:
    """Register PiCar Adeept MCP tools on the server."""

    # --- status ------------------------------------------------------------

    @mcp.tool
    async def picar_adeept_is_online() -> dict:
        """Check whether the Adeept PiCar is reachable."""
        info = await adapter.get("/info")
        if not info.get("ok"):
            return {"online": False, "error": info.get("error")}
        return {
            "online": True,
            "battery_v": info.get("battery_v"),
            "wheels": info.get("wheels"),
        }

    @mcp.tool
    async def picar_adeept_capabilities() -> dict:
        """What this car can do: wheel type, strafing, and which camera axes move.

        The Adeept AWR has ordinary wheels (no strafing) and a single tilt servo
        (no pan) — this reports what the car itself says, so check it if a
        plan depends on either.
        """
        return await adapter.capabilities(refresh=True)

    @mcp.tool
    async def picar_adeept_battery() -> dict:
        """Battery voltage and rough percentage. Two 18650 cells: ~8.4 V full;
        the car warns below 6.0 V.

        Motors brown out before the Pi does, so movement gets unreliable well
        before the car goes offline.
        """
        return await adapter.battery()

    # --- movement ----------------------------------------------------------

    @mcp.tool
    async def picar_adeept_drive(
        direction: Literal["forward", "back", "left", "right", "stop"],
        duty: int = 1600,
        duration_ms: int = 800,
    ) -> dict:
        """Drive using a preset, then stop automatically.

        `left` and `right` SPIN THE CAR IN PLACE; this car cannot move sideways.

        duty is motor power 0-4095. The stall floor on this car has not been
        measured yet; 1600 is the starting default, and if the car does not
        move, raise it in steps of ~300 rather than jumping to full power.
        duration_ms is capped at 3000 ms — issue repeated moves for longer
        journeys, checking picar_adeept_distance between them.
        """
        return await adapter.drive(direction, duty, duration_ms)

    @mcp.tool
    async def picar_adeept_move(
        vx: int = 0,
        omega: int = 0,
        duration_ms: int = 800,
    ) -> dict:
        """Drive and turn at once, then stop automatically.

        Positive vx drives FORWARD (negative reverses); positive omega turns
        COUNTER-CLOCKWISE (left), negative clockwise. Values are motor duty,
        0-4095; combine them for an arc, e.g. vx=1600, omega=600 curves left.
        This car has ordinary wheels and cannot strafe sideways.
        """
        return await adapter.move(vx, omega, duration_ms)

    @mcp.tool
    async def picar_adeept_stop() -> dict:
        """Stop the motors immediately. Safe to call at any time."""
        return await adapter.stop()

    @mcp.tool
    async def picar_adeept_look(tilt: int = 90) -> dict:
        """Tilt the camera. 90 is level; higher looks UP, lower looks down.

        The car has no pan servo — to look left or right, turn the car. The
        robot clamps tilt to its safe travel (about 70-150) and returns the
        angle it actually took.
        """
        return await adapter.look(tilt)

    # --- sensing -----------------------------------------------------------

    @mcp.tool
    async def picar_adeept_distance() -> dict:
        """Distance in cm to whatever is directly ahead, from the ultrasonic sensor.

        The sensor is fixed facing forward (it does not move with the camera).
        Useful range is about 2-200 cm; soft or angled surfaces can read as
        nothing at all. To check other directions, turn the car and re-read.
        """
        return await adapter.distance()

    @mcp.tool
    async def picar_adeept_line() -> dict:
        """Read the three downward-facing infrared line sensors.

        Reports each sensor (left, middle, right) as over the line or not, for
        following a dark line on a light floor.
        """
        return await adapter.line()

    @mcp.tool
    async def picar_adeept_snapshot() -> Image:
        """Capture one frame from the car's camera.

        Returns the actual image, shown inline by an MCP client. Aim it with
        picar_adeept_look first. The image is large — request it when you need
        to see, not as a routine status check.
        """
        jpeg = await adapter.snapshot_bytes()
        return Image(data=jpeg, format="jpeg")

    # --- lights and sound --------------------------------------------------

    @mcp.tool
    async def picar_adeept_led(
        effect: Literal["solid", "off", "blink", "rainbow", "breathing", "chase"] = "solid",
        r: int = 0,
        g: int = 0,
        b: int = 0,
        brightness: int | None = None,
        duration_ms: int | None = None,
        index: int | None = None,
        reverse: bool = False,
    ) -> dict:
        """Set the car's eight WS2812 LEDs.

        r/g/b are 0-255 and apply to `solid`; for `breathing` and `chase` a
        non-zero colour overrides the cycling rainbow. `index` (0-7) targets a
        single LED (solid only), otherwise all of them. `duration_ms` bounds an
        animation and leaves the LEDs dark afterwards; omit it and the effect
        runs until the next call. `reverse` flips which way `chase` and
        `rainbow` travel.
        """
        return await adapter.led(
            effect=effect, r=r, g=g, b=b, brightness=brightness,
            duration_ms=duration_ms, index=index, reverse=reverse,
        )

    @mcp.tool
    async def picar_adeept_beep(
        note: str = "A4",
        duration_ms: int = 200,
        notes: list[tuple[str, int]] | None = None,
    ) -> dict:
        """Sound the car's buzzer — to get attention or signal a step.

        Plays one `note` for `duration_ms`, or a `notes` sequence of
        [note, ms] pairs (e.g. [["C4", 200], ["rest", 100], ["G4", 300]]).
        Notes are names like "C4" or "A#5"; the buzzer covers roughly A3-A5.
        Total playing time is capped at 3000 ms.
        """
        return await adapter.buzzer(notes or [(note, duration_ms)])

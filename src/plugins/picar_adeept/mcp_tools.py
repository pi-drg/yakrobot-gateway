"""MCP-facing adapter: tool names, signatures and validation.

Every handler delegates to PicarAdeeptAdapter. The tool set is the Freenove
car's minus what this hardware lacks (pan, scan, strafe) plus the buzzer, so an
agent is never offered a tool that can only fail.

Duty notes are deliberately vaguer than Freenove's: this car's stall floor has
not been measured yet (see picar_adeept_fastapi's AGENTS.md). The robot's own
default of 1600 is the starting point.
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
        """Check whether the car is reachable."""
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
        """What this car can do: ordinary wheels (no strafing), tilt-only camera."""
        return await adapter.capabilities(refresh=True)

    @mcp.tool
    async def picar_adeept_battery() -> dict:
        """Battery voltage and rough percentage. Two 18650 cells: ~8.4 V full, warn at 6.0 V.

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
        """Drive using a differential preset, then stop automatically.

        `left` and `right` SPIN THE CAR IN PLACE. This car has ordinary wheels
        and cannot move sideways.

        duty is motor power 0-4095. The stall floor is not yet measured; 1600 is
        the starting point. duration_ms is capped at 3000 ms — issue repeated
        moves for longer journeys, checking sensors between them.
        """
        return await adapter.drive(direction, duty, duration_ms)

    @mcp.tool
    async def picar_adeept_move(
        vx: int = 0,
        omega: int = 0,
        duration_ms: int = 800,
    ) -> dict:
        """Drive and turn at once: positive vx FORWARD, positive omega COUNTER-CLOCKWISE.

        Values are motor duty (0-4095); combine them to drive an arc. There is
        no sideways axis — this car has ordinary wheels.
        """
        return await adapter.move(vx, omega, duration_ms)

    @mcp.tool
    async def picar_adeept_stop() -> dict:
        """Stop the motors immediately. Safe to call at any time."""
        return await adapter.stop()

    @mcp.tool
    async def picar_adeept_look(tilt: int = 90) -> dict:
        """Tilt the camera. 90 is level; higher tilt looks UP, lower looks down.

        The camera cannot pan — turn the car to look sideways. The robot clamps
        tilt to the bracket's safe travel (currently 70-150).
        """
        return await adapter.look(tilt)

    # --- sensing -----------------------------------------------------------

    @mcp.tool
    async def picar_adeept_distance() -> dict:
        """Distance in cm to whatever is directly ahead, from the bumper ultrasonic.

        The sensor is fixed facing forward and reads up to 2 m. To check the
        sides, spin the car and read again.
        """
        return await adapter.distance()

    @mcp.tool
    async def picar_adeept_line() -> dict:
        """Read the three downward-facing infrared line sensors (left, middle, right).

        True means that sensor sees the line (dark tape on a light floor).
        """
        return await adapter.line()

    @mcp.tool
    async def picar_adeept_snapshot() -> Image:
        """Capture one frame from the car's camera.

        Returns the actual image. Aim it with picar_adeept_look first. The image
        is large — request it when you need to see, not as a routine check.
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
        """Set the car's eight LEDs.

        r/g/b are 0-255 and apply to `solid`; for `breathing` and `chase` a
        non-zero colour overrides the cycling rainbow. `index` targets a single
        LED (solid only). `duration_ms` bounds an animation and leaves the LEDs
        dark afterwards; omit it and the effect runs until the next call.
        """
        return await adapter.led(
            effect=effect, r=r, g=g, b=b, brightness=brightness,
            duration_ms=duration_ms, index=index, reverse=reverse,
        )

    @mcp.tool
    async def picar_adeept_buzzer(
        notes: list[str] | None = None,
        note: str = "A4",
        ms: int = 200,
    ) -> dict:
        """Sound the buzzer. Give one `note` for `ms`, or a `notes` sequence.

        Notes are names like "C4", "A#5", or "rest"; in a sequence each may carry
        its own length as "C4:300", otherwise `ms` applies. The whole sequence is
        capped at 3000 ms.
        """
        seq = []
        for item in notes or [note]:
            name, _, length = item.partition(":")
            seq.append((name.strip(), int(length) if length.strip() else ms))
        return await adapter.buzzer(seq)

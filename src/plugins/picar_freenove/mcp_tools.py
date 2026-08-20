"""MCP-facing adapter: tool names, signatures and validation.

Every handler delegates to PicarFreenoveAdapter. Two conventions worth knowing
when reading these signatures:

* `Literal` types are load-bearing, not decoration. `/drive` takes `"back"` and
  raises on `"backward"`, so the enum removes a whole class of caller error.
* Duration and duty are clamped in the adapter, so a tool cannot be talked into
  a long unattended move by an over-eager caller.
"""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from .robot_adapter import PicarFreenoveAdapter


def register(mcp: FastMCP, adapter: PicarFreenoveAdapter) -> None:
    """Register PiCar Freenove MCP tools on the server."""

    # --- status ------------------------------------------------------------

    @mcp.tool
    async def picar_freenove_is_online() -> dict:
        """Check whether the PiCar is reachable."""
        info = await adapter.get("/info")
        if not info.get("ok"):
            return {"online": False, "error": info.get("error")}
        return {
            "online": True,
            "battery_v": info.get("battery_v"),
            "wheels": info.get("wheels"),
        }

    @mcp.tool
    async def picar_freenove_capabilities() -> dict:
        """What this specific car can do — wheel type and whether it can strafe.

        Call this before planning lateral movement. Cars with ordinary wheels
        cannot strafe and will refuse a sideways command; cars with mecanum
        wheels are fully holonomic.
        """
        return await adapter.capabilities(refresh=True)

    @mcp.tool
    async def picar_freenove_battery() -> dict:
        """Battery voltage in volts. Two 18650 cells: ~8.4 V full, ~6.4 V empty.

        Motors brown out before the Pi does, so movement gets unreliable well
        before the car goes offline.
        """
        return await adapter.battery()

    # --- movement ----------------------------------------------------------

    @mcp.tool
    async def picar_freenove_drive(
        direction: Literal["forward", "back", "left", "right", "stop"],
        duty: int = 1200,
        duration_ms: int = 800,
    ) -> dict:
        """Drive using a differential preset, then stop automatically.

        `left` and `right` SPIN THE CAR IN PLACE — they are not sideways moves.
        Use picar_freenove_move for strafing (mecanum wheels only).

        duty is motor power 0-4095; below ~700 the wheels stall, and on the floor
        1200 is a sensible default. duration_ms is capped at 3000 ms — issue
        repeated moves for longer journeys, checking sensors between them.
        """
        return await adapter.drive(direction, duty, duration_ms)

    @mcp.tool
    async def picar_freenove_move(
        vx: int = 0,
        vy: int = 0,
        omega: int = 0,
        duration_ms: int = 800,
    ) -> dict:
        """Move holonomically: forward/back, sideways, and rotation at once.

        Positive vx drives FORWARD, positive vy strafes LEFT, positive omega
        spins COUNTER-CLOCKWISE in place. Values are motor duty (0-4095, ~1200
        is a good default); combine axes freely.

        Strafing (vy) needs mecanum wheels. On a car with ordinary wheels this
        is refused with a clear error — check picar_freenove_capabilities first.
        Strafing also fights the wheel rollers, so it covers less ground than
        driving at the same duty and duration.
        """
        return await adapter.mecanum(vx, vy, omega, duration_ms)

    @mcp.tool
    async def picar_freenove_stop() -> dict:
        """Stop the motors immediately. Safe to call at any time."""
        return await adapter.stop()

    @mcp.tool
    async def picar_freenove_look(pan: int = 90, tilt: int = 90) -> dict:
        """Aim the camera head. 90/90 is centred and level.

        Higher pan looks RIGHT, lower looks LEFT. Higher tilt looks UP — the head
        travels from the horizon upward, it does not point at the floor. The
        robot clamps both axes to its own safe travel limits.
        """
        return await adapter.look(pan, tilt)

    # --- sensing -----------------------------------------------------------

    @mcp.tool
    async def picar_freenove_distance() -> dict:
        """Distance in cm to whatever is directly ahead, from the ultrasonic sensor.

        Single reading along the head's current aim. Useful range is roughly
        2-400 cm; soft or angled surfaces can read as nothing at all.
        """
        return await adapter.distance()

    @mcp.tool
    async def picar_freenove_scan() -> dict:
        """Sweep the head and return distance at each angle.

        Slower than a single reading — the servo has to travel and settle. Use it
        to choose a direction; use picar_freenove_distance to watch one.
        """
        return await adapter.scan()

    @mcp.tool
    async def picar_freenove_line() -> dict:
        """Read the three downward-facing infrared line sensors.

        Reports each sensor as seeing the line or not, for line-following on a
        marked course. Needs a strong light/dark contrast underneath.
        """
        return await adapter.line()

    @mcp.tool
    async def picar_freenove_snapshot() -> Image:
        """Capture one frame from the car's camera.

        Returns the actual image, not a description of it — an MCP client shows
        it inline. Aim the head with picar_freenove_look first. The image is
        large — request it when you need to see, not as a routine status check.
        """
        jpeg = await adapter.snapshot_bytes()
        return Image(data=jpeg, format="jpeg")

    # --- lights ------------------------------------------------------------

    @mcp.tool
    async def picar_freenove_led(
        effect: Literal["solid", "off", "blink", "rainbow", "breathing", "chase"] = "solid",
        r: int = 0,
        g: int = 0,
        b: int = 0,
        brightness: int | None = None,
        duration_ms: int | None = None,
        index: int | None = None,
        reverse: bool = False,
    ) -> dict:
        """Set the eight-LED ring on top of the car.

        r/g/b are 0-255 and apply to `solid`; for `breathing` and `chase` a
        non-zero colour overrides the cycling rainbow. `index` targets a single
        LED (solid only), otherwise the whole ring. `duration_ms` bounds an
        animation and leaves the ring dark afterwards; omit it and the effect
        runs until the next call. `reverse` flips which way `chase` and
        `rainbow` travel.

        Lights are on a separate bus from the motors, so animating never
        interferes with driving.
        """
        return await adapter.led(
            effect=effect, r=r, g=g, b=b, brightness=brightness,
            duration_ms=duration_ms, index=index, reverse=reverse,
        )

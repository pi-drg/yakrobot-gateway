from typing import Literal

from fastmcp import FastMCP

from .robot_adapter import TelloAdapter


def register(mcp: FastMCP, adapter: TelloAdapter) -> None:
    """Register Tello MCP tools on the server."""

    @mcp.tool
    async def tello_takeoff() -> dict:
        """Take off and hover at ~1 meter altitude.
        Must be called before any movement commands.
        Requires battery > 10%."""
        try:
            return await adapter.takeoff()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_land() -> dict:
        """Land the drone safely at its current position.
        The drone will descend and stop motors on touchdown."""
        try:
            return await adapter.land()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_move(
        direction: Literal["forward", "back", "left", "right", "up", "down"],
        distance: int,
    ) -> dict:
        """Move the drone in a direction by a specified distance.
        The drone must be flying (call tello_takeoff first).

        Args:
            direction: Movement direction relative to the drone's heading.
            distance: Distance in centimeters (20-500).
        """
        if not 20 <= distance <= 500:
            return {"status": "error", "message": "distance must be 20-500 cm"}
        try:
            return await adapter.move(direction, distance)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_rotate(
        direction: Literal["clockwise", "counter_clockwise"],
        degrees: int,
    ) -> dict:
        """Rotate the drone in place without changing position.
        The drone must be flying.

        Args:
            direction: Rotation direction.
            degrees: Rotation angle (1-360).
        """
        if not 1 <= degrees <= 360:
            return {"status": "error", "message": "degrees must be 1-360"}
        try:
            return await adapter.rotate(direction, degrees)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_flip(
        direction: Literal["forward", "back", "left", "right"],
    ) -> dict:
        """Perform an acrobatic flip. Requires sufficient battery (typically >50%).
        The drone must be flying.

        Args:
            direction: Flip direction.
        """
        try:
            return await adapter.flip(direction)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_get_status() -> dict:
        """Get comprehensive drone status including battery level,
        height, flight time, temperature, and flying state."""
        try:
            return await adapter.get_status()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_get_attitude() -> dict:
        """Get IMU orientation and spatial data: pitch, roll, yaw (degrees),
        barometer altitude (cm), TOF distance (cm), and velocity (cm/s)."""
        try:
            return await adapter.get_attitude()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_get_drone_info() -> dict:
        """Get static drone information: SDK version, serial number,
        and WiFi signal quality."""
        try:
            return await adapter.get_drone_info()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def tello_is_online() -> dict:
        """Check if the drone is powered on, connected, and responding.
        Returns online status and battery level if reachable."""
        return await adapter.is_online()

from typing import Literal
from fastmcp import FastMCP
from .robot_adapter import FakeRobotAdapter


def register(mcp: FastMCP, adapter: FakeRobotAdapter) -> None:
    """Register Fake Robot MCP tools on the server."""

    @mcp.tool
    async def fakerobot_move(
        direction: Literal["forward", "back", "left", "right", "stop"],
    ) -> dict:
        """Move the fake robot in a given direction.
        forward/back auto-stop after 2 seconds, left/right after 1 second,
        stop halts motors immediately."""
        return await adapter.get(f"/motor/{direction}")

    @mcp.tool
    async def fakerobot_is_online() -> dict:
        """Check if the fake robot simulator is running and reachable."""
        try:
            await adapter.get("/info")
            return {"online": True}
        except Exception:
            return {"online": False}

    @mcp.tool
    async def fakerobot_get_temperature_humidity() -> dict:
        """Read simulated temperature (C) and humidity (%) from the fake robot."""
        return await adapter.get("/sensor/ht")

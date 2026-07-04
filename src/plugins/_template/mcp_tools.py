from fastmcp import FastMCP
from .robot_adapter import TemplateAdapter


def register(mcp: FastMCP, adapter: TemplateAdapter) -> None:
    """Register your robot's MCP tools on the server."""

    @mcp.tool
    async def myrobot_is_online() -> dict:
        """Check if the robot is online and reachable."""
        try:
            await adapter.get("/info")
            return {"online": True}
        except Exception:
            return {"online": False}

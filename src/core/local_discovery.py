"""Local robot discovery — what is plugged into *this* orchestrator.

No blockchain involved: results are built from the gateway's in-process plugin
registry. On-chain discovery (ERC-8004) lives in the separate identity layer.
"""

from fastmcp import FastMCP

from core.plugin import RobotPlugin


def _terms_dict(plugin: RobotPlugin) -> dict | None:
    """Flatten a plugin's bidding terms, or ``None`` if it opts out."""
    terms = plugin.metadata().bidding_terms
    if terms is None:
        return None
    return {
        "min_price_cents": terms.min_price_cents,
        "currency": terms.currency,
        "accepted_task_types": list(terms.accepted_task_types),
    }


def register_local_discovery_tools(
    mcp: FastMCP,
    plugins: dict[str, RobotPlugin],
    mounted_robots: dict[str, str] | None = None,
) -> None:
    """Register a local discovery MCP tool listing robots on this gateway.

    Args:
        mcp: The FastMCP server to register the tool on.
        plugins: Map of plugin name → instantiated ``RobotPlugin``.
        mounted_robots: Map of plugin name → local MCP endpoint path
                        (e.g. {"tumbller": "/tumbller/mcp"}).
    """
    _mounted = mounted_robots or {}

    @mcp.tool
    async def list_connected() -> dict:
        """List everything currently plugged into this gateway (the local fleet).

        Reports robots served by this gateway right now — their type, local MCP
        endpoint, tools, and marketplace bidding terms. This is local introspection
        only; it does not query any blockchain. For on-chain robot discovery, use
        the identity layer's discovery tool.

        Returns:
            A dict with a "robots" list and a "count". Each robot entry has:
            - name: Human-readable robot name
            - robot_type: Locomotion/form-factor classification
            - local_endpoint: MCP endpoint path on this gateway (e.g. "/tumbller/mcp")
            - tools: List of MCP tool names the robot exposes
            - bidding_terms: Marketplace pricing dict, or null if not participating
        """
        robots = [
            {
                "name": plugin.metadata().name,
                "robot_type": plugin.metadata().robot_type,
                "local_endpoint": _mounted.get(name),
                "tools": plugin.tool_names(),
                "bidding_terms": _terms_dict(plugin),
            }
            for name, plugin in plugins.items()
        ]
        return {"robots": robots, "count": len(robots)}

"""
Template robot plugin — copy this directory to create a new robot.

1. Copy _template/ to a new directory: cp -r _template/ myrobot/
2. Rename the class and fill in metadata()
3. Implement robot_adapter.py for your robot's communication protocol
4. Define MCP tools in mcp_tools.py
5. Add optional dependencies to pyproject.toml if needed
"""

from core.plugin import RobotPlugin, RobotMetadata


class TemplatePlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="My Robot",
            description="Description of your robot.",
            robot_type="differential_drive",  # e.g. quadrotor, articulated_arm
            url_prefix="myrobot",              # URL path: /myrobot/mcp
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
        )

    def tool_names(self) -> list[str]:
        return ["myrobot_is_online"]

    def register_tools(self, mcp):
        from .robot_adapter import TemplateAdapter
        from .mcp_tools import register

        register(mcp, TemplateAdapter())

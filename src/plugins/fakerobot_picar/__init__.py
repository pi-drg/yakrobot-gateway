"""Fake PiCar — the PiCar plugin against a simulator instead of hardware.

Exists so the realtime teleop path (gateway WebSocket proxy → robot
``/ws/control`` and ``/ws/video``) can be exercised end to end with no robot,
no Raspberry Pi, and no one standing next to a car in Finland. It exposes the
same MCP tools and the same HTTP surface as the real car, so code that works
here is code that works there.

Start the simulator alongside the gateway::

    uv run python -m plugins.fakerobot_picar.simulator     # port 8081

Point it elsewhere with ``FAKEROBOT_PICAR_URL``.

Note on imports: everything robot-specific is imported *inside* methods.
``plugins.discover_plugins`` scans each plugin module's attributes for
RobotPlugin subclasses, so importing ``PicarFreenovePlugin`` at module level
here would register that class under this plugin's name and silently shadow
this one.
"""

from core.plugin import RobotPlugin, RobotMetadata


class FakePicarPlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="FakePiCar-Sim",
            description=(
                "A simulated Freenove 4WD mecanum car: the full PiCar control "
                "surface plus realtime WebSocket control and video, rendered "
                "rather than driven. For developing teleop without hardware."
            ),
            robot_type="mobile_robot",
            url_prefix="fakerobot_picar",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
            bidding_terms=None,
        )

    def tool_names(self) -> list[str]:
        # Deferred to the real plugin so the two surfaces cannot drift.
        from plugins.picar_freenove import PicarFreenovePlugin

        return PicarFreenovePlugin().tool_names()

    def register_tools(self, mcp):
        from plugins.picar_freenove.mcp_tools import register

        from .robot_adapter import make_adapter

        self.adapter = make_adapter()
        register(mcp, self.adapter)

    def control_base_urls(self) -> list[str]:
        from .robot_adapter import control_base_urls

        return control_base_urls()

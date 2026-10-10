"""PiCar Adeept — Adeept AWR-V3 4WD car on a Raspberry Pi, over MCP.

The car runs `picar_adeept_fastapi` (github.com/pi-drg/picar_adeept_fastapi),
which serves the same HTTP and WebSocket surface as the Freenove car's
`picar_freenove_fastapi`. That is why this plugin is a near-copy of
`picar_freenove`, and why the browser console and `/ws/*` proxy work for it
unchanged. The tool set differs only where the hardware does: no strafing, no
pan, no ultrasonic sweep, and a buzzer.

Point it at your car with `PICAR_ADEEPT_URL` (default
`http://picar-adeept.local:8080`; a comma-separated candidate list is fine), and
set `PICAR_ADEEPT_TOKEN` if the robot runs with `ROBOT_TOKEN` configured.

`url_prefix` must match the package name: the gateway mounts each robot at
`/{plugin_name}/mcp`, and a mismatch makes the descriptor advertise a 404.
"""

from core.plugin import RobotPlugin, RobotMetadata


class PicarAdeeptPlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            # A unit label, like the Freenove car's "PiCar-Finland-01" —
            # placeholder until this car has a home.
            name="PiCar-Adeept-01",
            description=(
                "An Adeept AWR-V3 4WD car on a Raspberry Pi: drive and turn, "
                "tilting camera with snapshots, forward ultrasonic distance, "
                "infrared line sensors, battery telemetry, eight RGB LEDs and "
                "a buzzer."
            ),
            robot_type="mobile_robot",
            url_prefix="picar_adeept",
            # Fleet identity is assigned by whoever registers the robot
            # on-chain, not claimed here.
            fleet_provider="",
            fleet_domain="",
            # Not participating in the task marketplace.
            bidding_terms=None,
        )

    def tool_names(self) -> list[str]:
        return [
            "picar_adeept_is_online",
            "picar_adeept_capabilities",
            "picar_adeept_battery",
            "picar_adeept_drive",
            "picar_adeept_move",
            "picar_adeept_stop",
            "picar_adeept_look",
            "picar_adeept_distance",
            "picar_adeept_line",
            "picar_adeept_snapshot",
            "picar_adeept_led",
            "picar_adeept_beep",
        ]

    def control_base_urls(self) -> list[str]:
        """The car's own FastAPI server — the gateway proxies /ws/* to it."""
        from .robot_adapter import control_base_urls

        return control_base_urls()

    def control_auth_token(self) -> str | None:
        """PICAR_ADEEPT_TOKEN, or None when the car runs with auth disabled."""
        from .robot_adapter import control_token

        return control_token() or None

    def register_tools(self, mcp):
        from .robot_adapter import PicarAdeeptAdapter
        from .mcp_tools import register

        self.adapter = PicarAdeeptAdapter()
        register(mcp, self.adapter)

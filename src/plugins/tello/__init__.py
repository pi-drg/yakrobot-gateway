import time

from core.robot_marketplace_tools import MARKETPLACE_TOOL_NAMES
from core.plugin import BiddingTerms, RobotPlugin, RobotMetadata


class TelloPlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="DJI Tello Drone",
            description="A DJI Tello quadrotor drone controllable via MCP.",
            robot_type="quadrotor",
            url_prefix="tello",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
            bidding_terms=BiddingTerms(
                min_price_cents=100,
                rate_per_minute_cents=50,
                currency="usd",
                accepted_task_types=["camera"],
                max_duration_secs=300,
                max_concurrent_tasks=1,
                requires_approval=True,
            ),
        )

    def tool_names(self) -> list[str]:
        return [
            "tello_takeoff",
            "tello_land",
            "tello_move",
            "tello_rotate",
            "tello_flip",
            "tello_get_status",
            "tello_get_attitude",
            "tello_get_drone_info",
            "tello_is_online",
            *MARKETPLACE_TOOL_NAMES,
        ]

    def register_tools(self, mcp):
        from .robot_adapter import TelloAdapter
        from .mcp_tools import register

        self.adapter = TelloAdapter()
        register(mcp, self.adapter)

    async def bid(self, task_spec: dict) -> dict | None:
        """Generate a bid for visual_inspection (camera) tasks."""
        # Category filter — Tello handles aerial camera tasks only
        if task_spec.get("task_category") not in ("visual_inspection", "camera"):
            return None

        # Budget check
        terms = self.metadata().bidding_terms
        min_price = terms.min_price_cents / 100
        if task_spec.get("budget_ceiling", 0) < min_price:
            return None

        # Liveness check
        adapter = getattr(self, "adapter", None)
        if adapter is None:
            from .robot_adapter import TelloAdapter
            adapter = TelloAdapter()
        liveness = await adapter.is_online()
        if not liveness.get("online"):
            return None

        return {
            "price": min_price,
            "currency": "usd",
            "sla_commitment_seconds": 120,
            "confidence": 0.90,
            "capabilities_offered": ["aerial_photo", "video_inspection", "360_scan"],
            "notes": "DJI Tello, 720p camera. Requires 30 cm clearance for takeoff.",
        }

    async def execute(self, task_id: str, task_description: str, parameters: dict) -> dict:
        """Perform a brief aerial inspection and return flight telemetry as delivery_data."""
        adapter = getattr(self, "adapter", None)
        if adapter is None:
            from .robot_adapter import TelloAdapter
            adapter = TelloAdapter()

        start = time.monotonic()
        partial_data: dict = {}

        try:
            # Pre-flight status
            pre_status = await adapter.get_status()
            partial_data["pre_flight_status"] = pre_status

            # Take off and hover for inspection
            await adapter.takeoff()

            # Capture in-flight telemetry for the inspection report
            attitude = await adapter.get_attitude()
            in_status = await adapter.get_status()
            partial_data["in_flight_attitude"] = attitude
            partial_data["in_flight_status"] = in_status

            # Land
            await adapter.land()

        except Exception as e:
            # Attempt safe landing before reporting failure
            try:
                await adapter.land()
            except Exception:
                pass
            return {
                "success": False,
                "error": str(e),
                "partial_data": partial_data,
            }

        duration = round(time.monotonic() - start, 2)
        height = in_status.get("height_cm")
        battery = in_status.get("battery")

        return {
            "success": True,
            "delivery_data": {
                "readings": [
                    {"type": "aerial_altitude", "value": height, "unit": "cm"},
                    {"type": "battery", "value": battery, "unit": "percent"},
                ],
                "summary": (
                    f"Visual inspection complete. "
                    f"Hover altitude: {height} cm, battery remaining: {battery}%."
                ),
                "robot_id": "tello",
                "robot_name": self.metadata().name,
                "duration_seconds": duration,
                "telemetry": partial_data,
            },
        }

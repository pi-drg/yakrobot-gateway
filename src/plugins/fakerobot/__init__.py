import time

from core.robot_marketplace_tools import MARKETPLACE_TOOL_NAMES
from core.plugin import BiddingTerms, RobotPlugin, RobotMetadata


class FakeRobotPlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="FakeRobot-Finland-01",
            description="A simulated differential-drive rover for development and testing.",
            robot_type="differential_drive",
            url_prefix="fakerobot",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
            bidding_terms=BiddingTerms(
                min_price_cents=50,
                rate_per_minute_cents=10,
                currency="usd",
                accepted_task_types=["sensor_reading"],
                max_duration_secs=180,
                max_concurrent_tasks=1,
                requires_approval=True,
            ),
        )

    def tool_names(self) -> list[str]:
        return [
            "fakerobot_move",
            "fakerobot_is_online",
            "fakerobot_get_temperature_humidity",
            *MARKETPLACE_TOOL_NAMES,
        ]

    def register_tools(self, mcp):
        from .robot_adapter import FakeRobotAdapter
        from .mcp_tools import register

        self.adapter = FakeRobotAdapter()
        register(mcp, self.adapter)

    async def bid(self, task_spec: dict) -> dict | None:
        """Generate a bid for env_sensing tasks after verifying liveness and sensors."""
        from .robot_adapter import FakeRobotAdapter

        # Category filter — only bid on sensor/environmental tasks
        if task_spec.get("task_category") not in ("env_sensing", "sensor_reading"):
            return None

        # Capability check
        reqs = task_spec.get("capability_requirements") or {}
        required_sensors = set(reqs.get("sensors_required", []))
        if required_sensors and not required_sensors.issubset({"temperature", "humidity"}):
            return None  # buyer needs sensors we don't have

        # Budget check
        terms = self.metadata().bidding_terms
        min_price = terms.min_price_cents / 100
        if task_spec.get("budget_ceiling", 0) < min_price:
            return None

        # Liveness check
        adapter = getattr(self, "adapter", None) or FakeRobotAdapter()
        try:
            await adapter.get("/info")
            await adapter.get("/sensor/ht")
        except Exception:
            return None  # simulator offline or sensor failed

        return {
            "price": min_price,
            "currency": "usd",
            "sla_commitment_seconds": 30,
            "confidence": 0.95,
            "capabilities_offered": ["temperature", "humidity"],
            "notes": "AHT20 sensor (simulated), accuracy ±0.3°C / ±2% RH.",
        }

    async def execute(self, task_id: str, task_description: str, parameters: dict) -> dict:
        """Read sensor data and return delivery_data."""
        from .robot_adapter import FakeRobotAdapter

        adapter = getattr(self, "adapter", None) or FakeRobotAdapter()
        start = time.monotonic()
        try:
            sensor_data = await adapter.get("/sensor/ht")
        except Exception as e:
            return {"success": False, "error": f"Sensor read failed: {e}", "partial_data": {}}

        duration = round(time.monotonic() - start, 2)
        temp = sensor_data.get("temperature")
        hum = sensor_data.get("humidity")
        meta = self.metadata()

        return {
            "success": True,
            "delivery_data": {
                "readings": [
                    {"type": "temperature", "value": temp, "unit": "celsius"},
                    {"type": "humidity", "value": hum, "unit": "percent_rh"},
                ],
                "summary": f"Temperature: {temp}°C, Humidity: {hum}%",
                "robot_id": "simulator",
                "robot_name": meta.name,
                "duration_seconds": duration,
            },
        }

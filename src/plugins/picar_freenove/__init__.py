"""PiCar Freenove — Freenove 4WD Smart Car on a Raspberry Pi, over MCP.

The car runs `picar_freenove_fastapi` (github.com/pi-drg/picar_freenove_fastapi),
which exposes an HTTP control surface on the Pi itself. This plugin adapts that
surface to MCP tools; the gateway runs off-board and reaches the car over the
network.

Point it at your car with `PICAR_FREENOVE_URL` (default
`http://picar-freenove.local:8080`), and set `PICAR_FREENOVE_TOKEN` if the robot
is running with `ROBOT_TOKEN` configured.

Naming: the package directory must be an importable Python module, so it is
`picar_freenove` with an underscore, and `url_prefix` matches it because the
gateway mounts each robot at `/{plugin_name}/mcp` — a mismatch would make the
exported descriptor advertise an endpoint that 404s.
"""

import base64
import os
import time

from core.plugin import BiddingTerms, RobotPlugin, RobotMetadata

# What this car can honestly promise while parked. The camera mount pans and
# tilts; the wheels never turn during a task — see execute().
SENSORS_OFFERED = ("distance", "battery", "line")
MODALITIES_OFFERED = ("still_image", "visible_light", "pan_tilt")

# Requirements that mean the buyer expects the car to drive somewhere. This
# plugin declines those rather than half-performing them; a buyer hears "no"
# while choosing an operator, instead of discovering it after payment.
MOBILITY_WORDS = ("navigat", "waypoint", "drive", "roam", "traverse", "autonomous", "patrol")

# The car's calibrated servo travel; the robot clamps again server-side.
SERVO_MIN, SERVO_MAX = 30, 150


def _wants_mobility(requirements: dict) -> bool:
    """True when any capability requirement implies the car must move to a site."""
    for key, value in (requirements or {}).items():
        text = " ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
        if any(word in f"{key} {text}".lower() for word in MOBILITY_WORDS):
            return True
    return False


def _servo_angle(value) -> int | None:
    """Pan/tilt degrees, clamped to travel; None when the caller didn't ask."""
    if value is None:
        return None
    try:
        return max(SERVO_MIN, min(SERVO_MAX, int(value)))
    except (TypeError, ValueError):
        return None


class PicarFreenovePlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="PiCar-Finland-01",
            description=(
                "A Freenove 4WD Smart Car on a Raspberry Pi: drive and strafe, "
                "pan/tilt camera with snapshots, ultrasonic distance and sweep, "
                "infrared line sensors, battery telemetry and an addressable "
                "LED ring."
            ),
            # Mecanum-equipped cars are holonomic, but the same plugin serves
            # cars with ordinary wheels, so the broader classification is the
            # honest one. picar_freenove_capabilities reports the actual build.
            robot_type="mobile_robot",
            url_prefix="picar_freenove",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
            # Marketplace participation: standing inspection and sensing jobs
            # only — the car answers a task where it is already parked, and
            # execute() never drives it (see that method). Pricing is the
            # operator's call, not something to infer from the hardware: 75¢
            # floor and 15¢/minute carried over from the PiZero that ran this
            # marketplace before, and requires_approval keeps a human on the
            # trigger for a physical machine.
            bidding_terms=BiddingTerms(
                min_price_cents=75,
                rate_per_minute_cents=15,
                currency="usd",
                accepted_task_types=["visual_inspection", "env_sensing"],
                max_duration_secs=120,
                max_concurrent_tasks=1,
                requires_approval=True,
            ),
        )

    def tool_names(self) -> list[str]:
        return [
            "picar_freenove_is_online",
            "picar_freenove_capabilities",
            "picar_freenove_battery",
            "picar_freenove_drive",
            "picar_freenove_move",
            "picar_freenove_stop",
            "picar_freenove_look",
            "picar_freenove_distance",
            "picar_freenove_scan",
            "picar_freenove_line",
            "picar_freenove_snapshot",
            "picar_freenove_led",
        ]

    def control_base_urls(self) -> list[str]:
        """The car's own FastAPI server — the gateway proxies /ws/* to it.

        Same candidates the MCP adapter resolves from, deliberately: both must
        agree on which car they are talking to.
        """
        from .robot_adapter import control_base_urls

        return control_base_urls()

    def control_auth_token(self) -> str | None:
        """PICAR_FREENOVE_TOKEN, or None when the car runs with auth disabled."""
        from .robot_adapter import control_token

        return control_token() or None

    def register_tools(self, mcp):
        from .robot_adapter import PicarFreenoveAdapter
        from .mcp_tools import register

        self.adapter = PicarFreenoveAdapter()
        register(mcp, self.adapter)

    # --- marketplace (robot_submit_bid / robot_execute_task) ------------------

    def _adapter(self):
        """The adapter register_tools() built, or a fresh one when called standalone."""
        adapter = getattr(self, "adapter", None)
        if adapter is None:
            from .robot_adapter import PicarFreenoveAdapter

            adapter = PicarFreenoveAdapter()
        return adapter

    async def bid(self, task_spec: dict) -> dict | None:
        """Answer a marketplace call for tender, or decline.

        Declining is the honest answer to most of what a task marketplace asks:
        work that needs the car to travel, sensors it doesn't carry, a budget
        under the floor, or a car that is simply unreachable right now.
        """
        terms = self.metadata().bidding_terms
        category = task_spec.get("task_category")
        if category not in terms.accepted_task_types:
            return None

        requirements = task_spec.get("capability_requirements") or {}
        if _wants_mobility(requirements):
            return None

        if category == "visual_inspection":
            required, offered = set(requirements.get("modalities_required", [])), list(MODALITIES_OFFERED)
        else:
            required, offered = set(requirements.get("sensors_required", [])), list(SENSORS_OFFERED)
        if required and not required.issubset(offered):
            return None

        min_price = terms.min_price_cents / 100
        if task_spec.get("budget_ceiling", 0) < min_price:
            return None

        # Liveness: a bid is a promise about a car that has to still be there.
        capabilities = await self._adapter().capabilities()
        if not capabilities.get("ok"):
            return None

        return {
            "price": min_price,
            "currency": terms.currency,
            "sla_commitment_seconds": min(int(task_spec.get("sla_seconds") or 60),
                                          terms.max_duration_secs),
            "confidence": 0.9,
            "capabilities_offered": offered,
            "notes": (
                "Freenove 4WD car with a pan/tilt camera, ultrasonic distance and "
                "line sensors. It serves inspection and sensing tasks from where it "
                "is parked and does not drive to a site on the buyer's behalf."
            ),
        }

    async def execute(self, task_id: str, task_description: str, parameters: dict) -> dict:
        """Deliver a reading, or a photograph, from where the car is standing.

        Deliberately motionless — no `/drive` or `/mecanum` call is made here,
        ever. A marketplace buyer is not the operator watching the car, this
        chassis is open-loop with no encoders, and a wrong move on a real floor
        is not something a task can undo. Only the camera mount moves, inside
        its calibrated travel.

        ``parameters["task_category"] == "visual_inspection"`` (or an explicit
        ``include_image``) asks for a still; anything else returns readings. A
        pan/tilt pair re-aims the camera first; one without the other is ignored
        rather than guessed at.
        """
        parameters = parameters or {}
        adapter = self._adapter()
        start = time.monotonic()
        partial: dict = {}

        wants_image = (
            str(parameters.get("task_category", "")).lower() == "visual_inspection"
            or bool(parameters.get("include_image"))
        )

        # Readings ride along with every delivery: cheap, and they make a
        # photograph defensible ("124 cm from the obstruction, 8.0 V").
        distance = await adapter.distance()
        partial["distance"] = distance
        if not distance.get("ok"):
            return {"success": False,
                    "error": f"Distance reading failed: {distance.get('error', distance)}",
                    "partial_data": partial}
        readings = [{"type": "distance", "value": distance.get("cm"), "unit": "cm"}]

        battery = await adapter.battery()
        partial["battery"] = battery
        if battery.get("ok"):
            readings.append({"type": "battery_voltage", "value": battery.get("volts"),
                             "unit": "volts"})

        delivery: dict = {"readings": readings}

        if wants_image:
            pan, tilt = _servo_angle(parameters.get("pan")), _servo_angle(parameters.get("tilt"))
            if pan is not None and tilt is not None:
                aim = await adapter.look(pan, tilt)
                partial["look"] = aim
                if not aim.get("ok"):
                    return {"success": False,
                            "error": f"Camera aim failed: {aim.get('error', aim)}",
                            "partial_data": partial}
            try:
                jpeg = await adapter.snapshot_bytes()
            except Exception as exc:
                return {"success": False, "error": f"Snapshot failed: {exc}",
                        "partial_data": partial}
            delivery["capture"] = {
                "image_base64": base64.b64encode(jpeg).decode("ascii"),
                "mime_type": "image/jpeg",
                "size_bytes": len(jpeg),
                "pan": pan,
                "tilt": tilt,
            }

        meta = self.metadata()
        volts = battery.get("volts") if battery.get("ok") else None
        delivery.update({
            "summary": (
                f"Still image captured {distance.get('cm')} cm from the nearest "
                f"obstruction, battery {volts} V."
                if wants_image else
                f"Distance to nearest obstruction {distance.get('cm')} cm, "
                f"battery {volts} V."
            ),
            "robot_id": os.getenv("PICAR_FREENOVE_AGENT_ID") or meta.url_prefix,
            "robot_name": meta.name,
            "duration_seconds": round(time.monotonic() - start, 2),
            "requested_work": task_description,
            "stationary": True,
        })
        partial["delivery"] = "complete"
        return {"success": True, "delivery_data": delivery}

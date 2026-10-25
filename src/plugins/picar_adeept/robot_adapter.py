"""Robot-facing adapter for the Adeept AWR-V3 4WD car.

Speaks the HTTP surface served on the car's Raspberry Pi by
`picar_adeept_fastapi` (github.com/pi-drg/picar_adeept_fastapi). That server
deliberately serves the Freenove contract, so this adapter is the Freenove one
with the car's differences layered on top:

* **One servo.** The camera tilts but cannot pan; `/look` takes `tilt` only.
* **No sweep.** The ultrasonic sensor is fixed to the bumper — `/scan` is a 501
  on the robot, so no scan is offered here at all.
* **Ordinary wheels.** `/mecanum` refuses `vy`; moves are sent with vy=0.
* **Buzzer.** The HAT has a passive buzzer, exposed as `/buzzer`.

Transport, error shaping and the duration/duty clamps are inherited unchanged —
the safety belt must not differ between the two cars.
"""

import os
import socket
from urllib.parse import urlparse

from plugins.picar_freenove.robot_adapter import PicarFreenoveAdapter

# The gateway is meant to run on the car's own Pi, next to the robot server,
# which then binds loopback only. Point PICAR_ADEEPT_URL elsewhere to run the
# gateway off-board.
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# The robot caps a whole buzzer sequence at its buzzer_max_ms (3000 by default);
# mirrored here so an over-long request is trimmed before it reaches the wire.
MAX_BUZZER_MS = 3000


def control_base_urls() -> list[str]:
    """Candidate URLs for the car's control server, in preference order.

    ``PICAR_ADEEPT_URL`` may hold a comma-separated list, exactly as
    ``PICAR_FREENOVE_URL`` does — see that adapter for why. Shared by this
    adapter and the gateway's WebSocket proxy, so both talk to the same car.
    """
    raw = os.getenv("PICAR_ADEEPT_URL", DEFAULT_BASE_URL)
    urls = [u.strip().rstrip("/") for u in raw.split(",")]
    return [u for u in urls if u] or [DEFAULT_BASE_URL]


def control_base_url() -> str:
    """The first candidate whose hostname resolves (resolution, not reachability)."""
    candidates = control_base_urls()
    for url in candidates:
        host = urlparse(url).hostname
        if not host:
            continue
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            continue
        return url
    return candidates[0]


def control_token() -> str:
    """The car's ROBOT_TOKEN, or "" when it runs with auth disabled."""
    return os.getenv("PICAR_ADEEPT_TOKEN", "")


class PicarAdeeptAdapter(PicarFreenoveAdapter):
    """HTTP adapter for the Adeept car's on-robot control server."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 8.0):
        # Resolve here, not in the parent: the parent's defaults read the
        # PICAR_FREENOVE_* variables and would aim this adapter at the wrong car.
        super().__init__(
            base_url=base_url or control_base_url(),
            token=token if token is not None else control_token(),
            timeout=timeout,
        )

    async def capabilities(self, refresh: bool = False) -> dict:
        """The parent's capability summary plus the servos this car has."""
        if self._caps is None or refresh:
            info = await self.get("/info")
            if not info.get("ok"):
                return info
            self._caps = {
                "ok": True,
                "online": True,
                "robot": info.get("robot"),
                "wheels": info.get("wheels", "unknown"),
                "holonomic": bool(info.get("holonomic", False)),
                "servos": info.get("servos", ["tilt"]),
                "battery_v": info.get("battery_v"),
                "battery_pct": info.get("battery_pct"),
            }
        return self._caps

    async def move(self, vx: int, omega: int, duration_ms: int) -> dict:
        """Forward/back plus rotation. No strafe: the wheels are ordinary."""
        return await self.mecanum(vx, 0, omega, duration_ms)

    async def look(self, tilt: int) -> dict:
        """Tilt the camera. The robot clamps to the bracket's travel limits."""
        return await self.post("/look", {"tilt": int(tilt)})

    async def buzzer(self, notes: list[tuple[str, int]]) -> dict:
        """Play [(note, ms), ...] on the passive buzzer, trimmed to MAX_BUZZER_MS."""
        trimmed, total = [], 0
        for note, ms in notes:
            ms = max(0, min(int(ms), MAX_BUZZER_MS - total))
            if ms <= 0:
                break
            trimmed.append({"note": note, "ms": ms})
            total += ms
        if not trimmed:
            return {"ok": False, "error": "no notes to play"}
        return await self.post("/buzzer", {"notes": trimmed})

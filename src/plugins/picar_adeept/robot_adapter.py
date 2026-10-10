"""Robot-facing adapter for the Adeept AWR-V3 PiCar.

Speaks the HTTP surface served on the car's Raspberry Pi by
`picar_adeept_fastapi` (github.com/pi-drg/picar_adeept_fastapi):

    GET  /info /battery /distance /line /snapshot /led
    POST /drive /mecanum /look /led /buzzer

That server deliberately mirrors `picar_freenove_fastapi`, so this adapter is
the picar_freenove one with the hardware differences applied: one tilt servo
and no pan, ordinary wheels only, a fixed ultrasonic sensor (its /scan answers
501 and is not wrapped here), and a buzzer.

Two things this adapter owns, so the MCP layer above stays declarative:

* **Safety clamps.** The car is open-loop with no encoders. The robot server
  auto-stops after `duration_ms`, but nothing stops a caller asking for sixty
  seconds of motion, so durations are capped here before they reach the wire.
* **Error shaping.** A refused command carries information the agent should
  act on, so HTTP errors come back as structured results rather than exceptions.
"""

import os
import socket
from urllib.parse import urlparse

import httpx

# The robot bounds each move server-side; this is a second, tighter belt for
# remote callers. Callers wanting more issue repeated moves.
MAX_DURATION_MS = 3000
MAX_DUTY = 4095
# The robot caps a buzzer sequence at 3 s itself; mirrored so a caller learns
# the limit from the tool rather than from a truncated tune.
MAX_BUZZER_MS = 3000

DEFAULT_BASE_URL = "http://picar-adeept.local:8080"


def control_base_urls() -> list[str]:
    """Candidate URLs for the car's control server, in preference order.

    ``PICAR_ADEEPT_URL`` may hold a **comma-separated list** (mDNS name and IP,
    say), because neither naming scheme is reliable alone. One source of truth
    for this adapter and the gateway's WebSocket proxy — they must agree on
    which car they are talking to.
    """
    raw = os.getenv("PICAR_ADEEPT_URL", DEFAULT_BASE_URL)
    urls = [u.strip().rstrip("/") for u in raw.split(",")]
    return [u for u in urls if u] or [DEFAULT_BASE_URL]


def control_base_url() -> str:
    """The single URL this adapter's HTTP client should use: the first
    candidate whose hostname resolves. Resolution is not reachability."""
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
    """Bearer token for the car, or "" when it runs with auth disabled."""
    return os.getenv("PICAR_ADEEPT_TOKEN", "")


def clamp_duration(ms: int) -> int:
    return max(0, min(MAX_DURATION_MS, int(ms)))


def clamp_duty(duty: int) -> int:
    return max(-MAX_DUTY, min(MAX_DUTY, int(duty)))


class PicarAdeeptAdapter:
    """HTTP adapter for the Adeept PiCar's on-robot control server."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 8.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url or control_base_url()
        token = token if token is not None else control_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers, transport=transport
        )
        self._caps: dict | None = None

    # --- transport -----------------------------------------------------------

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One request. Never raises on an HTTP error — shapes it into a result."""
        try:
            resp = await self.client.request(method, path, json=payload)
        except httpx.RequestError as exc:
            return {"ok": False,
                    "error": f"cannot reach the robot at {self.base_url}: {exc}"}
        if resp.is_error:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            return {"ok": False, "status": resp.status_code, "error": detail}
        try:
            body = resp.json()
        except Exception:
            return {"ok": True, "body": resp.text}
        if isinstance(body, dict):
            return {"ok": True, **body}
        return {"ok": True, "result": body}

    async def get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def post(self, path: str, payload: dict) -> dict:
        return await self._request("POST", path, payload)

    # --- capabilities --------------------------------------------------------

    async def capabilities(self, refresh: bool = False) -> dict:
        """What this car can do, from GET /info. Cached: it only changes when the
        car's per-unit config does."""
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

    # --- motion --------------------------------------------------------------

    async def drive(self, direction: str, duty: int, duration_ms: int) -> dict:
        """Differential preset. `left`/`right` spin in place."""
        return await self.post("/drive", {
            "direction": direction,
            "duty": clamp_duty(duty),
            "duration_ms": clamp_duration(duration_ms),
        })

    async def move(self, vx: int, omega: int, duration_ms: int) -> dict:
        """Forward speed plus rotation: +vx forward, +omega counter-clockwise.

        Goes through the robot's /mecanum endpoint with vy pinned to 0 — the
        AWR has ordinary wheels and cannot strafe.
        """
        return await self.post("/mecanum", {
            "vx": clamp_duty(vx),
            "vy": 0,
            "omega": clamp_duty(omega),
            "duration_ms": clamp_duration(duration_ms),
        })

    async def stop(self) -> dict:
        return await self.post("/drive", {"direction": "stop", "duration_ms": 0})

    async def look(self, tilt: int) -> dict:
        """Tilt the camera. The robot clamps to its own travel limits."""
        return await self.post("/look", {"tilt": int(tilt)})

    # --- sensing -------------------------------------------------------------

    async def distance(self) -> dict:
        return await self.get("/distance")

    async def line(self) -> dict:
        return await self.get("/line")

    async def battery(self) -> dict:
        return await self.get("/battery")

    async def snapshot_bytes(self) -> bytes:
        """One camera frame, as raw JPEG bytes.

        Raises instead of returning {"ok": False, ...}, because the caller
        returns an MCP Image, which has no room for an error dict; FastMCP turns
        the exception into a clean tool error.
        """
        try:
            resp = await self.client.get("/snapshot")
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"cannot reach the robot at {self.base_url}: {exc}") from exc
        if resp.is_error:
            raise RuntimeError(f"robot returned {resp.status_code}: {resp.text}")
        return resp.content

    # --- lights and sound ----------------------------------------------------

    async def led(self, **kwargs) -> dict:
        return await self.post("/led", {k: v for k, v in kwargs.items() if v is not None})

    async def buzzer(self, notes: list[tuple[str, int]]) -> dict:
        """Play [(note, ms), ...]; the total is capped at MAX_BUZZER_MS."""
        capped, total = [], 0
        for note, ms in notes:
            ms = max(0, min(int(ms), MAX_BUZZER_MS - total))
            if ms <= 0:
                break
            capped.append({"note": note, "ms": ms})
            total += ms
        return await self.post("/buzzer", {"notes": capped})

    async def aclose(self) -> None:
        await self.client.aclose()

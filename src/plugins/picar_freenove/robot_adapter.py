"""Robot-facing adapter for the Freenove 4WD PiCar.

Speaks the HTTP surface served on the car's Raspberry Pi by
`picar_freenove_fastapi` (github.com/pi-drg/picar_freenove_fastapi):

    GET  /info /battery /distance /scan /line /snapshot /led
    POST /drive /mecanum /look /led

Two things this adapter owns, so the MCP layer above stays declarative:

* **Safety clamps.** The car is open-loop with no encoders. The robot server
  auto-stops after `duration_ms`, but nothing stops a caller asking for sixty
  seconds of motion, so durations are capped here before they reach the wire.
* **Error shaping.** A refused command ("this car has ordinary wheels") carries
  information the agent should act on, so HTTP errors come back as structured
  results rather than exceptions.
"""

import os
import socket
from urllib.parse import urlparse

import httpx

# The robot bounds each move server-side; this is a second, tighter belt for
# remote callers. Long enough to be useful, short enough that a mistake is a
# nudge rather than a journey. Callers wanting more issue repeated moves.
MAX_DURATION_MS = 3000
MAX_DUTY = 4095

DEFAULT_BASE_URL = "http://picar-freenove.local:8080"


def control_base_urls() -> list[str]:
    """Candidate URLs for the car's control server, in preference order.

    ``PICAR_FREENOVE_URL`` may hold a **comma-separated list**, so a unit can be
    named both ways at once:

        PICAR_FREENOVE_URL=http://picar-finland-01.local:8080,http://192.168.8.201:8080

    Neither form is reliable alone — the mDNS name survives the DHCP lease
    moving but needs a working resolver; the IP needs no resolver but goes
    stale when the lease moves. Listing both means whichever is true today
    wins. A single URL (the common case) is just a one-element list.

    One source of truth for this adapter and the gateway's WebSocket proxy
    (``core.ws_proxy``) — they must agree on which car they are talking to, and
    a second ``os.getenv`` with its own default is how that quietly stops being
    true.
    """
    raw = os.getenv("PICAR_FREENOVE_URL", DEFAULT_BASE_URL)
    urls = [u.strip().rstrip("/") for u in raw.split(",")]
    return [u for u in urls if u] or [DEFAULT_BASE_URL]


def control_base_url() -> str:
    """The single URL this adapter's HTTP client should use.

    httpx has no multi-host failover, so the choice is made once here: the first
    candidate whose hostname actually resolves. That covers the case this exists
    for — an mDNS name listed first on a host with no working resolver falls
    through to the IP — without paying a reachability probe on every call.

    Resolution is not reachability: a name that resolves to a stale address is
    still chosen. The candidate list fixes naming, not a car that is switched
    off.
    """
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
    """Bearer token for the car, or "" when it runs with auth disabled.

    Empty is the normal state on a trusted LAN (the robot's ROBOT_TOKEN unset).
    """
    return os.getenv("PICAR_FREENOVE_TOKEN", "")


def clamp_duration(ms: int) -> int:
    return max(0, min(MAX_DURATION_MS, int(ms)))


def clamp_duty(duty: int) -> int:
    return max(-MAX_DUTY, min(MAX_DUTY, int(duty)))


class PicarFreenoveAdapter:
    """HTTP adapter for the PiCar's on-robot control server."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 8.0):
        self.base_url = base_url or control_base_url()
        # Set PICAR_FREENOVE_TOKEN when the robot runs with ROBOT_TOKEN configured.
        # Absent means the robot has auth disabled — only sane on a trusted LAN.
        token = token if token is not None else control_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers
        )
        self._caps: dict | None = None

    # --- transport -----------------------------------------------------------

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One request. Never raises on an HTTP error — shapes it into a result.

        A 400 from /mecanum ("this car has ordinary wheels") is a fact the agent
        needs in order to choose a different move, not a crash. A connection
        failure is likewise reportable: the car is battery-powered and drops off
        the network when the pack dies.
        """
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
        """What this particular car can do, from GET /info.

        Wheel type is per-unit: the same robot code drives cars with mecanum
        wheels (holonomic — can strafe) and with ordinary wheels (cannot).
        Cached, because it only changes when someone physically swaps the wheels
        and re-runs the robot's calibration.
        """
        if self._caps is None or refresh:
            info = await self.get("/info")
            if not info.get("ok"):
                return info
            self._caps = {
                "ok": True,
                "online": True,
                "robot": info.get("robot"),
                "wheels": info.get("wheels", "unknown"),
                # Older robot builds predate these fields. Absent means we cannot
                # promise strafing, so assume the safer answer.
                "holonomic": bool(info.get("holonomic", False)),
                "battery_v": info.get("battery_v"),
            }
        return self._caps

    # --- motion --------------------------------------------------------------

    async def drive(self, direction: str, duty: int, duration_ms: int) -> dict:
        """Differential preset. `left`/`right` spin in place; they are not strafes."""
        return await self.post("/drive", {
            "direction": direction,
            "duty": clamp_duty(duty),
            "duration_ms": clamp_duration(duration_ms),
        })

    async def mecanum(self, vx: int, vy: int, omega: int, duration_ms: int) -> dict:
        """Holonomic motion: +vx forward, +vy strafe LEFT, +omega counter-clockwise.

        Matches ROS REP-103. A non-zero `vy` on a car with ordinary wheels is
        refused by the robot with HTTP 400 rather than silently scrubbing tyres.
        """
        return await self.post("/mecanum", {
            "vx": clamp_duty(vx),
            "vy": clamp_duty(vy),
            "omega": clamp_duty(omega),
            "duration_ms": clamp_duration(duration_ms),
        })

    async def stop(self) -> dict:
        return await self.post("/drive", {"direction": "stop", "duration_ms": 0})

    async def look(self, pan: int, tilt: int) -> dict:
        """Aim the camera head. The robot clamps to its own travel limits."""
        return await self.post("/look", {"pan": int(pan), "tilt": int(tilt)})

    # --- sensing -------------------------------------------------------------

    async def distance(self) -> dict:
        return await self.get("/distance")

    async def scan(self) -> dict:
        return await self.get("/scan")

    async def line(self) -> dict:
        return await self.get("/line")

    async def battery(self) -> dict:
        return await self.get("/battery")

    async def snapshot_bytes(self) -> bytes:
        """One camera frame, as raw JPEG bytes.

        /snapshot returns raw bytes on the wire, not the JSON _request() expects
        — a plain `curl` or browser needs nothing extra that way. Raises instead
        of returning {"ok": False, ...} like the other methods, because the
        caller (picar_freenove_snapshot) returns an MCP Image, which has no room
        for an error dict; FastMCP turns a raised exception into a clean tool
        error the agent sees as a failure message, not a crash.
        """
        try:
            resp = await self.client.get("/snapshot")
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"cannot reach the robot at {self.base_url}: {exc}") from exc
        if resp.is_error:
            raise RuntimeError(f"robot returned {resp.status_code}: {resp.text}")
        return resp.content

    # --- lights --------------------------------------------------------------

    async def led(self, **kwargs) -> dict:
        return await self.post("/led", {k: v for k, v in kwargs.items() if v is not None})

    async def aclose(self) -> None:
        await self.client.aclose()

"""Reverse-proxy realtime WebSockets from the gateway through to a robot.

The gateway is the only host with a public URL — the tunnel terminates here
(``core/tunnel.py``), not on the robot, which sits on a private LAN with no
inbound ports. A browser driving the car therefore cannot open a socket to the
robot directly; it opens one to ``/{robot}/ws/<path>`` here and this module
carries it the last LAN hop.

**This is two sockets spliced, not a pass-through.** A WebSocket cannot be
forwarded the way an HTTP request can: the gateway terminates the browser's
socket, opens its own client socket to the robot, and pumps frames between
them until either end closes.

    browser --wss--> tunnel edge --ws--> gateway :8000
                                            |  accepts the Upgrade, then
                                            |  connects outward as a client
                                            v
                              ws://picar-finland-01.local:8080/ws/control

Three properties this proxy must preserve, all of them load-bearing for the
safety model that lives on the robot:

1. **No queueing.** The control protocol sends *absolute* velocity state, so a
   lost frame is harmless but a *late* one is not: a buffered frame delivered
   after a stop would re-apply a stale velocity. Every relay here is a direct
   ``await send`` with no intermediate queue, so backpressure propagates to the
   robot, which then simply emits fewer frames.
2. **Prompt close in both directions.** The robot's deadman timer is the
   backstop, but a half-open socket held after the browser vanishes delays the
   robot's own ``stop()``-on-disconnect. When either side ends, the other is
   torn down immediately.
3. **No state of its own.** Deadman, duty caps and the single-driver slot are
   enforced on the robot, where the hardware is. This module adds none of them
   and must not start. The brief reachability cache below is not an exception:
   it remembers only that a connect just failed, which changes how quickly a
   refusal is returned, never what the robot is permitted to do.
"""

import asyncio
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlparse

import websockets
from fastapi import FastAPI
from starlette.websockets import WebSocket

from core.plugin import RobotPlugin

logger = logging.getLogger(__name__)

# JPEG video frames are the big ones — ~20-40 KB at 400x300, but a unit
# configured for a larger stream should not hit a wall in the proxy before it
# hits one on the robot. Generous, but still a bound: unset would remove the
# only guard against a runaway frame exhausting gateway memory.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# Fail fast when a robot is off or unreachable, so the browser gets a prompt
# refusal rather than hanging on a socket that will never carry anything.
CONNECT_TIMEOUT_S = 5.0

# How long one failed connect suppresses further attempts for that robot. A
# console whose robot is off retries two sockets on a timer, and without this
# every retry re-runs the whole candidate list and its connect timeouts. Short
# enough that a robot finishing its boot is picked up on the next retry.
OFFLINE_CACHE_S = 3.0


def _ws_url(base_url: str, path: str, query: str) -> str:
    """Turn a robot's HTTP base URL into the ws:// URL for one of its sockets."""
    parts = urlparse(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    prefix = parts.path.rstrip("/")
    url = f"{scheme}://{parts.netloc}{prefix}/ws/{path}"
    return f"{url}?{query}" if query else url


def _upstream_query(query: str, robot_token: str | None, gateway_auth: bool) -> str:
    """Build the query string for the upstream handshake.

    Browsers cannot set headers on a WebSocket handshake, so both the gateway
    and the robot take their credential as a ``token`` query parameter — the
    same name for two different secrets, one per leg of this proxy. Keeping
    them straight is the whole job here:

    * **Gateway auth on** — the client's ``token`` is the *gateway's*
      credential. It is consumed by the check above and stripped here: the
      robot would reject it anyway, and a browser credential has no business
      travelling further into the network than the boundary that validates it.
      The robot's own token is injected in its place, so the browser never
      holds it.
    * **Gateway auth off** (the deferred-auth state) — there is no gateway
      credential to consume, so a client-supplied ``token`` is meant for the
      robot and passes through untouched.
    """
    pairs = parse_qsl(query, keep_blank_values=True)
    if gateway_auth:
        pairs = [(k, v) for k, v in pairs if k != "token"]
    if robot_token and not any(k == "token" for k, _ in pairs):
        pairs.append(("token", robot_token))
    return urlencode(pairs)


def video_enabled() -> bool:
    """Whether ``/ws/video`` may be proxied at all.

    A hard switch for a metered or congested link: video is by far the most
    expensive thing crossing the tunnel, and this refuses it for every client
    regardless of what any browser chooses to do. Control keeps working — the
    car stays drivable, just blind.

    Set ``VIDEO_ENABLED=0`` (or false/no/off) to disable. Absent means enabled,
    so the default posture is unchanged.
    """
    raw = os.getenv("VIDEO_ENABLED", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _gateway_tokens() -> set[str]:
    """Tokens that authorise a browser to open a socket here, if any.

    Mirrors the MCP auth semantics in ``core.server``: configured means
    enforced, unset means open. This is the one place a token has to be
    switched on to close the public URL — the robot behind it may have no auth
    of its own.
    """
    from core.server import _load_tokens

    return set(_load_tokens())


async def _connect_upstream(candidates: list[str], path: str, query: str):
    """Open a client socket to the first candidate URL that accepts one.

    Candidates come from the plugin (mDNS name, IP, ...) precisely because
    neither naming scheme is reliable alone; see RobotPlugin.control_base_urls.
    """
    last_error: Exception | None = None
    for base_url in candidates:
        url = _ws_url(base_url, path, query)
        try:
            return await websockets.connect(
                url,
                open_timeout=CONNECT_TIMEOUT_S,
                max_size=MAX_FRAME_BYTES,
                # JPEG is already compressed; permessage-deflate would burn CPU
                # on both ends to make the frames very slightly larger.
                compression=None,
            ), url
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            logger.info("ws proxy: %s unreachable (%s)", url, exc)
            last_error = exc
    raise ConnectionError(
        f"no reachable candidate among {candidates!r}"
    ) from last_error


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    """Turn a browser away with a code it can actually read.

    Closing before ``accept()`` makes Starlette reject the handshake with HTTP
    403, and a browser reports *any* failed handshake as close code 1006 — the
    reason never arrives and every refusal looks alike. Accepting first costs
    one round trip and delivers the code intact, which is what lets the console
    tell "stop retrying" (1008, policy) from "try again shortly" (1013).

    Delivery is best-effort. Deciding to refuse can take seconds — long enough
    for the browser to give up, reload, or be closed — and neither the accept
    nor the close can reach a client that has already gone. That is not an
    error worth a traceback: a caller who left needs no explanation.
    """
    try:
        await ws.accept()
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


async def _pump_to_robot(ws: WebSocket, robot) -> None:
    """browser -> robot. Text (control JSON) and binary both pass through."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None:
            await robot.send(text)
            continue
        data = message.get("bytes")
        if data is not None:
            await robot.send(data)


async def _pump_to_browser(ws: WebSocket, robot) -> None:
    """robot -> browser. Video frames arrive binary, telemetry as text."""
    async for message in robot:
        if isinstance(message, str):
            await ws.send_text(message)
        else:
            await ws.send_bytes(message)


def register_ws_proxy(app: FastAPI, plugins: dict[str, RobotPlugin]) -> None:
    """Add ``/{robot}/ws/{path}`` to the gateway.

    **Must be called before the per-robot MCP apps are mounted.** Starlette
    matches routes in registration order and ``app.mount("/picar_freenove", ...)``
    claims everything beneath that prefix — mount first and this route is dead
    code that never sees a request.
    """
    proxied = {
        name: (p.control_base_urls(), p.control_auth_token())
        for name, p in plugins.items()
    }
    proxied = {name: entry for name, entry in proxied.items() if entry[0]}

    if not proxied:
        logger.info("ws proxy: no plugin exposes a control server; /ws/* not served")
        return
    for name, (urls, _) in proxied.items():
        logger.info("ws proxy: /%s/ws/* -> %s", name, ", ".join(urls))

    # Reachability, remembered briefly. Not safety state — deadman, duty caps
    # and the single-driver slot stay on the robot, as the module docstring
    # requires; this only remembers that the last connect attempt failed, which
    # changes how fast we say no, never what the robot is allowed to do.
    offline_until: dict[str, float] = {}
    offline_logged: set[str] = set()

    @app.websocket("/{robot}/ws/{path:path}")
    async def robot_ws_proxy(ws: WebSocket, robot: str, path: str):
        entry = proxied.get(robot)
        if not entry:
            # Unknown robot, or one with no HTTP control server (the Tello
            # speaks UDP). Permanent, so send the code that stops the retries.
            await _refuse(ws, 1008, "no realtime socket for this robot")
            return
        candidates, robot_token = entry

        if path.strip("/") == "video" and not video_enabled():
            # 1008 (policy violation) rather than a generic error: the console
            # keys off this code to stop retrying, instead of reconnecting into
            # a refusal every second.
            await _refuse(ws, 1008, "video disabled on the gateway")
            return

        gateway_tokens = _gateway_tokens()
        if gateway_tokens:
            supplied = dict(parse_qsl(ws.url.query, keep_blank_values=True)).get("token", "")
            if supplied not in gateway_tokens:
                await ws.close(code=1008, reason="unauthorized")
                return

        query = _upstream_query(ws.url.query, robot_token, bool(gateway_tokens))

        loop = asyncio.get_running_loop()
        if offline_until.get(robot, 0.0) > loop.time():
            # A connect attempted moments ago found nothing there. Answering
            # from that verdict keeps a retrying console cheap: no candidate
            # sweep, no connect timeouts, no second log line.
            await _refuse(ws, 1013, "robot offline")
            return

        try:
            robot_ws, url = await _connect_upstream(candidates, path, query)
        except ConnectionError as exc:
            # Dated from here, not from before the sweep: an unreachable mDNS
            # name can absorb the whole connect timeout, and an entry stamped
            # with the pre-sweep clock would already have expired on arrival.
            offline_until[robot] = loop.time() + OFFLINE_CACHE_S
            # Once per outage, not once per retry — a console reconnecting on a
            # timer would otherwise bury every other line in the log.
            if robot not in offline_logged:
                offline_logged.add(robot)
                logger.warning("ws proxy: %s is offline — %s", robot, exc)
            # 1013 (try again later), not 1008: the robot may simply be
            # rebooting, and the console must keep retrying so it reconnects
            # on its own when the robot comes back.
            await _refuse(ws, 1013, "robot offline")
            return

        offline_until.pop(robot, None)
        if robot in offline_logged:
            offline_logged.discard(robot)
            # Warning, not info, purely so it is visible: uvicorn leaves this
            # module's logger at the root level, where info is filtered out.
            # An outage that logs its start and not its end reads like an
            # outage that never ended.
            logger.warning("ws proxy: %s is back", robot)

        await ws.accept()
        logger.info("ws proxy: %s/ws/%s <-> %s", robot, path, url)

        async with robot_ws:
            tasks = [
                asyncio.create_task(_pump_to_robot(ws, robot_ws)),
                asyncio.create_task(_pump_to_browser(ws, robot_ws)),
            ]
            try:
                _, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                # One direction ended; the socket is finished either way. Tear
                # the other down now rather than leaving the robot streaming
                # video into a browser that has gone.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                try:
                    await ws.close()
                except Exception:
                    # Already closed by the peer, or closed so abruptly that the
                    # server's own socket machinery is half torn down. A browser
                    # vanishing mid-drive is routine teleop, not an incident.
                    pass
        logger.info("ws proxy: %s/ws/%s closed", robot, path)

"""Operator UI session (§0.16).

``/{robot}/ui/`` serves a pre-built SPA behind a signed, scoped cookie. The bundle's
assets are public code (no cookie), but the ``index.html`` shell — and any SPA
sub-route — require a session, because that is the page that then calls the relay
(§0.17, added in a later step).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from core.plugin import ApiRoute, RobotPlugin, StaticUi
from core.ws_proxy import _gateway_token_clients, _public_origin, _pump_to_browser, _pump_to_robot, _refuse

logger = logging.getLogger(__name__)

UI_COOKIE = "yk_ui"
_DEFAULT_SESSION_HOURS = 8

# One key per process: a gateway restart logs every operator out. Never logged, never
# derived from anything else.
_SESSION_KEY = secrets.token_bytes(32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _session_hours() -> int:
    raw = os.getenv("UI_SESSION_HOURS", str(_DEFAULT_SESSION_HOURS)).strip()
    try:
        hours = int(raw)
    except ValueError:
        raise ValueError(f"UI_SESSION_HOURS must be an integer, got {raw!r}") from None
    if not 1 <= hours <= 72:
        raise ValueError(f"UI_SESSION_HOURS must be 1–72, got {hours}")
    return hours


def _sign(payload: str) -> str:
    return _b64url(hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).digest())


def _make_cookie(robot: str, client_id: str, now: int, hours: int) -> str:
    payload = _b64url(json.dumps({"c": client_id, "r": robot, "exp": now + hours * 3600}).encode())
    return f"{payload}.{_sign(payload)}"


def _verify_cookie(robot: str, value: str, now: int) -> str | None:
    """Return the ``client_id`` when the cookie is valid for ``robot``, else ``None``."""
    try:
        payload, sig = value.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_b64url_decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("r") != robot:
        return None
    try:
        if int(data["exp"]) <= now:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    client_id = data.get("c")
    # The client must *still* be an admitted token's client_id — removing a token from
    # MCP_TOKENS_FILE ends its UI sessions without a restart.
    if not isinstance(client_id, str) or client_id not in _gateway_token_clients().values():
        return None
    return client_id


def _login_page(robot: str, status: int, message: str) -> HTMLResponse:
    # Inline HTML/CSS, no scripts. The token travels only in the POST body.
    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Operator login</title>'
        '<style>body{font-family:system-ui,sans-serif;max-width:20rem;margin:4rem auto;'
        'color:#2c2c2a;line-height:1.5}label{display:block;margin-top:.75rem;'
        'font-size:.85rem}input,button{font:inherit;padding:.5rem;margin-top:.25rem;'
        'width:100%;box-sizing:border-box}.msg{color:#d85a30;font-size:.9rem}</style>'
        "</head><body>"
        f"<h1>{html.escape(robot)} operator login</h1>"
        + (f'<p class="msg">{html.escape(message)}</p>' if message else "")
        + f'<form method="post" action="/{quote(robot, safe="")}/ui/login">'
        '<label>Token <input name="token" type="password" autocomplete="off" required></label>'
        '<button type="submit">Log in</button></form></body></html>'
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _not_built_page(robot: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>UI not built</title></head>'
        f"<body><p>UI not built for {html.escape(robot)}: run scripts/build_lelab_ui.sh.</p></body></html>"
    )


# -- §0.17 relay ----------------------------------------------------------------

_MAX_BODY_BYTES = 1 * 1024 * 1024
_ALLOWED_REQ_HEADERS = {"content-type", "accept", "range", "if-none-match", "if-modified-since"}
_ALLOWED_RES_HEADERS = {
    "content-type", "content-length", "content-range", "accept-ranges",
    "cache-control", "etag", "last-modified", "content-disposition",
}

# One shared client per event loop, created lazily so the serve environment stays
# httpx-free until a plugin actually declares an http_api() (httpx lives in the
# `stripe`/`lelab` extras). Tracking the loop matters: starlette's TestClient opens a
# fresh loop per test, and an AsyncClient is bound to the loop it was created on.
_client: tuple[Any, Any] | None = None  # (loop, AsyncClient)


def _get_client():
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop:
        import httpx

        _client = (loop, httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=30, pool=5)))
    return _client[1]


class _RelayRefusal(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _admit(
    conn: HTTPConnection,
    robot: str,
    path: str,
    method: str,
    plugins: dict[str, RobotPlugin],
    registry,
    *,
    is_ws: bool,
) -> tuple[str, ApiRoute]:
    """Run §0.17's admission steps 1–5. Returns ``(client_id, route)`` or raises
    ``_RelayRefusal``."""
    http_api = plugins[robot].http_api() if robot in plugins else None
    if http_api is None:
        raise _RelayRefusal(404, "no api for this robot")

    client_id = _verify_cookie(robot, conn.cookies.get(UI_COOKIE, ""), int(time.time()))
    if client_id is None:
        raise _RelayRefusal(401, "operator login required")

    if method not in ("GET", "HEAD"):
        origin = conn.headers.get("origin")
        if origin and origin != _public_origin(conn):
            raise _RelayRefusal(403, "cross-origin request refused")

    lookup = "WS" if is_ws else ("GET" if method == "HEAD" else method)
    route = next(
        (r for r in http_api.routes if r.method == lookup and re.fullmatch(r.pattern, path)),
        None,
    )
    if route is None:
        raise _RelayRefusal(403, "not available through the gateway")

    # Reservation — the holder is the session's client_id.
    if route.kind == "control":
        if not registry.reserve(robot, client_id):
            holder = registry.status(robot).get("holder", "")
            raise _RelayRefusal(409, f"robot is held by {holder}")
    elif route.kind == "read":
        if registry.status(robot).get("holder") == client_id:
            registry.reserve(robot, client_id)  # renew, never acquire
    # stop and config: no reservation.
    return client_id, route


async def _relay_http(
    request: Request, robot: str, path: str, plugins: dict[str, RobotPlugin], registry
) -> StreamingResponse | JSONResponse:
    try:
        _admit(request, robot, path, request.method, plugins, registry, is_ws=False)
    except _RelayRefusal as r:
        return JSONResponse({"detail": r.detail}, status_code=r.status)

    import httpx

    http_api = plugins[robot].http_api()
    assert http_api is not None  # _admit passed
    base_url = http_api.base_url.rstrip("/")
    url = f"{base_url}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _ALLOWED_REQ_HEADERS
    }
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)

    upstream_req = _get_client().build_request(request.method, url, headers=headers, content=body)
    try:
        upstream_res = await _get_client().send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "LeLab backend unreachable"}, status_code=502)

    async def _iter_and_close():
        try:
            async for chunk in upstream_res.aiter_raw():
                yield chunk
        finally:
            # Close the upstream on client disconnect. During cancellation the task is
            # already tearing down, so guard the close with a timeout — it must never
            # block the response teardown.
            try:
                await asyncio.wait_for(upstream_res.aclose(), timeout=2)
            except Exception:
                pass

    response_headers = {
        k: v for k, v in upstream_res.headers.items() if k.lower() in _ALLOWED_RES_HEADERS
    }
    return StreamingResponse(
        _iter_and_close(),
        status_code=upstream_res.status_code,
        headers=response_headers,
    )


async def _relay_ws(
    ws: WebSocket, robot: str, path: str, plugins: dict[str, RobotPlugin], registry
) -> None:
    try:
        _admit(ws, robot, f"ws/{path}", "WS", plugins, registry, is_ws=True)
    except _RelayRefusal as r:
        await _refuse(ws, 1008, r.detail)
        return

    http_api = plugins[robot].http_api()
    assert http_api is not None  # _admit passed
    base_url = http_api.base_url
    parts = urlparse(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    upstream_url = f"{scheme}://{parts.netloc}/ws/{path}"
    try:
        upstream = await websockets.connect(upstream_url, open_timeout=5, max_size=1_048_576, compression=None)
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
        await _refuse(ws, 1013, "LeLab backend unreachable")
        return

    await ws.accept()
    async with upstream:
        tasks = [
            asyncio.create_task(_pump_to_robot(ws, upstream)),
            asyncio.create_task(_pump_to_browser(ws, upstream)),
        ]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


def register_http_relay(app: FastAPI, plugins: dict[str, RobotPlugin], registry) -> None:
    """Register the §0.16 session + UI-serving routes.

    **Must be called before ``register_console`` and before the mounts**: the console's
    bare ``/{robot}/ui`` route 307-redirects static-UI robots here, and a mount would
    claim every path beneath its prefix first.

    ``registry`` is the shared reservation registry; the relay (§0.17) uses it.
    """
    static_ui_plugins = {name: p for name, p in plugins.items() if p.static_ui() is not None}
    if not static_ui_plugins:
        logger.info("http_relay: no plugin exposes a served UI; relay not registered")
        return

    session_hours = _session_hours()  # validated once, at startup
    session_max_age = session_hours * 3600

    for name in sorted(static_ui_plugins):
        logger.info("http_relay: /%s/ui/ → static UI", name)

    def _static_ui_for(robot: str) -> StaticUi:
        static_ui = plugins[robot].static_ui()
        assert static_ui is not None  # robot is in static_ui_plugins
        return static_ui

    def _ui_root_response(robot: str, request: Request):
        static_ui = _static_ui_for(robot)
        index = static_ui.directory / "index.html"
        if not index.exists():
            return HTMLResponse(
                _not_built_page(robot), status_code=503, headers={"Cache-Control": "no-store"}
            )
        if _verify_cookie(robot, request.cookies.get(UI_COOKIE, ""), int(time.time())) is None:
            return _login_page(robot, 200, "")
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/{robot}/ui/")
    async def ui_root(robot: str, request: Request):
        if robot not in static_ui_plugins:
            raise HTTPException(404, f"no UI for {robot!r}")
        return _ui_root_response(robot, request)

    @app.post("/{robot}/ui/login")
    async def ui_login(robot: str, request: Request):
        if robot not in static_ui_plugins:
            raise HTTPException(404, f"no UI for {robot!r}")
        # CSRF: a cross-origin login form could otherwise drive a logged-in session.
        origin = request.headers.get("origin")
        if origin and origin != _public_origin(request):
            raise HTTPException(403, "cross-origin login refused")
        form = await request.form()
        token = str(form.get("token", ""))
        client_id = _gateway_token_clients().get(token)
        if client_id is None:
            return _login_page(robot, 401, "unknown token")
        now = int(time.time())
        cookie = _make_cookie(robot, client_id, now, session_hours)
        api_query_param = _static_ui_for(robot).api_query_param
        location = f"/{robot}/ui/"
        if api_query_param:
            # Always append, on every login — the stored fallback in the bundle is the
            # exact path §0.19 guards against.
            api = _public_origin(request) + f"/{robot}/api"
            location = f"/{robot}/ui/?{api_query_param}={quote(api, safe='')}"
        response = RedirectResponse(location, status_code=303)
        response.set_cookie(
            UI_COOKIE,
            cookie,
            path=f"/{robot}/",
            httponly=True,
            samesite="strict",
            secure=_public_origin(request).startswith("https://"),
            max_age=session_max_age,
        )
        return response

    @app.post("/{robot}/ui/logout")
    async def ui_logout(robot: str):
        response = RedirectResponse(f"/{robot}/ui/", status_code=303)
        response.delete_cookie(UI_COOKIE, path=f"/{robot}/")
        return response

    @app.get("/{robot}/ui/{path:path}")
    async def ui_asset(robot: str, path: str, request: Request):
        if robot not in static_ui_plugins:
            raise HTTPException(404, f"no UI for {robot!r}")
        static_ui = _static_ui_for(robot)
        directory = static_ui.directory.resolve()
        resolved = (static_ui.directory / path).resolve()
        # Path traversal: refuse anything that resolves outside the bundle directory.
        if resolved != directory and directory not in resolved.parents:
            raise HTTPException(404, "not found")
        if resolved.is_file():
            # Assets are public code — no cookie, so the login page can style itself.
            return FileResponse(resolved)
        if "." not in path:
            # An SPA route (/recording) — treated as GET /{robot}/ui/.
            return _ui_root_response(robot, request)
        raise HTTPException(404, "not found")

    # -- §0.17 relay routes ----------------------------------------------------

    http_api_plugins = {name: p for name, p in plugins.items() if p.http_api() is not None}
    for name in sorted(http_api_plugins):
        http_api = plugins[name].http_api()
        assert http_api is not None
        logger.info("http_relay: /%s/api → %s", name, http_api.base_url)

    @app.api_route("/{robot}/api/{path:path}", methods=["GET", "HEAD", "POST"])
    async def relay_http(robot: str, path: str, request: Request):
        return await _relay_http(request, robot, path, plugins, registry)

    @app.websocket("/{robot}/api/ws/{path:path}")
    async def relay_ws(ws: WebSocket, robot: str, path: str):
        await _relay_ws(ws, robot, path, plugins, registry)

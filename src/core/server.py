import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from core.plugin import RobotPlugin

load_dotenv()

logger = logging.getLogger(__name__)


def _load_tokens() -> dict[str, dict]:
    """Build {token: {client_id, scopes}} for StaticTokenVerifier from env.

    Tier-1 per-agent identity — give each agent its own token so the gateway (and the
    reservation layer) can tell callers apart:

        MCP_TOKENS="marketplace=tok_abc,teleop-ui=tok_def"   # client_id=token pairs

    A single MCP_BEARER_TOKEN is still honored (legacy / dev), mapped to client_id
    'default'; but callers sharing one token are indistinguishable, so reservation
    enforcement needs distinct per-agent tokens.
    """
    tokens: dict[str, dict] = {}
    raw = os.getenv("MCP_TOKENS", "").strip()
    if raw:
        for pair in raw.split(","):
            client_id, sep, token = pair.strip().partition("=")
            client_id, token = client_id.strip(), token.strip()
            if sep and client_id and token:
                tokens[token] = {"client_id": client_id, "scopes": []}
    legacy = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if legacy:
        tokens.setdefault(legacy, {"client_id": "default", "scopes": []})
    return tokens


def _parse_token_file(path: str) -> dict[str, dict]:
    """Parse a token file into {token: {client_id, scopes}}.

    One ``client_id=token`` per line; ``#`` comments and blank lines ignored.
    """
    tokens: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            client_id, sep, token = line.partition("=")
            client_id, token = client_id.strip(), token.strip()
            if sep and client_id and token:
                tokens[token] = {"client_id": client_id, "scopes": []}
    return tokens


class FileTokenVerifier(StaticTokenVerifier):
    """A static token verifier that **hot-reloads** its tokens from a file.

    Re-reads the file whenever its mtime changes, so agents can be added or revoked by
    editing the file with **no gateway restart** — the change takes effect on the next
    request. If the file is missing, all tokens are rejected (secure default) until it
    appears. Reuses ``StaticTokenVerifier``'s expiry/scope logic.
    """

    def __init__(self, path: str):
        self._path = path
        self._mtime: float | None = None
        super().__init__(tokens={})
        self._refresh()

    def _refresh(self) -> None:
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            if self.tokens:
                logger.warning("Token file %s missing — all tokens now rejected.", self._path)
            self.tokens = {}
            self._mtime = None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self.tokens = _parse_token_file(self._path)
        logger.info("Loaded %d token(s) from %s", len(self.tokens), self._path)

    async def verify_token(self, token: str):
        self._refresh()  # cheap stat; re-parses only when the file changed
        return await super().verify_token(token)


def _make_auth():
    """Create a shared auth provider, or None if unauthenticated.

    Precedence: if ``MCP_TOKENS_FILE`` is set, use the hot-reloading file verifier
    (add/revoke agents by editing the file, no restart). Otherwise fall back to the
    static env tokens (``MCP_TOKENS`` / ``MCP_BEARER_TOKEN``).
    """
    token_file = os.getenv("MCP_TOKENS_FILE", "").strip()
    if token_file:
        return FileTokenVerifier(token_file)
    tokens = _load_tokens()
    if not tokens:
        return None
    return StaticTokenVerifier(tokens=tokens)


def create_robot_server(plugin: RobotPlugin, registry, robot_name: str) -> FastMCP:
    """Create an isolated FastMCP server for a single robot plugin.

    Every tool on this server passes through ReservationMiddleware, so the robot's own
    control tools and the marketplace tools all honor the same per-robot reservation.
    """
    meta = plugin.metadata()
    auth = _make_auth()

    mcp = FastMCP(
        name=meta.name,
        instructions=f"Control and monitor: {meta.name}",
        auth=auth,
    )
    plugin.register_tools(mcp)

    from core.robot_marketplace_tools import register as register_marketplace_tools
    register_marketplace_tools(mcp, plugin)

    from core.reservation import ReservationMiddleware, register_reservation_tools
    register_reservation_tools(mcp, registry, robot_name)
    mcp.add_middleware(ReservationMiddleware(registry, robot_name))

    return mcp


def create_fleet_server(
    plugins: dict[str, RobotPlugin],
    mounted_robots: dict[str, str] | None = None,
) -> FastMCP:
    """Create the fleet orchestrator MCP server (local discovery only).

    Answers "what robots are plugged into this gateway?" via list_connected.
    Task auctions / marketplace bidding live in the separate yakrobot-marketplace
    service, which reaches robots over MCP — this gateway is a pure robot controller.

    Args:
        plugins: Map of plugin name → instantiated plugin, for local discovery.
        mounted_robots: Map of plugin name → endpoint path, so discovery results
                        include each robot's local URL.
    """
    auth = _make_auth()

    mcp = FastMCP(
        name="Robot Fleet Orchestrator",
        instructions="List robots connected to this gateway.",
        auth=auth,
    )

    from core.local_discovery import register_local_discovery_tools

    register_local_discovery_tools(mcp, plugins, mounted_robots=mounted_robots)

    return mcp


def create_gateway(plugins: dict[str, RobotPlugin]) -> FastAPI:
    """Create a FastAPI gateway that sub-mounts each robot's MCP server.

    Each robot gets its own isolated FastMCP instance mounted at /{name}/.
    The fleet orchestrator is mounted at /fleet/.
    All served on a single port behind one ngrok tunnel.

    FastMCP v3 requires each MCP app's lifespan to be started for its
    StreamableHTTPSessionManager task group. We compose all lifespans
    into the gateway's lifespan.
    """
    from core.reservation import ReservationRegistry

    registry = ReservationRegistry()  # shared across every robot server + the index

    mcp_apps = {}
    mounted_robots: dict[str, str] = {}
    for name, plugin in plugins.items():
        mcp = create_robot_server(plugin, registry, name)
        mcp_apps[name] = mcp.http_app()
        mounted_robots[name] = f"/{name}/mcp"

    # Fleet orchestrator (local discovery only — auctions live in yakrobot-marketplace)
    fleet_mcp = create_fleet_server(plugins, mounted_robots=mounted_robots)
    mcp_apps["fleet"] = fleet_mcp.http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Start all MCP app lifespans (initializes their task groups)
        async with _compose_lifespans(mcp_apps.values()):
            yield

    app = FastAPI(title="Robot Fleet Gateway", lifespan=lifespan)

    # Everything the gateway serves under a robot's own prefix: the realtime socket
    # proxy to its control server, the driving console, and its descriptor JSON. All
    # three registered BEFORE the mounts below — Starlette matches in registration
    # order, and app.mount("/{name}") claims every path beneath it, so mounting first
    # makes these dead code.
    from core.console import register_console
    from core.descriptor_route import CORS_HEADERS, register_descriptor_route
    from core.ws_proxy import register_ws_proxy

    register_ws_proxy(app, plugins)
    register_console(app, plugins)
    register_descriptor_route(app, plugins)

    for name, mcp_app in mcp_apps.items():
        app.mount(f"/{name}", mcp_app)

    # The index carries the same CORS headers as /{robot}/descriptor, and for the same
    # caller: the registration page is handed a bare tunnel root and reads this route to
    # discover which robots are here before fetching any descriptor. Without the headers
    # (and the preflight, which ngrok-skip-browser-warning forces) descriptor_endpoint
    # below would be unreachable from the only client that wants it.
    @app.options("/")
    async def index_preflight():
        return Response(status_code=204, headers=CORS_HEADERS)

    @app.get("/")
    async def index():
        return JSONResponse(
            {
                "service": "Robot Fleet Gateway",
                "robots": {
                    name: {
                        "mcp_endpoint": f"/{name}/mcp",
                        "descriptor_endpoint": f"/{name}/descriptor",
                        # Present only for robots with a realtime control server —
                        # its absence is how a caller knows this one cannot be driven
                        # from a browser.
                        **({"ui_endpoint": f"/{name}/ui"} if plugin.control_base_urls() else {}),
                        "tools": plugin.tool_names(),
                        "reservation": registry.status(name),
                    }
                    for name, plugin in plugins.items()
                },
                "fleet_endpoint": "/fleet/mcp",
            },
            headers=CORS_HEADERS,
        )

    return app


@asynccontextmanager
async def _compose_lifespans(apps):
    """Recursively enter the lifespan of each ASGI app."""
    apps = list(apps)
    if not apps:
        yield
        return

    first, *rest = apps
    async with first.lifespan(first):
        async with _compose_lifespans(rest):
            yield

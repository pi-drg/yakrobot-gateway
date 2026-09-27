from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP


@dataclass
class BiddingTerms:
    """Pricing and task-acceptance rules for marketplace participation.

    Set on a plugin to opt it into the bidding marketplace. Leave ``bidding_terms``
    as ``None`` on ``RobotMetadata`` to opt out entirely.
    """

    min_price_cents: int = 50               # Floor price; 50 = $0.50
    rate_per_minute_cents: int | None = 10  # Per-minute rate; None = flat price only
    currency: str = "usd"
    accepted_task_types: list[str] = field(default_factory=list)
    max_duration_secs: int = 300
    max_concurrent_tasks: int = 1
    requires_approval: bool = True          # True = human must call fleet_execute_task


@dataclass
class RobotMetadata:
    """Robot classification used to build its agent descriptor (see yakrobot-descriptor)."""

    name: str
    description: str
    robot_type: str
    url_prefix: str = ""        # URL path segment (e.g. "tumbller" → /tumbller/mcp)
    fleet_provider: str = ""
    fleet_domain: str = ""
    image: str = ""
    bidding_terms: BiddingTerms | None = None  # None = not participating in marketplace


@dataclass(frozen=True)
class DatasetFeatures:
    """How a robot's teleop frames map onto LeRobot features.

    Returned by ``RobotPlugin.dataset_features()`` for robots whose teleop sessions can
    be recorded and exported as a dataset. Core owns the tap, the queue, the writer and
    the file layout; the plugin owns what each field means (design §2.2).

    The three callables all take the *parsed JSON object* of one frame (the ``msg`` in a
    ``control.jsonl`` line) and describe it from the robot's point of view:

    - ``action_update``: an upward (browser→robot) control frame → a partial action dict
      to merge, or ``None`` to ignore the frame. Returns only the fields this message
      sets — a ``drive`` carries velocities, a ``look`` carries pan/tilt, and a ``ping``
      or ``stop`` sets nothing.
    - ``state_update``: a downward (robot→browser) frame → a partial observation.state
      dict, or ``None``. The state is what the robot *reported* (a clamped servo echo,
      a telemetry reading), never what the browser asked for.
    - ``is_stop``: an upward frame → True when it is an explicit stop.
    """

    spec_version: int                 # bump when names/meaning change
    action_names: tuple[str, ...]
    state_names: tuple[str, ...]
    camera_key: str                   # LeRobot feature key for the one camera
    default_fps: int
    deadman_ms_default: int           # used when hello has no deadman_ms
    velocity_names: tuple[str, ...]   # subset of action_names zeroed by deadman/stop
    action_update: Callable[[dict], dict | None]
    state_update: Callable[[dict], dict | None]
    is_stop: Callable[[dict], bool]


@dataclass(frozen=True)
class StaticUi:
    """A served, pre-built SPA for a robot (execution plan §0.15).

    Returned by ``RobotPlugin.static_ui()`` for robots whose operator UI is a built
    frontend bundle served from disk rather than the gateway's one-file console. The
    two are mutually exclusive with ``control_base_urls()``.
    """

    directory: Path                 # must contain index.html to be served
    api_query_param: str | None      # e.g. "api": login redirect appends ?api=<origin>/<robot>/api


@dataclass(frozen=True)
class ApiRoute:
    """One allowlisted upstream call the gateway relays to a robot's own server (§0.15)."""

    method: str                     # "GET" | "POST" | "WS"
    pattern: str                    # re.fullmatch against the path after "/api/", no leading slash
    kind: str                       # "read" | "config" | "control" | "stop"


@dataclass(frozen=True)
class HttpApi:
    """An allowlisted HTTP/WS API the gateway relays to the robot's own server (§0.15)."""

    base_url: str
    routes: tuple[ApiRoute, ...]


class RobotPlugin(ABC):
    """Base class for all robot plugins.

    A plugin is responsible for:
    1. Declaring its metadata (name, type, fleet info)
    2. Registering its MCP tools on a FastMCP server instance
    3. Providing its tool names for its agent descriptor
    """

    @abstractmethod
    def metadata(self) -> RobotMetadata:
        """Return the robot's descriptor metadata."""
        ...

    @abstractmethod
    def register_tools(self, mcp: FastMCP) -> None:
        """Register this robot's MCP tools on the shared server."""
        ...

    @abstractmethod
    def tool_names(self) -> list[str]:
        """Return the list of MCP tool names this plugin registers.

        Must match the function names passed to @mcp.tool exactly.
        """
        ...

    def control_base_urls(self) -> list[str]:
        """Candidate base HTTP URLs for the robot's own control server.

        Implemented by plugins that front an on-robot HTTP server — the PiCar
        runs ``picar_freenove_fastapi`` on the Raspberry Pi itself. The gateway
        uses these to reverse-proxy realtime WebSocket traffic
        (``/{robot}/ws/*``) through to the robot, so a browser that can reach
        only the gateway can still hold a live control socket against hardware.

        A **list, tried in order**, because neither way of naming a robot on a
        LAN is reliable alone: an mDNS name survives the DHCP lease changing
        but needs a working resolver, while a bare IP needs neither but breaks
        the moment the lease moves. Listing both means whichever is true today
        wins, and no one has to notice which.

        Empty list means "no realtime socket": plugins that reach their
        hardware another way (djitellopy speaks UDP to the Tello) get no
        ``/ws/*`` route, and a request for one is refused rather than misrouted.
        """
        return []

    def control_auth_token(self) -> str | None:
        """This robot's own bearer token, if it runs with auth enabled.

        The gateway injects it into the upstream WebSocket handshake so the
        browser never holds a robot credential. Per-plugin rather than read
        from one env var in the proxy, because sending one robot's token to
        another robot is both a leak and a puzzling 401.

        None (the default) means the robot has no auth — normal on a trusted
        LAN, and the current state of the PiCar.
        """
        return None

    def dataset_features(self) -> DatasetFeatures | None:
        """Map this robot's control/telemetry frames to LeRobot features.

        Implemented by plugins whose teleop sessions can be recorded and exported as a
        dataset — the PiCar (and its simulator) declare velocity/pan/tilt actions and a
        front camera. ``None`` (the default) means the robot is not recordable: the
        gateway records nothing for it, and the index reports ``recording: false``. This
        is the same opt-in pattern as ``control_base_urls()``.
        """
        return None

    def static_ui(self) -> StaticUi | None:
        """A served, pre-built operator UI (execution plan §0.15).

        Implemented by plugins whose operator frontend is a built SPA on disk (LeLab),
        served at ``/{robot}/ui/…`` behind an operator session. ``None`` (the default)
        means the robot uses the gateway's one-file console instead. Mutually exclusive
        with ``control_base_urls()``: a robot has either realtime sockets to proxy or a
        served UI, never both.
        """
        return None

    def http_api(self) -> HttpApi | None:
        """An allowlisted HTTP/WS API relayed to the robot's own server (§0.15).

        Implemented by plugins that front an on-robot server whose calls the gateway
        relays behind the operator session — ``/{robot}/api/{path}`` (HTTP) and
        ``/{robot}/api/ws/{path}`` (WebSocket). ``None`` (the default) means no relay.
        """
        return None

    async def bid(self, task_spec: dict) -> dict | None:
        """Generate a bid for a task. Override in marketplace-participating plugins.

        Reached via the robot's ``robot_submit_bid`` tool (called by yakrobot-marketplace
        over MCP). Returns bid parameters dict or None to decline.
        Default: returns None (backward-compatible opt-out).
        """
        return None

    async def execute(self, task_id: str, task_description: str, parameters: dict) -> dict:
        """Execute an accepted task. Override in marketplace-participating plugins.

        Reached via the robot's ``robot_execute_task`` tool. Returns a delivery_data dict
        with at minimum {"success": bool}.
        Default: returns a not-implemented failure so the caller marks the task 'failed'
        rather than crashing.
        """
        return {"success": False, "error": "execute() not implemented for this plugin."}

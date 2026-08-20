from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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

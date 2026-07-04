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

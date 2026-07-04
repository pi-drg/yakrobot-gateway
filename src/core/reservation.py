"""Generic per-robot reservation — who currently controls a robot, enforced across ALL
of that robot's MCP tools.

A physical robot must not be driven by two agents at once. This lives in ``core`` (not in
any single tool) so every control path honors the same rule: the plugin's own tools
(``tumbller_move`` …) and the marketplace tools (``robot_execute_task``) all pass through
one ``ReservationMiddleware``.

**Identity** comes from the MCP access token's ``client_id`` (Tier-1 per-agent static
tokens — see ``core.server._make_auth``). A robot reserved by client *X* rejects control
calls from any *other* client until *X* releases it or the lease TTL expires. State is
visible via ``robot_status`` and the gateway index, so any agent can check before acting.

Enforcement is **explicit**: the middleware blocks a call only when the robot is currently
reserved by a *different* client. It does not auto-reserve on every call (a stray sensor
read shouldn't seize control). Agents that want exclusive control call ``robot_reserve``
first; the marketplace does this around task execution.

Caveat: with no auth (or a single shared token) every caller resolves to the same identity,
so the reservation cannot distinguish agents — enforcement is only meaningful with Tier-1
per-agent tokens.
"""

import time
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

SESSION_TTL = 300.0  # seconds a reservation holds before auto-expiry (guards crashed holders)

# Reservation-management + read-only discovery tools are never themselves gated.
_UNGATED = {"robot_reserve", "robot_release", "robot_status", "robot_get_pricing"}


@dataclass
class Reservation:
    holder: str
    since: float
    expires_at: float


class ReservationRegistry:
    """In-process, per-robot reservation state shared across the gateway."""

    def __init__(self):
        self._by_robot: dict[str, Reservation] = {}

    def _active(self, robot: str) -> Reservation | None:
        r = self._by_robot.get(robot)
        if r is None:
            return None
        if time.monotonic() >= r.expires_at:
            del self._by_robot[robot]  # lapsed — treat as free
            return None
        return r

    def reserve(self, robot: str, holder: str, ttl: float = SESSION_TTL) -> bool:
        """Acquire/renew for ``holder``. True if free/expired or already held by holder;
        False if currently held by a different client."""
        r = self._active(robot)
        if r is not None and r.holder != holder:
            return False
        now = time.monotonic()
        self._by_robot[robot] = Reservation(
            holder=holder,
            since=r.since if r else now,
            expires_at=now + ttl,
        )
        return True

    def release(self, robot: str, holder: str) -> bool:
        """Release only if ``holder`` currently holds it. Returns whether it was released."""
        r = self._active(robot)
        if r is not None and r.holder == holder:
            del self._by_robot[robot]
            return True
        return False

    def blocks(self, robot: str, caller: str) -> Reservation | None:
        """Return the active reservation if it would block ``caller`` (held by someone
        else), else None."""
        r = self._active(robot)
        if r is not None and r.holder != caller:
            return r
        return None

    def status(self, robot: str) -> dict:
        r = self._active(robot)
        if r is None:
            return {"reserved": False}
        now = time.monotonic()
        return {
            "reserved": True,
            "holder": r.holder,
            "held_seconds": round(now - r.since, 1),
            "expires_in": round(r.expires_at - now, 1),
        }


def caller_id() -> str:
    """Identity of the current MCP caller from its access token, or 'anonymous'."""
    token = get_access_token()
    return token.client_id if token and token.client_id else "anonymous"


class ReservationMiddleware(Middleware):
    """Enforce one robot's reservation across every tool call on its MCP server."""

    def __init__(self, registry: ReservationRegistry, robot_name: str):
        self.registry = registry
        self.robot_name = robot_name

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if context.message.name not in _UNGATED:
            blocking = self.registry.blocks(self.robot_name, caller_id())
            if blocking is not None:
                raise ToolError(
                    f"Robot {self.robot_name!r} is reserved by {blocking.holder!r} — "
                    f"another agent is in control (expires in ~{round(blocking.expires_at - time.monotonic())}s). "
                    f"Call robot_status to check, or wait for them to robot_release it."
                )
        return await call_next(context)


def register_reservation_tools(mcp, registry: ReservationRegistry, robot_name: str) -> None:
    """Register robot_reserve / robot_release / robot_status on a robot's MCP server."""

    @mcp.tool
    async def robot_reserve() -> dict:
        """Reserve this robot for exclusive control (a teleop session or task).

        While reserved, control calls from other agents are rejected until you
        robot_release it or the reservation expires. Calling again renews the lease.
        Returns whether you now hold it, plus the current reservation status.
        """
        caller = caller_id()
        acquired = registry.reserve(robot_name, caller)
        return {"acquired": acquired, "you": caller, **registry.status(robot_name)}

    @mcp.tool
    async def robot_release() -> dict:
        """Release your reservation on this robot so other agents can control it."""
        caller = caller_id()
        released = registry.release(robot_name, caller)
        return {"released": released, **registry.status(robot_name)}

    @mcp.tool
    async def robot_status() -> dict:
        """Show whether this robot is currently reserved and by whom."""
        return registry.status(robot_name)

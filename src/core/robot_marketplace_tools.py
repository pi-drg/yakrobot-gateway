from fastmcp import FastMCP

from core.plugin import RobotPlugin

# Tool names registered by this module — used by plugins to declare them in tool_names().
MARKETPLACE_TOOL_NAMES = [
    "robot_submit_bid",
    "robot_execute_task",
    "robot_get_pricing",
]


def register(mcp: FastMCP, plugin: RobotPlugin) -> None:
    """Register the three marketplace tools on a robot's MCP server.

    Called automatically by create_robot_server() for every plugin — plugins
    do not need to call this themselves. The external marketplace calls these
    tools directly at /{robot}/mcp; they delegate to plugin.bid() and
    plugin.execute().

    Concurrency / exclusivity is handled generically by ReservationMiddleware
    (see core/reservation.py) across ALL of a robot's tools — including the
    plugin's own control tools — so it is not re-implemented here.
    """

    @mcp.tool
    async def robot_submit_bid(
        task_description: str,
        task_category: str,
        budget_ceiling: float,
        sla_seconds: int,
        capability_requirements: dict,
    ) -> dict:
        """Evaluate a task and return a bid, or decline with a reason.

        task_category uses marketplace categories: "env_sensing" or "visual_inspection".
        budget_ceiling is the maximum the buyer will pay in USD.
        sla_seconds is the time the buyer expects the task to complete within.
        capability_requirements is a freeform dict (e.g. {"sensors_required": ["temperature"]}).
        """
        task_spec = {
            "task_description": task_description,
            "task_category": task_category,
            "budget_ceiling": budget_ceiling,
            "sla_seconds": sla_seconds,
            "capability_requirements": capability_requirements,
        }
        result = await plugin.bid(task_spec)
        if result is None:
            return {
                "willing_to_bid": False,
                "reason": "Task not supported or robot unavailable.",
            }
        try:
            price = float(result.get("price", 0))
        except (TypeError, ValueError):
            return {
                "willing_to_bid": False,
                "reason": "Plugin returned an invalid price value.",
            }
        if price > budget_ceiling:
            return {
                "willing_to_bid": False,
                "reason": f"Bid price {price} exceeds budget ceiling {budget_ceiling}.",
            }
        # Normalise to schema — fakerobot uses "ai_confidence", target field is "confidence"
        return {
            "willing_to_bid": True,
            "price": price,
            "currency": result.get("currency", "usd"),
            "sla_commitment_seconds": int(result.get("sla_commitment_seconds", 0)),
            "confidence": float(result.get("confidence", result.get("ai_confidence", 0.5))),
            "capabilities_offered": result.get("capabilities_offered", []),
            "notes": result.get("notes", ""),
        }

    @mcp.tool
    async def robot_execute_task(
        task_id: str,
        task_description: str,
        parameters: dict,
        payment_source: str = "fleet",
    ) -> dict:
        """Execute an accepted task and return delivery_data for IPFS upload.

        payment_source="marketplace": external marketplace has already handled
        payment (Stripe or USDC) — just execute and return results.
        payment_source="fleet": called from the internal fleet server — payment
        is handled by the fleet engine, not by this tool.

        Either way, this tool never initiates payment.
        """
        return await plugin.execute(task_id, task_description, parameters)

    @mcp.tool
    async def robot_get_pricing() -> dict:
        """Return this robot's pricing, availability, and accepted task categories.

        Also exposes ``requires_approval`` so an out-of-process fleet marketplace can
        decide, after payment, whether to auto-execute (Mode B) or wait for the operator
        (Mode A) — the same decision the in-process engine reads from BiddingTerms.
        """
        terms = getattr(plugin.metadata(), "bidding_terms", None)
        if terms is not None:
            rate = (
                terms.rate_per_minute_cents / 100
                if terms.rate_per_minute_cents is not None
                else None  # None = flat price only, no per-minute rate
            )
            currencies = [terms.currency] + ([] if terms.currency == "usdc" else ["usdc"])
        else:
            rate = 0.10
            currencies = ["usd", "usdc"]
        return {
            "min_task_price_usd": (terms.min_price_cents / 100) if terms else 0.50,
            "rate_per_minute_usd": rate,
            "accepted_currencies": currencies,
            "max_concurrent_tasks": terms.max_concurrent_tasks if terms else 1,
            "task_categories": terms.accepted_task_types if terms else [],
            "requires_approval": terms.requires_approval if terms else True,
            "availability": "online",
        }

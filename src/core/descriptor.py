"""Build a robot plugin's ``RobotDescriptor`` — the gateway's producer side of the
shared ``yakrobot-descriptor`` contract.

This module imports ``yakrobot_descriptor`` (the ``export`` extra), so it stays optional
for a gateway that only drives hardware: the CLI's ``export`` command imports it eagerly,
and ``core.descriptor_route`` imports it **lazily inside the request handler** and answers
501 when the extra is absent. Nothing at import time in the serving path reaches it.

The JSON this produces is the whole of the gateway's contribution to on-chain identity —
registration is a transaction signed by a browser wallet, and this repo holds no chain
code.
"""

from yakrobot_descriptor import RobotDescriptor, BiddingTerms

from core.plugin import RobotPlugin


def build_descriptor(plugin: RobotPlugin, public_domain: str = "") -> RobotDescriptor:
    """Map a plugin's ``RobotMetadata`` to a validated ``RobotDescriptor``.

    ``public_domain`` resolves the public MCP/fleet endpoints (an ngrok, Cloudflare, or
    any other host); pass ``""`` to emit only the metadata fields (no URLs). Pydantic
    validates the result on construction.
    """
    meta = plugin.metadata()
    base = f"https://{public_domain}" if public_domain else ""
    terms = meta.bidding_terms
    return RobotDescriptor(
        name=meta.name,
        description=meta.description,
        robot_type=meta.robot_type,
        fleet_provider=meta.fleet_provider,
        fleet_domain=meta.fleet_domain,
        image=meta.image,
        mcp_endpoint=f"{base}/{meta.url_prefix}/mcp" if base else "",
        fleet_endpoint=f"{base}/fleet/mcp" if base else "",
        tool_names=plugin.tool_names(),
        bidding_terms=None if terms is None else BiddingTerms(
            min_price_cents=terms.min_price_cents,
            currency=terms.currency,
            accepted_task_types=list(terms.accepted_task_types),
        ),
    )

# yakrobot-gateway

Robot **controller**. Serves per-robot MCP control endpoints, local fleet discovery,
and a generic per-robot reservation (who's in control), behind a single port and one
ngrok tunnel. **Contains no blockchain code** — on-chain identity, registration,
discovery, and attestation live in the separate
[`yakrobot-identity`](../yakrobot-identity) layer. **Task auctions / marketplace** live
in the separate [`yakrobot-marketplace`](../yakrobot-marketplace) service, which reaches
robots over MCP; this gateway is a pure controller.

> Extracted from
> [`YakRoboticsGarage/yakrover-8004-mcp`](https://github.com/YakRoboticsGarage/yakrover-8004-mcp)
> @`791833e`. That repo remains the canonical record of the pre-split history.

> Named `yakrobot-gateway` (not `-mcp-gateway`): MCP is today's transport, but the
> gateway is meant to host future robots driven by CLI or skills too — the name stays
> transport-agnostic.

## Architecture

- **FastAPI gateway** with ASGI sub-mounts — each robot gets its own isolated FastMCP
  server instance. Single port, single ngrok tunnel.
- Endpoints: `/fleet/mcp` (local discovery), `/{robot}/mcp` (per-robot control).
- `/fleet/mcp` exposes `list_connected` (local plugin-registry introspection —
  **no chain**).
- Every `/{robot}/mcp` tool passes through a reservation guard (`robot_reserve` /
  `robot_release` / `robot_status`) so two agents can't drive one robot at once.
- Plugin auto-discovery scans `src/plugins/` for `RobotPlugin` subclasses.

## Plugin system

Each robot is a package under `src/plugins/{name}/`:

- `__init__.py` — `RobotPlugin` subclass: `metadata()`, `tool_names()`, `register_tools(mcp)`
- `robot_adapter.py` — robot-facing adapter: robot-specific comms (HTTP, UDP, serial, SDK, …)
- `mcp_tools.py` — MCP-facing adapter: `register(mcp, robot)` defining `@mcp.tool` handlers

Tool naming: `{robot_prefix}_{action}` (e.g. `tumbller_move`).

## Common commands

Manage the gateway with the `yakrobot-py` CLI (Typer-based; `uv run yakrobot-py --help`,
or `uv tool install --editable .` for a global `yakrobot-py`):

```bash
uv sync --extra fakerobot                          # serve-only deps + fake robot (no chain)
uv run yakrobot-py robots                           # list available robot plugins
uv run yakrobot-py serve --robots fakerobot         # serve one robot
uv run yakrobot-py serve --robots tumbller --tunnel ngrok   # with a public tunnel
uv run yakrobot-py status                           # inspect a running gateway (mounts + reservations)
uv run yakrobot-py sim                              # start the hardware-free fakerobot simulator (:8080)
```

The legacy `uv run python scripts/serve.py …` entrypoint still works — it forwards to the
same implementation.

### Exporting a robot descriptor (for on-chain registration elsewhere)

This gateway holds **no chain code**. To put a robot on-chain you *export its descriptor
as JSON here*, then register it from `yakrobot-identity`:

```bash
uv sync --extra export
uv run yakrobot-py export tumbller   # --public-domain defaults from $NGROK_DOMAIN / $CLOUDFLARE_DOMAIN
# writes robot-descriptors/tumbller.json (a gitignored, regenerable artifact); then:
cd ../yakrobot-identity
uv run python scripts/register.py --descriptor ../yakrobot-gateway/robot-descriptors/tumbller.json --chain base-sepolia
```

The descriptor shape is the shared [`yakrobot-descriptor`](../yakrobot-descriptor)
contract (`descriptor.schema.json`) — the JSON is the only thing that crosses between
the repos; neither imports the other's types. To discover or attest robots on-chain, use
the identity repo's own CLI (`scripts/discover.py`, `scripts/attest.py`).

## End-to-end flow (two repos)

```
1. serve     (this repo)  bring robots online behind one ngrok URL
2. export    (this repo)  write each robot's descriptor JSON (yakrobot-descriptor contract)
3. register  (yakrobot-identity)  put each robot on-chain from that JSON
4. attest    (yakrobot-identity)  vouch for a robot via EAS  (optional)
5. discover  (yakrobot-identity)  find robots on-chain, write .mcp.json for a client
6. connect   the client talks to this gateway's /{robot}/mcp endpoints
```

Registration, attestation, and discovery all run from
[`yakrobot-identity`](../yakrobot-identity). This gateway owns steps 1–2 only — it serves
robots and exports their descriptors, and holds no chain code.

## Environment

Serving with a tunnel: `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`.
Auth: `MCP_TOKENS`/`MCP_TOKENS_FILE` (per-agent tokens — needed for reservations to tell
callers apart) or `MCP_BEARER_TOKEN` (single shared token).
Optional: `TUMBLLER_URL`, `TELLO_HOST`, `FAKEROBOT_URL`.
On-chain registration secrets (`SIGNER_PVT_KEY`, `PINATA_JWT`, …) live in
`yakrobot-identity`; payment/marketplace secrets live in `yakrobot-marketplace` — not here.

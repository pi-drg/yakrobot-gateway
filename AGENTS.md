# AGENTS.md — yakrobot-gateway

## First-Time Setup — Connecting to the Robot Fleet

On-chain discovery and the `.mcp.json` setup flow live in the **`yakrobot-identity`**
repo, not here. When a user wants to discover robots on-chain and wire them up, run
that repo's flow:

```bash
# in ../yakrobot-identity
uv run python scripts/discover.py --provider yakrobot
uv run python scripts/discover.py --add-mcp --provider yakrobot --token <BEARER>
```

This gateway only **serves** robots and answers "what's plugged in here?" locally (no
chain) via the `/fleet/mcp` `list_connected` tool.

---

## Project Overview

Robot **controller**: per-robot MCP control + local fleet discovery + a generic
per-robot reservation. Plugin-based so any robot is added with minimal glue. **No
blockchain code** — on-chain identity/registration/discovery/attestation is delegated to
the sibling `yakrobot-identity` package. **Task auctions / marketplace** were extracted to
the sibling `yakrobot-marketplace` service, which reaches robots over MCP.

## Repository Structure

```
yakrobot-gateway/
├── src/
│   ├── core/              # Shared infrastructure (never changes per robot)
│   │   ├── server.py      # FastAPI gateway + ASGI sub-mounts + auth (per-agent tokens)
│   │   ├── tunnel.py      # ngrok tunnel
│   │   ├── local_discovery.py # list_connected tool (local, no chain)
│   │   ├── robot_marketplace_tools.py # per-robot robot_submit_bid/execute/pricing (called by yakrobot-marketplace)
│   │   ├── reservation.py # per-robot reservation registry + middleware + reserve/release/status
│   │   ├── plugin.py      # RobotPlugin base + RobotMetadata
│   │   └── descriptor.py  # build_descriptor: plugin metadata → RobotDescriptor (export extra)
│   └── plugins/           # One sub-package per robot (device-neutral; IoT/printers later)
│       ├── tumbller/  tello/  fakerobot/  picar_freenove/  _template/
└── scripts/               # serve.py + export_descriptor.py (emits the JSON contract)
```

## Architecture

- **FastAPI gateway** with ASGI sub-mounts — each robot gets its own isolated FastMCP
  instance. Single port, single ngrok tunnel.
- Endpoints: `/fleet/mcp` (local discovery only), `/{robot}/mcp` (control).
- Every `/{robot}/mcp` tool passes through `ReservationMiddleware` — a robot reserved by
  one agent rejects control calls from others (identity = per-agent token `client_id`).
- Plugin auto-discovery scans `src/plugins/` for `RobotPlugin` subclasses.

## Plugin System

Each robot plugin is a package under `src/plugins/{name}/` with three files:

- `__init__.py` — `RobotPlugin` subclass with `metadata()`, `tool_names()`, `register_tools(mcp)`
- `robot_adapter.py` — **robot-facing** adapter: robot-specific communication (HTTP, UDP,
  serial, SDK wrapper, etc.) exposed as a clean capability API
- `mcp_tools.py` — **MCP-facing** adapter: `register(mcp, robot)` defining `@mcp.tool`
  handlers (names, signatures, validation) that delegate to the robot adapter

The two files are adapters pointing in opposite directions (ports-and-adapters):
`mcp_tools.py` adapts the MCP protocol inward, `robot_adapter.py` adapts the robot's
native interface outward. Sometimes the adapter *is* the transport (Tumbller owns
`httpx`); sometimes it wraps an existing client/SDK (Tello wraps `djitellopy`).

Tool naming convention: `{robot_prefix}_{action}` (e.g. `tumbller_move`, `tello_takeoff`).

## Key Technologies

- **Python 3.13+**, managed with `uv`
- **FastMCP** — MCP server framework
- **FastAPI + uvicorn** — ASGI gateway
- **pyngrok** — tunnel management
- **yakrobot-descriptor** (`export` extra) — shared JSON `RobotDescriptor` contract
  consumed by `yakrobot-identity` for on-chain registration (which lives entirely there)

## Common Commands

The gateway is managed with the **`yakrobot-py`** CLI (Typer-based; defined in
`src/yakrobot_cli/`, registered as a `[project.scripts]` console script). Run it via
`uv run yakrobot-py …`, or `uv tool install --editable .` for a bare `yakrobot-py`. The
`scripts/serve.py` / `scripts/export_descriptor.py` entrypoints still work — they forward
to the same implementation in `src/yakrobot_cli/commands.py` (one source of truth).

```bash
# Install dependencies (serve-only; no chain deps)
uv sync                        # Core only (includes the yakrobot-py CLI)
uv sync --extra tumbller       # With Tumbller support
uv sync --extra picar-freenove # With Freenove 4WD PiCar support
uv sync --extra fakerobot      # With fake robot (no hardware needed)
uv sync --extra all            # All robots

# Discover + serve robots
uv run yakrobot-py robots                                  # List available robot plugins
uv run yakrobot-py serve                                   # All robots, no tunnel
uv run yakrobot-py serve --tunnel ngrok                    # All robots via ngrok
uv run yakrobot-py serve --tunnel cloudflare               # ...or via Cloudflare Tunnel
uv run yakrobot-py serve --robots tumbller --tunnel ngrok  # Single robot
uv run yakrobot-py status                                  # Inspect a running gateway (mounts + reservations)

# Fake robot (hardware-free development)
uv run yakrobot-py sim                                     # Start simulator on :8080
uv run yakrobot-py serve --robots fakerobot                # Gateway for fake robot

# Export a robot's JSON descriptor (needs the `export` extra), then register from
# yakrobot-identity — no chain code runs here.
uv sync --extra export
uv run yakrobot-py export tumbller     # --public-domain defaults from $NGROK_DOMAIN / $CLOUDFLARE_DOMAIN
# writes robot-descriptors/tumbller.json (gitignored artifact; source of truth = metadata())
# then, in ../yakrobot-identity:
#   uv run python scripts/register.py --descriptor ../yakrobot-gateway/robot-descriptors/tumbller.json

# On-chain registration / discovery / attestation / wallet → use yakrobot-identity's CLI.
```

## Environment Variables

Serving:
- `TUNNEL_PROVIDER` — (optional) public tunnel provider: `ngrok` (default) or `cloudflare`;
  `yakrobot-py serve --tunnel {ngrok,cloudflare}` overrides it.
- `NGROK_AUTHTOKEN` — ngrok auth token (required for the ngrok tunnel)
- `NGROK_DOMAIN` — ngrok static domain (also the default for `yakrobot-py export`'s
  `--public-domain`, which resolves the descriptor's public endpoints; `CLOUDFLARE_DOMAIN`
  is the fallback)
- Cloudflare Tunnel (`--tunnel cloudflare`) needs the `cloudflared` binary on PATH. Set
  `CLOUDFLARE_TUNNEL_TOKEN` + `CLOUDFLARE_DOMAIN` for a stable named tunnel (its dashboard
  ingress must point at `http://localhost:<port>`); omit both for an ephemeral
  `*.trycloudflare.com` quick tunnel.
- `MCP_TOKENS` / `MCP_TOKENS_FILE` — (optional) per-agent tokens (`client_id=token`); the
  file variant hot-reloads (add/revoke agents without restart). Needed for reservations to
  distinguish callers. `MCP_BEARER_TOKEN` — (optional) single shared token (legacy).
- `TUMBLLER_URL` / `TELLO_HOST` / `FAKEROBOT_URL` — (optional) robot addresses
- `PICAR_FREENOVE_URL` — (optional) PiCar address, default
  `http://picar-freenove.local:8080`. `PICAR_FREENOVE_TOKEN` — bearer token, only
  if the car runs with `ROBOT_TOKEN` set; omit when the robot has auth disabled.
- Payments/marketplace secrets (Stripe, …) live in `yakrobot-marketplace`, not here.

On-chain registration secrets (`SIGNER_PVT_KEY`, `PINATA_JWT`, `RPC_URL`, …) are **not
used here** — they live in `yakrobot-identity`, which performs registration from the
exported descriptor JSON.

## Development Guidelines

- When adding a new robot, create a package under `src/plugins/` — see `src/plugins/_template/`
- Robot adapter code is fully self-contained; do not put robot-specific logic in `src/core/`
- Add robot-specific dependencies as optional extras in `pyproject.toml`
- No framework code changes should be needed to add a new robot
- This repo holds **no chain code**: on-chain concerns belong in `yakrobot-identity`
- Use the `fakerobot` plugin for development/testing without physical hardware

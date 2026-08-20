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

Robot **controller**: per-robot MCP control + a browser driving console + local fleet
discovery + a generic per-robot reservation. Plugin-based so any robot is added with
minimal glue. Two ways in, one gateway: agents drive over MCP, humans drive from the
console — both reach the same robot. **No
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
│   │   ├── ws_proxy.py    # /{robot}/ws/* spliced through to the robot's own control server
│   │   ├── console.py     # /{robot}/ui — serves the driving console
│   │   ├── static/        # console.html: the whole console, one self-contained file
│   │   └── descriptor.py  # build_descriptor: plugin metadata → RobotDescriptor (export extra)
│   └── plugins/           # One sub-package per robot (device-neutral; IoT/printers later)
│       ├── tumbller/  tello/  fakerobot/  picar_freenove/  fakerobot_picar/  _template/
└── scripts/               # serve.py + export_descriptor.py (emits the JSON contract)
```

`fakerobot_picar` is the PiCar simulator — it serves the same `/ws/control` and
`/ws/video` a real car does, so the whole teleop path runs in tests without hardware.

## Architecture

- **FastAPI gateway** with ASGI sub-mounts — each robot gets its own isolated FastMCP
  instance. Single port, single ngrok tunnel.
- Endpoints: `/fleet/mcp` (local discovery only), `/{robot}/mcp` (control),
  `/{robot}/ui` (console), `/{robot}/ws/*` (realtime sockets).
- Every `/{robot}/mcp` tool passes through `ReservationMiddleware` — a robot reserved by
  one agent rejects control calls from others (identity = per-agent token `client_id`).
- Plugin auto-discovery scans `src/plugins/` for `RobotPlugin` subclasses.
- **Teleop is not MCP.** The console speaks its own JSON protocol over a WebSocket the
  gateway splices to the robot; it does not go through FastMCP or the reservation
  registry. Its own single-driver rule and deadman timer live *on the robot*.

**Route order is load-bearing.** `register_ws_proxy` and `register_console` must run
before `app.mount(f"/{name}", ...)` in `create_gateway`. Starlette matches in
registration order and a mount claims every path beneath its prefix, so a `/{robot}/ui`
or `/{robot}/ws/*` route added after the mounts is dead code that never sees a request.

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

Two optional hooks make a robot drivable from a browser — implement both on the
`RobotPlugin` subclass and the console and socket proxy appear for that robot with no
core changes:

- `control_base_urls()` — candidate base URLs of the robot's own control server (mDNS
  name, IP, …), tried in order. Returning `[]` opts the robot out: no `/ui`, no `/ws/*`.
  The Tello opts out this way — it speaks UDP and has no socket to proxy.
- `control_auth_token()` — the robot's own token, or `None`. It is injected into the
  upstream handshake so the browser never holds it.

## Key Technologies

- **Python 3.13+**, managed with `uv`
- **FastMCP** — MCP server framework
- **FastAPI + uvicorn** — ASGI gateway
- **websockets** — client half of the teleop proxy (the gateway connects outward as a
  client to each robot's control server)
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

# Browser teleop — the console is at /{robot}/ui on the serving gateway
uv run yakrobot-py serve --robots picar_freenove           # → :8000/picar_freenove/ui
uv run yakrobot-py sim --robot fakerobot_picar             # Simulated car on :8081 (with sockets)
uv run yakrobot-py serve --robots fakerobot_picar          # ...its gateway → :8000/fakerobot_picar/ui

# Tests (the teleop suite drives the simulator; no hardware, nothing to start by hand)
uv sync --extra dev
uv run pytest -q

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
- `TUMBLLER_URL` / `TELLO_HOST` / `FAKEROBOT_URL` / `FAKEROBOT_PICAR_URL` — (optional)
  robot addresses
- `PICAR_FREENOVE_URL` — (optional) PiCar address, default
  `http://picar-freenove.local:8080`. Accepts a **comma-separated candidate list**
  (mDNS name, IP, …) tried in order, because neither naming scheme is reliable alone.
  `PICAR_FREENOVE_TOKEN` — bearer token, only if the car runs with `ROBOT_TOKEN` set;
  omit when the robot has auth disabled.
- `VIDEO_ENABLED` — (optional) set `0`/`false`/`no`/`off` to refuse `/ws/video` for every
  client, for a metered or congested link. Control keeps working: the car stays drivable,
  just blind. Absent means enabled.
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
- Use the `fakerobot` plugin for development/testing without physical hardware, and
  `fakerobot_picar` when the change touches teleop (it serves the realtime sockets)

### Working on teleop

- **Keep safety on the robot.** Deadman, duty caps and the single-driver slot are
  enforced where the hardware is. The proxy must not grow its own — a gateway that
  believes it stopped a car it cannot reach is worse than one that never claimed to.
- **Never queue frames in the proxy.** Control messages carry *absolute* velocity, so a
  dropped frame is harmless but a late one re-applies a stale command after a stop.
  Relay directly and let backpressure reach the robot.
- **Refusals close after `accept()`, never before.** A browser reports every failed
  handshake as close code 1006, so a code sent before accept never reaches the page.
  `1008` = policy, stop retrying; `1013` = robot offline, keep retrying with backoff.
- **Closing a socket is best-effort.** The peer may already be gone, and on some
  uvicorn/websockets combinations that raises rather than no-ops. A browser vanishing
  mid-drive is routine, not an incident — never let it surface as a traceback.
- `console.html` is deliberately one self-contained file: inline CSS and JS, no build
  step, no asset requests. Over a long link, extra round trips cost more than bytes.

# yakrobot-gateway

Robot **controller**. Serves per-robot MCP control endpoints, a browser driving
console, local fleet discovery, and a generic per-robot reservation (who's in
control), behind a single port and one ngrok tunnel. **Contains no blockchain
code** — on-chain discovery and attestation reads live in the separate
[`yakrobot-identity`](https://github.com/pi-drg/yakrobot-identity) layer, and
registration is signed by a browser wallet outside both. **Task auctions /
marketplace** live in the separate `yakrobot-marketplace` service, which reaches
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
- Endpoints: `/fleet/mcp` (local discovery), `/{robot}/mcp` (per-robot control),
  `/{robot}/ui` (driving console), `/{robot}/ws/*` (realtime sockets).
- `/fleet/mcp` exposes `list_connected` (local plugin-registry introspection —
  **no chain**).
- Every `/{robot}/mcp` tool passes through a reservation guard (`robot_reserve` /
  `robot_release` / `robot_status`) so two agents can't drive one robot at once.
- Plugin auto-discovery scans `src/plugins/` for `RobotPlugin` subclasses.

`GET /` lists every mounted robot; a `ui_endpoint` in its entry means that robot
can be driven from a browser.

## Plugin system

Each robot is a package under `src/plugins/{name}/`:

- `__init__.py` — `RobotPlugin` subclass: `metadata()`, `tool_names()`, `register_tools(mcp)`
- `robot_adapter.py` — robot-facing adapter: robot-specific comms (HTTP, UDP, serial, SDK, …)
- `mcp_tools.py` — MCP-facing adapter: `register(mcp, robot)` defining `@mcp.tool` handlers

Tool naming: `{robot_prefix}_{action}` (e.g. `tumbller_move`).

A plugin that also implements `control_base_urls()` (and `control_auth_token()`
if its robot needs one) gets the console and the socket proxy for free. Returning
no URL is how a robot opts out — the Tello speaks UDP and has no socket to proxy,
so it is served over MCP only.

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
uv run yakrobot-py sim --robot fakerobot_picar      # ...the PiCar simulator instead (:8081, with sockets)
```

The legacy `uv run python scripts/serve.py …` entrypoint still works — it forwards to the
same implementation.

### Exporting a robot descriptor (for on-chain registration elsewhere)

This gateway holds **no chain code**. To put a robot on-chain you *export its descriptor
as JSON here*, and register it with that JSON elsewhere:

```bash
uv sync --extra export
uv run yakrobot-py export tumbller   # --public-domain defaults from $NGROK_DOMAIN / $CLOUDFLARE_DOMAIN
# writes robot-descriptors/tumbller.json (a gitignored, regenerable artifact)
```

The descriptor shape is the shared
[`yakrobot-descriptor`](https://github.com/pi-drg/yakrobot-descriptor) contract
(`descriptor.schema.json`) — the JSON is the only thing that crosses between the repos;
neither imports the other's types. See [End-to-end flow](#end-to-end-flow).

A running gateway also serves the same document **live**, so nothing has to hand a file
around:

```bash
curl -s localhost:8000/                        # which robots, and each descriptor_endpoint
curl -s localhost:8000/tumbller/descriptor     # the descriptor itself
```

Both routes send `Access-Control-Allow-Origin: *`, because the browser page that
registers a robot reads them from another origin; they are the only cross-origin
surfaces here. The public endpoints inside the descriptor resolve from `$NGROK_DOMAIN`,
then `$CLOUDFLARE_DOMAIN`, then the request's own `Host` header — so a gateway behind a
tunnel is registerable with no configuration at all. Without the `export` extra the route
answers `501`, and with no resolvable public host `503`, rather than emitting a
descriptor whose MCP URL is empty.

Registration is signed by a browser wallet, so there is no CLI for it in any repo:
`yakrobot-identity` is read-only. To *find* or *verify* robots already on-chain, use its
`scripts/discover.py` and `scripts/attestations.py`.

`fleet_provider` and `fleet_domain` are exported **empty**. A gateway cannot verify whose
fleet it belongs to, so it makes no unverified claim; whoever registers the robot supplies
them.

## Driving from a browser

Robots with a realtime control server are drivable from `/{robot}/ui` — a
self-contained console (keyboard, on-screen pad, live video, latency gauge) that
the gateway serves itself. The page opens `/{robot}/ws/control` and
`/{robot}/ws/video`, which the gateway proxies the last LAN hop to the robot;
the robot needs no public address of its own.

```bash
uv sync --extra picar-freenove
uv run yakrobot-py serve --robots picar_freenove    # then open localhost:8000/picar_freenove/ui
```

Hardware-free, in two terminals — the simulator serves the same sockets a real
car does:

```bash
uv run yakrobot-py sim --robot fakerobot_picar      # terminal 1  (:8081)
uv run yakrobot-py serve --robots fakerobot_picar   # terminal 2  → :8000/fakerobot_picar/ui
```

The console is served unauthenticated even when gateway tokens are set — it is
inert markup, and every socket it opens is checked on connect. With `MCP_TOKENS`
configured, open it as `/{robot}/ui?token=…`; the page passes that token to its
sockets, which is where it is actually enforced.

Safety and etiquette live on the robot, not here: a deadman timer stops a car
whose operator goes quiet, and only one browser holds control at a time (others
watch). Video is the expensive half of the link — the console can turn it off
per operator, and `VIDEO_ENABLED=0` refuses it gateway-wide, leaving the car
drivable but blind.

## End-to-end flow

[`yakrobot-descriptor`](https://github.com/pi-drg/yakrobot-descriptor) is the seam
between this repo and the chain side. Neither imports the other; both depend on the
contract package, and a JSON document is the only thing that crosses:

```
    ┌─ yakrobot-gateway ────┐              ┌─ yakrobot-identity ───┐
    │  this repo            │              │  reads the chain      │
    │  serves robots        │              │                       │
    │                       │  robot.json  │                       │
    │  plugin.metadata()    │              │  descriptor_io.py     │
    │          │            │              │          │            │
    │          ▼            │              │          ▼            │
    │  yakrobot-py export ──┼─────────────►│  build_onchain_       │
    │                       │              │       metadata()      │
    └───────────┬───────────┘              └───────────┬───────────┘
                │                                      │
                │        both validate against         │
                ▼                                      ▼
          ┌─ yakrobot-descriptor ───────────────────────────┐
          │  descriptor.schema.json                         │
          │  the only dependency the two of them share      │
          └─────────────────────────────────────────────────┘
```

A plugin's `metadata()` is the source of truth; `export` writes it out as a descriptor
validated against the shared schema. Because the contract is a JSON Schema rather than a
Python type, this gateway can be replaced by a producer in another language without the
chain side changing — and this repo keeps no robot types in common with it.

```
1. serve     (this repo)  bring robots online behind one ngrok URL
2. export    (this repo)  write each robot's descriptor JSON (yakrobot-descriptor contract)
3. register  (browser wallet)     put each robot on-chain from that JSON
4. attest    (browser wallet)     vouch for a robot via EAS  (optional)
5. discover  (yakrobot-identity)  find attested robots, write .mcp.json for a client
6. connect   the client talks to this gateway's /{robot}/mcp endpoints
```

Steps 3–4 are signed transactions and are made from a browser wallet — no repo holds a
key for them. Step 5 runs from
[`yakrobot-identity`](https://github.com/pi-drg/yakrobot-identity), which is read-only.
This gateway owns steps 1–2 only: it serves robots and exports their descriptors, and
holds no chain code.

## Environment

Serving with a tunnel: `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`.
Auth: `MCP_TOKENS`/`MCP_TOKENS_FILE` (per-agent tokens — needed for reservations to tell
callers apart) or `MCP_BEARER_TOKEN` (single shared token).
Optional: `TUMBLLER_URL`, `TELLO_HOST`, `FAKEROBOT_URL`, `FAKEROBOT_PICAR_URL`.
`PICAR_FREENOVE_URL` takes a comma-separated candidate list (mDNS name, IP, …) — the
first that answers wins; `PICAR_FREENOVE_TOKEN` only if the car runs with auth on.
Teleop: `VIDEO_ENABLED=0` refuses `/ws/video` for every client.
No chain secrets live in any of these repos: registration and attestation are signed by a
browser wallet, and `yakrobot-identity` is read-only. Payment/marketplace secrets live in
`yakrobot-marketplace` — not here.

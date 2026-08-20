"""Fake PiCar — emulates `picar_freenove_fastapi` with no hardware attached.

Stands in for the real Freenove 4WD car so the teleop UI and the gateway's
WebSocket proxy can be built and tested without a robot, a Raspberry Pi, or
anyone in Finland to pick the car up off the floor.

It emulates two surfaces:

* **The HTTP control surface** the real car serves today — same paths, same
  response shapes — so the PiCar MCP adapter drives this unchanged.
* **The `/ws/control` and `/ws/video` realtime protocol** — absolute-state
  drive messages, clamped look echoes, ping/pong and periodic telemetry, with
  binary JPEG frames on the video socket. Message shapes are listed under
  "websockets" below. When this and the real car disagree, one of them is a
  bug, and this file is the cheaper place to find out which.

Behaviours deliberately reproduced, because they are what teleop code gets
wrong and what a hardware test cycle is expensive to discover:

* **Deadman.** Velocities zero themselves `DEADMAN_MS` after the last drive
  command, exactly as `Robot._arm_auto_stop` does on the car. Stop sending
  heartbeats and the simulated car stops — visibly, in the video stream.
* **Single-driver slot.** A second control socket is accepted but told
  ``{"type":"error","detail":"busy"}`` and its drive commands are ignored; it
  still receives telemetry and video, which is what a spotter wants.
* **Clamps.** Duties to ±`MAX_DUTY`, servos to 30-150°, matching
  `config.servo_angle_limits`.
* **Tilt travels horizon-and-upward.** Higher tilt looks *up*; there is no
  looking down on this car. The horizon in the rendered frame moves
  accordingly, so an inverted axis in the UI is visible immediately.

Run standalone::

    uv run python -m plugins.fakerobot_picar.simulator          # port 8081
"""

import asyncio
import io
import json
import math
import random
import time
from dataclasses import dataclass, field

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from PIL import Image, ImageDraw
from pydantic import BaseModel

# --- protocol constants ------------------------------------------------------

DEADMAN_MS = 700
MAX_DUTY = 1400
TELEMETRY_S = 3.0
SERVO_MIN, SERVO_MAX = 30, 150
FRAME_SIZE = (400, 300)
VIDEO_FPS = 15
DEFAULT_PORT = 8081

SKY = (44, 62, 84)
GROUND = (28, 34, 30)
GRID = (86, 120, 96)
HORIZON_LINE = (150, 190, 160)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class SimState:
    vx: int = 0
    vy: int = 0
    omega: int = 0
    pan: int = 90
    tilt: int = 90

    # Integrated pose — drives the rendered view so motion is actually visible.
    odom_x: float = 0.0
    odom_y: float = 0.0
    yaw: float = 0.0

    battery_v: float = 8.02
    frame: int = 0
    # monotonic deadline after which velocities zero themselves; 0 = stopped
    drive_expires: float = 0.0
    controller: object | None = None
    leds: dict = field(default_factory=lambda: {"effect": "off", "r": 0, "g": 0, "b": 0})

    def stop(self) -> None:
        self.vx = self.vy = self.omega = 0
        self.drive_expires = 0.0

    def distance_cm(self) -> float:
        """Synthetic ultrasonic: a wall 120 cm ahead that you can drive toward."""
        ahead = 120.0 - self.odom_x * 2.0
        while ahead < 10:  # drove through it; pretend the next wall is ahead
            ahead += 200.0
        return round(_clamp(ahead + random.uniform(-1.5, 1.5), 2.0, 400.0), 1)


STATE = SimState()


# --- video -------------------------------------------------------------------


def render_frame() -> bytes:
    """One JPEG frame of a synthetic first-person view.

    The point is not realism — it is that every control input changes the
    picture in a distinguishable way, so a broken axis, an inverted sign or a
    dead deadman is obvious on screen instead of subtle.
    """
    w, h = FRAME_SIZE
    s = STATE

    # Higher tilt = looking further up = horizon lower in frame.
    horizon = int(_clamp(h * 0.42 + (s.tilt - 90) * 1.6, 20, h - 40))

    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:horizon] = SKY
    img[horizon:] = GROUND
    img[horizon:horizon + 2] = HORIZON_LINE

    # Ground lines receding to the horizon, scrolling with forward motion:
    # spacing ~ 1/distance is what makes translation read as translation.
    scroll = s.odom_x % 1.0
    for i in range(1, 16):
        y = horizon + int(260.0 / (i + scroll))
        if horizon + 2 < y < h - 1:
            img[y:y + 2, :] = GRID

    # Verticals carry strafe (odom_y) and rotation (yaw); pan shifts the whole
    # view sideways because the camera, not the car, is turning.
    offset = (s.odom_y * 9.0 + s.yaw * 110.0 + (s.pan - 90) * 2.2) % 48
    for x in range(-48, w + 48, 48):
        xx = int(x + offset)
        if 0 <= xx < w - 1:
            img[horizon:, xx:xx + 2] = GRID

    im = Image.fromarray(img)
    d = ImageDraw.Draw(im)

    # Strobe: flips every frame, so glass-to-glass latency can be measured by
    # filming the operator's screen next to the robot's.
    d.rectangle([w - 26, 6, w - 6, 26], fill=(255, 255, 255) if s.frame % 2 else (0, 0, 0))

    wall_ms = int(time.time() * 1000) % 100000
    d.text((8, 6), f"FAKE PiCar  frame {s.frame:06d}  t={wall_ms:05d}ms", fill=(230, 240, 235))
    d.text((8, 22), f"vx {s.vx:+5d}  vy {s.vy:+5d}  omega {s.omega:+5d}", fill=(180, 220, 190))
    d.text((8, 38), f"pan {s.pan:3d}  tilt {s.tilt:3d}", fill=(180, 220, 190))
    d.text((8, h - 34), f"batt {s.battery_v:.2f} V", fill=(200, 210, 205))

    dist = s.distance_cm()
    d.text((8, h - 18), f"dist {dist:6.1f} cm", fill=(255, 90, 90) if dist < 30 else (200, 210, 205))
    if s.vx == s.vy == s.omega == 0:
        d.text((w // 2 - 20, h // 2), "STOPPED", fill=(255, 170, 90))

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    s.frame += 1
    return buf.getvalue()


# --- physics + deadman -------------------------------------------------------


async def _tick_loop() -> None:
    """Integrate motion and enforce the deadman, at 50 Hz.

    Runs for the app's lifetime rather than per-connection: the car must keep
    coasting to a stop (and the video keep showing it) after the last control
    socket has gone, which is precisely the failure this simulates.
    """
    dt = 0.02
    while True:
        s = STATE
        if s.drive_expires and time.monotonic() > s.drive_expires:
            s.stop()

        s.odom_x += s.vx / 1200.0 * dt * 6.0
        s.odom_y += s.vy / 1200.0 * dt * 6.0
        s.yaw += s.omega / 900.0 * dt * 1.2

        # Battery sags under load and recovers when idle — enough to make a
        # low-battery HUD warning testable without waiting for a real cell.
        load = (abs(s.vx) + abs(s.vy) + abs(s.omega)) / 3000.0
        s.battery_v = round(_clamp(s.battery_v - load * 0.0015 + 0.0004, 6.2, 8.4), 3)
        await asyncio.sleep(dt)


# --- app ---------------------------------------------------------------------


async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Fake PiCar Simulator", lifespan=_lifespan)


class DriveReq(BaseModel):
    direction: str
    duty: int = 1200
    duration_ms: int = 800


class MecanumReq(BaseModel):
    vx: int = 0
    vy: int = 0
    omega: int = 0
    duration_ms: int = 800


class LookReq(BaseModel):
    pan: int = 90
    tilt: int = 90


def _apply_drive(vx: int, vy: int, omega: int, duration_ms: int = DEADMAN_MS) -> None:
    STATE.vx = int(_clamp(vx, -MAX_DUTY, MAX_DUTY))
    STATE.vy = int(_clamp(vy, -MAX_DUTY, MAX_DUTY))
    STATE.omega = int(_clamp(omega, -MAX_DUTY, MAX_DUTY))
    moving = any((STATE.vx, STATE.vy, STATE.omega))
    STATE.drive_expires = time.monotonic() + duration_ms / 1000.0 if moving else 0.0


def _apply_look(pan: int, tilt: int) -> dict:
    STATE.pan = int(_clamp(pan, SERVO_MIN, SERVO_MAX))
    STATE.tilt = int(_clamp(tilt, SERVO_MIN, SERVO_MAX))
    return {"pan": STATE.pan, "tilt": STATE.tilt}


@app.get("/info")
async def info():
    return {
        "robot": "freenove-4wd-sim",
        "hardware": "ok",
        "battery_v": STATE.battery_v,
        "wheels": "mecanum",
        "holonomic": True,
    }


@app.get("/battery")
async def battery():
    return {"volts": STATE.battery_v}


@app.get("/distance")
async def distance():
    return {"cm": STATE.distance_cm()}


@app.get("/scan")
async def scan():
    return {str(a): round(STATE.distance_cm() + random.uniform(-20, 60), 1)
            for a in range(30, 151, 30)}


@app.get("/line")
async def line():
    return {"left": False, "middle": True, "right": False}


@app.post("/drive")
async def drive(req: DriveReq):
    vectors = {
        "forward": (req.duty, 0, 0), "back": (-req.duty, 0, 0),
        "left": (0, 0, req.duty), "right": (0, 0, -req.duty),
        "stop": (0, 0, 0),
    }
    if req.direction not in vectors:
        raise HTTPException(status_code=500, detail=f"unknown direction {req.direction}")
    _apply_drive(*vectors[req.direction], duration_ms=req.duration_ms)
    return {"direction": req.direction}


@app.post("/mecanum")
async def mecanum(req: MecanumReq):
    _apply_drive(req.vx, req.vy, req.omega, req.duration_ms)
    return {"duties": {"left_front": STATE.vx, "left_rear": STATE.vx,
                       "right_front": STATE.vx, "right_rear": STATE.vx}}


@app.post("/look")
async def look(req: LookReq):
    return {"status": "ok", **_apply_look(req.pan, req.tilt)}


@app.get("/snapshot")
async def snapshot():
    return Response(content=render_frame(), media_type="image/jpeg")


@app.get("/led")
async def led_state():
    return STATE.leds


@app.post("/led")
async def led(payload: dict):
    STATE.leds = {**STATE.leds, **payload}
    return STATE.leds


# --- websockets --------------------------------------------------------------
#
# /ws/control, JSON text frames.
#
#   up:   {"type":"drive","vx":1200,"vy":0,"omega":0}   absolute state, not deltas
#         {"type":"look","pan":96,"tilt":84}            absolute angles
#         {"type":"stop"}
#         {"type":"ping","t":<client clock ms>}
#
#   down: {"type":"hello","wheels":"mecanum","holonomic":true,"battery_v":7.9,
#          "deadman_ms":700,"max_duty":1400,"controller":true}
#         {"type":"look","pan":96,"tilt":84}            CLAMPED echo
#         {"type":"telemetry","battery_v":7.8,"distance_cm":87.2}
#         {"type":"pong","t":<echoed>}
#         {"type":"error","detail":"busy"}
#
# /ws/video streams binary JPEG frames and takes no input.


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await ws.accept()

    # First socket in owns the car; later ones are spotters. Identity is the
    # socket object itself, so a reconnect after a drop is a new controller
    # only once the old socket's finally-block has released the slot.
    is_controller = STATE.controller is None
    if is_controller:
        STATE.controller = ws
    else:
        await ws.send_text(json.dumps({"type": "error", "detail": "busy"}))

    await ws.send_text(json.dumps({
        "type": "hello", "wheels": "mecanum", "holonomic": True,
        "battery_v": STATE.battery_v, "deadman_ms": DEADMAN_MS,
        "max_duty": MAX_DUTY, "controller": is_controller,
    }))

    async def telemetry() -> None:
        while True:
            await asyncio.sleep(TELEMETRY_S)
            await ws.send_text(json.dumps({
                "type": "telemetry",
                "battery_v": STATE.battery_v,
                "distance_cm": STATE.distance_cm(),
            }))

    telemetry_task = asyncio.create_task(telemetry())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")

            if kind == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t": msg.get("t")}))
                continue

            # A spotter may watch and ping, but not drive.
            if not is_controller:
                if kind in ("drive", "look", "stop"):
                    await ws.send_text(json.dumps({"type": "error", "detail": "busy"}))
                continue

            if kind == "drive":
                _apply_drive(msg.get("vx", 0), msg.get("vy", 0), msg.get("omega", 0))
            elif kind == "stop":
                STATE.stop()
            elif kind == "look":
                echo = _apply_look(msg.get("pan", STATE.pan), msg.get("tilt", STATE.tilt))
                await ws.send_text(json.dumps({"type": "look", **echo}))
            else:
                await ws.send_text(json.dumps(
                    {"type": "error", "detail": f"unknown message type {kind!r}"}))
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    finally:
        telemetry_task.cancel()
        if is_controller:
            STATE.stop()          # matches the real server's finally: robot.stop()
            STATE.controller = None


@app.websocket("/ws/video")
async def ws_video(ws: WebSocket):
    await ws.accept()
    interval = 1.0 / VIDEO_FPS
    try:
        while True:
            started = time.monotonic()
            await ws.send_bytes(render_frame())
            # Sleep the remainder rather than a fixed interval, so a slow
            # encode drops the rate instead of building a backlog — the
            # latest-frame model the real camera path uses.
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
    except (WebSocketDisconnect, RuntimeError):
        pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)

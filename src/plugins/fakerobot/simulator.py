"""
Fake robot HTTP server — emulates Tumbller-compatible endpoints.

Run standalone:  uv run python -m plugins.fakerobot.simulator
Starts on port 8080 by default.
"""

import asyncio
import random
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="Fake Robot Simulator")

# Simulated state
_state = {
    "direction": "stop",
    "moving_since": None,
    "temperature": 22.5,
    "humidity": 45.0,
}

# Auto-stop durations (seconds), matching real Tumbller behavior
_AUTO_STOP = {
    "forward": 2.0,
    "back": 2.0,
    "left": 1.0,
    "right": 1.0,
}


def _drift_sensor():
    """Add small random drift to sensor readings for realism."""
    _state["temperature"] += random.uniform(-0.3, 0.3)
    _state["humidity"] += random.uniform(-0.5, 0.5)
    _state["temperature"] = round(max(15.0, min(35.0, _state["temperature"])), 1)
    _state["humidity"] = round(max(20.0, min(80.0, _state["humidity"])), 1)


@app.get("/motor/{direction}")
async def motor(direction: str):
    """Move the fake robot. Matches Tumbller endpoint behavior.

    forward/back auto-stop after 2s, left/right after 1s.
    Returns plain-text HTML like the real ESP32 firmware.
    """
    if direction == "stop":
        _state["direction"] = "stop"
        _state["moving_since"] = None
        return HTMLResponse(f"<h1>Motor: stop</h1>")

    if direction not in _AUTO_STOP:
        return {"error": f"Unknown direction: {direction}"}

    _state["direction"] = direction
    _state["moving_since"] = time.time()

    # Simulate auto-stop delay (non-blocking)
    duration = _AUTO_STOP[direction]

    async def auto_stop():
        await asyncio.sleep(duration)
        if _state["direction"] == direction:
            _state["direction"] = "stop"
            _state["moving_since"] = None

    asyncio.create_task(auto_stop())

    return HTMLResponse(f"<h1>Motor: {direction}</h1>")


@app.get("/info")
async def info():
    """Robot info endpoint. Returns JSON like the real Tumbller."""
    return {
        "name": "Fake Robot",
        "firmware": "simulator-1.0.0",
        "uptime_seconds": int(time.time()) % 86400,
        "direction": _state["direction"],
    }


@app.get("/sensor/ht")
async def sensor_ht():
    """Temperature and humidity sensor. Returns JSON with drifting values."""
    _drift_sensor()
    return {
        "temperature": _state["temperature"],
        "humidity": _state["humidity"],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

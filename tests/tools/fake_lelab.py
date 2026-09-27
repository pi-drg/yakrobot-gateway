"""A fake LeLab backend for relay and plugin tests (execution plan §7.1).

Serves exactly the surface the ``lelab_so101`` plugin's ``ROUTES`` allowlist forwards,
plus the two routes the tests must prove are *never* reached (``POST /system/update`` and
``POST /hf-auth/login``). Every call is recorded in ``state.calls`` as
``(method, path, json_body)`` so a test can assert both what was forwarded and what was
not. A ``__main__`` block serves it on ``:8001`` for manual runs.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse


@dataclass
class LeLabState:
    """Mutable state the fake serves from, and the call log.

    The rig starts configured (the §7.1 "returns /dev/ttyFAKE-{t}" default) so the
    happy-path tools work out of the box; a test that needs "no saved rig" clears these
    two dicts, and the endpoints then return a ``None`` value for ``saved_*``.
    """

    saved_port: dict[str, str] = field(
        default_factory=lambda: {"leader": "/dev/ttyFAKE-leader", "follower": "/dev/ttyFAKE-follower"}
    )
    saved_config: dict[str, str] = field(
        default_factory=lambda: {"leader": "leader.json", "follower": "follower.json"}
    )
    calls: list[tuple[str, str, object]] = field(default_factory=list)

    def record(self, method: str, path: str, body: object = None) -> None:
        self.calls.append((method, path, body))


# A single tiny frame for the MJPEG stream and the Range test. Real JPEG bytes are not
# required — the relay forwards opaque bytes and the tests only count and close them.
_FRAME = b"\xff\xd8frame-bytes\xff\xd9"


def create_fake_lelab(state: LeLabState) -> FastAPI:
    app = FastAPI()

    def _record(method: str, path: str, body: object = None) -> None:
        state.record(method, path, body)

    # -- GET status / data -----------------------------------------------------

    @app.get("/teleoperation-status")
    async def teleoperation_status():
        _record("GET", "/teleoperation-status")
        return {"connected": True, "fps": 30}

    @app.get("/recording-status")
    async def recording_status():
        _record("GET", "/recording-status")
        return {"recording": False, "episode": 0}

    @app.get("/inference-status")
    async def inference_status():
        _record("GET", "/inference-status")
        return {"running": False, "policy": ""}

    @app.get("/joint-positions")
    async def joint_positions():
        _record("GET", "/joint-positions")
        return {"leader": [0.0] * 6, "follower": [0.0] * 6}

    @app.get("/datasets")
    async def datasets():
        _record("GET", "/datasets")
        return {"datasets": []}

    @app.get("/available-cameras")
    async def available_cameras():
        _record("GET", "/available-cameras")
        return {"cameras": [{"index": 0, "name": "front"}]}

    @app.get("/robot-port/{t}")
    async def robot_port(t: str):
        _record("GET", f"/robot-port/{t}")
        return {"saved_port": state.saved_port.get(t)}

    @app.get("/robot-config/{t}")
    async def robot_config(t: str):
        _record("GET", f"/robot-config/{t}")
        return {"saved_config": state.saved_config.get(t)}

    # -- POST commands (the allowlisted ones) ----------------------------------

    @app.post("/start-recording")
    async def start_recording(request: Request):
        body = await request.json()
        _record("POST", "/start-recording", body)
        return {"status": "recording"}

    @app.post("/stop-recording")
    async def stop_recording():
        _record("POST", "/stop-recording", None)
        return {"status": "stopped"}

    @app.post("/recording-exit-early")
    async def recording_exit_early():
        _record("POST", "/recording-exit-early", None)
        return {"status": "exited"}

    @app.post("/start-inference")
    async def start_inference(request: Request):
        body = await request.json()
        _record("POST", "/start-inference", body)
        return {"status": "inferring"}

    @app.post("/stop-inference")
    async def stop_inference():
        _record("POST", "/stop-inference", None)
        return {"status": "stopped"}

    @app.post("/move-arm")
    async def move_arm(request: Request):
        body = await request.json()
        _record("POST", "/move-arm", body)
        return {"status": "moving"}

    @app.post("/upload-dataset")
    async def upload_dataset(request: Request):
        body = await request.json()
        _record("POST", "/upload-dataset", body)
        return {"status": "uploaded"}

    @app.post("/save-robot-port")
    async def save_robot_port(request: Request):
        body = await request.json()
        _record("POST", "/save-robot-port", body)
        return {"status": "saved"}

    @app.post("/save-robot-config")
    async def save_robot_config(request: Request):
        body = await request.json()
        _record("POST", "/save-robot-config", body)
        return {"status": "saved"}

    # -- Routes the relay must NEVER reach -------------------------------------

    @app.post("/system/update")
    async def system_update(request: Request):
        body = await request.json()
        _record("POST", "/system/update", body)
        return {"status": "updated"}

    @app.post("/hf-auth/login")
    async def hf_auth_login(request: Request):
        body = await request.json()
        _record("POST", "/hf-auth/login", body)
        return {"status": "ok"}

    # -- Streaming -------------------------------------------------------------

    @app.get("/camera-feed/{cam}")
    async def camera_feed(cam: str):
        _record("GET", f"/camera-feed/{cam}")
        boundary = b"--lelab-frame"

        async def gen():
            try:
                while True:
                    yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + _FRAME + b"\r\n"
                    await asyncio.sleep(0.1)  # 10 fps
            finally:
                _record("DISCONNECT", f"/camera-feed/{cam}")

        return StreamingResponse(
            gen(), media_type="multipart/x-mixed-replace; boundary=lelab-frame"
        )

    @app.get("/dataset-video")
    async def dataset_video():
        _record("GET", "/dataset-video")
        # 64 KiB so Range requests and Content-Range work.
        body = (_FRAME * (65536 // len(_FRAME) + 1))[:65536]
        return FileResponse(
            _tmp_file(body),
            media_type="video/mp4",
            filename="episode_0.mp4",
        )

    # -- WebSocket -------------------------------------------------------------

    @app.websocket("/ws/joint-data")
    async def ws_joint_data(ws: WebSocket):
        await ws.accept()
        _record("WS", "/ws/joint-data")
        try:
            while True:
                await ws.send_json({"type": "joint_update", "joints": {"leader": [0.0] * 6}})
                # LeLab's handler reads with a 1s timeout, so echo any inbound text.
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            _record("WS-DISCONNECT", "/ws/joint-data")

    return app


_TMP_FILES: list[Path] = []


def _tmp_file(body: bytes) -> Path:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(body)
        path = Path(f.name)
    _TMP_FILES.append(path)
    return path


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_fake_lelab(LeLabState()), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8001)

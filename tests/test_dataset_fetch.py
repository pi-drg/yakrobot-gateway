"""Tests for `dataset fetch` (execution plan step 4.2)."""

import asyncio
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yakrobot_cli.dataset_fetch import fetch  # noqa: E402


def _serve(app) -> tuple[int, object]:
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

    def run() -> None:
        asyncio.run(server.serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return port, server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def _links_server(files: dict[str, bytes], tamper: dict[str, bytes] | None = None):
    from fastapi import FastAPI, Request, Response

    app = FastAPI()

    @app.get("/links")
    def links(request: Request):
        base = str(request.base_url).rstrip("/")
        entries = [
            {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             "url": f"{base}/file/{path}"}
            for path, data in files.items()
        ]
        return {"store": "r2", "files": entries, "expires_at": 0}

    @app.get("/file/{path:path}")
    def file(path: str):
        from fastapi.responses import Response

        data = (tamper or {}).get(path, files[path])
        return Response(content=data, media_type="application/octet-stream")

    return app


def test_fetch_links_verifies_sha256(tmp_path):
    files = {
        "meta/info.json": b'{"codebase_version":"v3.0"}',
        "videos/chunk-000/file-000.mp4": b"\x00\x00\x00\x18ftypmp42",
        "README.md": b"# card\n",
    }
    port, _ = _serve(_links_server(files))
    out = tmp_path / "picar_red_box_v1"
    fetch(links=f"http://127.0.0.1:{port}/links", out=str(out))
    assert (out / "meta/info.json").read_bytes() == files["meta/info.json"]
    assert (out / "videos/chunk-000/file-000.mp4").read_bytes() == files["videos/chunk-000/file-000.mp4"]
    assert (out / "README.md").read_bytes() == files["README.md"]


def test_fetch_rejects_tampered_file(tmp_path):
    files = {"a.bin": b"correct-content-123"}
    port, _ = _serve(_links_server(files, tamper={"a.bin": b"tampered-content-45"}))
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        fetch(links=f"http://127.0.0.1:{port}/links", out=str(tmp_path / "out"))
    # The partial file was not left behind.
    assert not (tmp_path / "out" / "a.bin").exists()

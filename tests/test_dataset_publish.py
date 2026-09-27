"""Tests for `dataset publish` (execution plan step 3.3).

R2 publishes run against the local fake R2 (a real uvicorn server on an ephemeral port);
hf publishes are structural and monkeypatch HfApi.
"""

import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yakrobot_cli.dataset_publish import build_manifest, publish  # noqa: E402
from tools.fake_r2 import FakeR2State, create_fake_r2  # noqa: E402


def _fake_dataset(tmp_path: Path) -> Path:
    """A minimal but structurally valid LeRobot v3 tree (validate() passes without lerobot)."""
    root = tmp_path / "ds"
    (root / "meta" / "episodes").mkdir(parents=True)
    (root / "videos").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0",
        "robot_type": "fakerobot_picar",
        "total_episodes": 1,
        "total_frames": 1,
    }))
    (root / "meta" / "episodes" / "file-000.parquet").write_bytes(b"episodes")
    (root / "videos" / "file-000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (root / "README.md").write_text("# card\n")
    return root


def _serve(app) -> tuple[int, object]:
    import asyncio

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

    def _run() -> None:
        asyncio.run(server.serve())

    threading.Thread(target=_run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return port, server
        time.sleep(0.05)
    raise RuntimeError("fake R2 did not start")


def _r2_env(monkeypatch, tmp_path: Path, port: int, datasets_file: Path):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "read")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "rsecret")
    monkeypatch.setenv("R2_WRITE_ACCESS_KEY_ID", "write")
    monkeypatch.setenv("R2_WRITE_SECRET_ACCESS_KEY", "wsecret")
    monkeypatch.setenv("DATASETS_FILE", str(datasets_file))


def _fresh_fake_r2() -> FakeR2State:
    state = FakeR2State()
    state.keys["write"] = "wsecret"
    state.keys["read"] = "rsecret"
    state.read_only.add("read")
    return state


def test_manifest_is_sorted_and_hashed():
    files = [("b.txt", b"b"), ("meta/info.json", b"{}"), ("a.txt", b"a")]
    rev, manifest = build_manifest("d", files)
    assert manifest["format"] == 1
    assert manifest["dataset"] == "d"
    assert [f["path"] for f in manifest["files"]] == ["a.txt", "b.txt", "meta/info.json"]
    assert manifest["files"][0] == {"path": "a.txt", "bytes": 1, "sha256": hashlib.sha256(b"a").hexdigest()}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(raw.encode()).hexdigest() == rev


def test_publish_r2_to_fake(tmp_path, monkeypatch):
    root = _fake_dataset(tmp_path)
    state = _fresh_fake_r2()
    port, _server = _serve(create_fake_r2(state))
    datasets_file = tmp_path / "datasets.toml"
    _r2_env(monkeypatch, tmp_path, port, datasets_file)

    publish(str(root), "r2", "picar_r2_v1", datasets_file=str(datasets_file))

    # Object layout: everything under datasets/{id}/{rev}/, manifest PUT last.
    keys = set(state.objects)
    assert any(k.startswith("datasets/picar_r2_v1/") for k in keys)
    assert any(k.endswith("/preview/README.md") for k in keys)
    manifest_key = state.put_order[-1]
    assert manifest_key.endswith("/manifest.json")
    rev = manifest_key.split("/")[2]
    assert f"datasets/picar_r2_v1/{rev}/README.md" in keys
    assert f"datasets/picar_r2_v1/{rev}/meta/info.json" in keys
    assert f"datasets/picar_r2_v1/{rev}/preview/README.md" in keys

    # Manifest content: sorted, hashed, rev = sha256 of its bytes.
    manifest = json.loads(state.objects[manifest_key])
    assert manifest["format"] == 1 and manifest["dataset"] == "picar_r2_v1"
    assert hashlib.sha256(state.objects[manifest_key]).hexdigest() == rev

    # Listing appended.
    text = datasets_file.read_text()
    assert 'id = "picar_r2_v1"' in text
    assert f'rev = "{rev}"' in text


def test_publish_r2_refuses_existing_rev(tmp_path, monkeypatch):
    from yakrobot_cli.dataset_publish import _walk_files

    root = _fake_dataset(tmp_path)
    rev, manifest = build_manifest("picar_r2_v1", _walk_files(root))
    state = _fresh_fake_r2()
    state.objects[f"datasets/picar_r2_v1/{rev}/manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    port, _server = _serve(create_fake_r2(state))
    datasets_file = tmp_path / "datasets.toml"
    _r2_env(monkeypatch, tmp_path, port, datasets_file)

    with pytest.raises(SystemExit, match="already exists"):
        publish(str(root), "r2", "picar_r2_v1", datasets_file=str(datasets_file))


def test_publish_hf_refuses_listed_repo(tmp_path, monkeypatch):
    root = _fake_dataset(tmp_path)
    datasets_file = tmp_path / "datasets.toml"
    datasets_file.write_text(
        '[[dataset]]\n'
        'id = "picar_hf_v1"\n'
        'robot = "fakerobot_picar"\n'
        'store = "hf"\n'
        'hub_repo = "myname/picar_hf_v1"\n'
        'rev = "' + "a" * 40 + '"\n'
        'episodes = 1\n'
        'frames = 1\n'
        'license = "cc-by-nc-4.0"\n'
        'source = "gateway"\n'
    )
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("DATASETS_FILE", str(datasets_file))

    with pytest.raises(SystemExit, match="already listed"):
        publish(str(root), "hf", "picar_hf_v1", repo_id="myname/picar_hf_v1",
                datasets_file=str(datasets_file))

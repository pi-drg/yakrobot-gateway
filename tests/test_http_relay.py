"""Tests for the plugin UI/HTTP hooks (execution plan §0.15) and, later in this file,
the operator session and relay (steps 7.3–7.4).

The §0.15 hook tests use tiny ``RobotPlugin`` subclasses defined here so no real robot
package or its dependencies are needed.
"""

import os
import socket
import time
from pathlib import Path

import pytest

# Import before clearing env (see test_ws_teleop._Stack's note on load_dotenv running at
# module level).
import core.server  # noqa: F401
import yakrobot_cli  # noqa: F401

from core.plugin import ApiRoute, HttpApi, RobotMetadata, RobotPlugin, StaticUi  # noqa: E402
from core.server import create_gateway  # noqa: E402


_CLEARED_ENV_VARS = (
    "MCP_TOKENS", "MCP_BEARER_TOKEN", "MCP_TOKENS_FILE",
    "NGROK_DOMAIN", "CLOUDFLARE_DOMAIN",
    "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
    "TELEOP_PRICE_USDC", "TELEOP_LEASE_MINUTES",
    "STRIPE_GATE_ENABLED", "STRIPE_SECRET_KEY", "STRIPE_PRICE_CENTS",
    "STRIPE_CURRENCY", "STRIPE_API_BASE", "STRIPE_AUTOMATIC_TAX", "STRIPE_TAX_CODE",
    "RECORDING_ENABLED", "RECORDINGS_DIR", "RECORDINGS_MAX_GB",
    "RECORDING_QUEUE_FRAMES", "VIDEO_ENABLED",
    "DATASETS_FILE", "DATASET_REDEEM_DAYS", "HF_TOKEN",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_URL_TTL_S", "R2_ENDPOINT",
    "LELAB_URL", "LELAB_UI_DIR", "UI_SESSION_HOURS",
)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path):
    for key in _CLEARED_ENV_VARS:
        os.environ.pop(key, None)
    os.environ["LELAB_UI_DIR"] = str(tmp_path / "ui_dist")
    yield


class _TestPlugin(RobotPlugin):
    """A minimal plugin with switchable hooks, for exercising the §0.15 wiring."""

    def __init__(self, name, *, control_urls=(), static_ui=None, http_api=None):
        self._name = name
        self._control_urls = list(control_urls)
        self._static_ui = static_ui
        self._http_api = http_api

    def metadata(self):
        return RobotMetadata(
            name=self._name, description="test plugin", robot_type="test",
            url_prefix=self._name, fleet_provider="", fleet_domain="",
        )

    def register_tools(self, mcp):
        pass

    def tool_names(self):
        return []

    def control_base_urls(self):
        return list(self._control_urls)

    def static_ui(self):
        return self._static_ui

    def http_api(self):
        return self._http_api


def _ui(tmp_path: Path) -> StaticUi:
    d = tmp_path / "ui_dist"
    d.mkdir(exist_ok=True)
    (d / "index.html").write_text("<!doctype html><title>ui</title>")
    return StaticUi(directory=d, api_query_param="api")


def test_plugin_with_console_and_static_ui_is_rejected(tmp_path):
    plugin = _TestPlugin("both", control_urls=["http://127.0.0.1:1"], static_ui=_ui(tmp_path))
    with pytest.raises(ValueError, match="control_base_urls.*static_ui.*not both"):
        create_gateway({"both": plugin})


def test_index_reports_static_ui_endpoint(tmp_path):
    plugin = _TestPlugin("lelab", static_ui=_ui(tmp_path))
    app = create_gateway({"lelab": plugin})
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        body = client.get("/").json()
    assert body["robots"]["lelab"]["ui_endpoint"] == "/lelab/ui/"


def test_console_path_redirects_for_static_ui_robot(tmp_path):
    plugin = _TestPlugin("lelab", static_ui=_ui(tmp_path))
    app = create_gateway({"lelab": plugin})
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        res = client.get("/lelab/ui", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"] == "/lelab/ui/"


def test_existing_console_unchanged():
    from yakrobot_cli.commands import _load_plugins

    app = create_gateway(_load_plugins(["fakerobot_picar"]))
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        res = client.get("/fakerobot_picar/ui")
    assert res.status_code == 200
    assert "console" in res.text.lower() or "driv" in res.text.lower()


# -- §0.16 operator session (step 7.3) -----------------------------------------


def _ui_app(tmp_path: Path, *, tokens="op=tok", index=True, robots=("lelab",)):
    """A gateway with one or more static-UI robots, an MCP_TOKENS env, and a bundle dir."""
    from starlette.testclient import TestClient

    plugins = {}
    for i, robot in enumerate(robots):
        ui_dir = tmp_path / f"ui_dist_{i}"
        ui_dir.mkdir(exist_ok=True)
        if index:
            (ui_dir / "index.html").write_text(f"<!doctype html><title>{robot} ui</title>")
            (ui_dir / "assets").mkdir(exist_ok=True)
            (ui_dir / "assets" / "app.js").write_text("console.log('hi')")
        plugins[robot] = _TestPlugin(robot, static_ui=StaticUi(directory=ui_dir, api_query_param="api"))
    os.environ["MCP_TOKENS"] = tokens
    # Loopback base URL so `_public_origin` derives "http://…" and the session cookie is
    # not marked `Secure` (httpx won't send a Secure cookie over http).
    return TestClient(create_gateway(plugins), base_url="http://127.0.0.1")


def test_ui_root_without_cookie_shows_login(tmp_path):
    with _ui_app(tmp_path) as client:
        res = client.get("/lelab/ui/")
    assert res.status_code == 200
    assert "login" in res.text.lower()


def test_login_rejects_unknown_token(tmp_path):
    with _ui_app(tmp_path) as client:
        res = client.post("/lelab/ui/login", data={"token": "wrong"}, follow_redirects=False)
    assert res.status_code == 401
    assert "unknown token" in res.text


def test_login_sets_scoped_httponly_cookie_and_redirects_with_api(tmp_path):
    with _ui_app(tmp_path) as client:
        res = client.post("/lelab/ui/login", data={"token": "tok"}, follow_redirects=False)
    assert res.status_code == 303
    location = res.headers["location"]
    assert location.startswith("/lelab/ui/?api=")
    assert "lelab" in location and "api" in location
    set_cookie = res.headers["set-cookie"]
    assert "yk_ui=" in set_cookie
    assert "Path=/lelab/" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_ui_root_with_cookie_serves_index(tmp_path):
    with _ui_app(tmp_path) as client:
        client.post("/lelab/ui/login", data={"token": "tok"})
        res = client.get("/lelab/ui/")
    assert res.status_code == 200
    assert "lelab ui" in res.text.lower()


def test_spa_route_serves_index(tmp_path):
    with _ui_app(tmp_path) as client:
        client.post("/lelab/ui/login", data={"token": "tok"})
        res = client.get("/lelab/ui/recording")
    assert res.status_code == 200
    assert "lelab ui" in res.text.lower()


def test_asset_served_without_cookie(tmp_path):
    with _ui_app(tmp_path) as client:
        res = client.get("/lelab/ui/assets/app.js")
    assert res.status_code == 200
    assert "console.log" in res.text


def test_path_traversal_refused(tmp_path):
    with _ui_app(tmp_path) as client:
        assert client.get("/lelab/ui/../../pyproject.toml").status_code == 404
        assert client.get("/lelab/ui/%2e%2e/%2e%2e/pyproject.toml").status_code == 404


def test_revoked_token_ends_session(tmp_path):
    token_file = tmp_path / "tokens.txt"
    token_file.write_text("op=tok\n")
    os.environ.pop("MCP_TOKENS", None)
    os.environ["MCP_TOKENS_FILE"] = str(token_file)
    try:
        with _ui_app(tmp_path, tokens="") as client:
            # _ui_app sets MCP_TOKENS="", which is fine — the file takes precedence.
            client.post("/lelab/ui/login", data={"token": "tok"})
            assert "lelab ui" in client.get("/lelab/ui/").text.lower()
            # Revoke: drop the "op" client entirely and touch the mtime so any cached
            # reader notices.
            token_file.write_text("other=other-token\n")
            os.utime(token_file, None)
            res = client.get("/lelab/ui/")
            assert "login" in res.text.lower()
    finally:
        os.environ.pop("MCP_TOKENS_FILE", None)


def test_forged_cookie_refused(tmp_path):
    with _ui_app(tmp_path) as client:
        client.post("/lelab/ui/login", data={"token": "tok"})
        # Tamper with the cookie value's signature.
        cookie = client.cookies.get("yk_ui")
        payload, _sig = cookie.split(".")
        client.cookies.set("yk_ui", payload + ".AAAA", path="/lelab/")
        res = client.get("/lelab/ui/")
    assert "login" in res.text.lower()


def test_cookie_for_other_robot_refused(tmp_path):
    with _ui_app(tmp_path, robots=("lelab", "lelab2")) as client:
        client.post("/lelab/ui/login", data={"token": "tok"})
        res = client.get("/lelab2/ui/")
    assert "login" in res.text.lower()


def test_ui_not_built_page(tmp_path):
    with _ui_app(tmp_path, index=False) as client:
        res = client.get("/lelab/ui/")
    assert res.status_code == 503
    assert "UI not built" in res.text


# -- §0.17 relay (step 7.4) ----------------------------------------------------


# Mirrors plugins/lelab_so101's ROUTES (§0.18): first match wins, anything unmatched
# is refused. Refused by omission: every DELETE/PUT, POST to system/…, hf-auth/login,
# jobs/training, jobs/import and delete-dataset.
_RELAY_ROUTES = (
    ApiRoute("POST", r"stop-teleoperation|stop-recording|recording-exit-early|stop-calibration|stop-inference|jobs/[^/]+/stop", "stop"),
    ApiRoute("POST", r"move-arm|start-recording|recording-rerecord-episode|start-calibration|complete-calibration-step|start-inference|start-port-detection|detect-port-after-disconnect", "control"),
    ApiRoute("POST", r"save-robot-port|save-robot-config|upload-dataset|dataset-info|robots/[^/]+", "config"),
    ApiRoute("WS", r"ws/joint-data", "read"),
    ApiRoute("GET", r".*", "read"),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def lelab_server():
    """Run the fake LeLab backend on a loopback port in a background thread."""
    import threading

    import uvicorn
    from tools.fake_lelab import LeLabState, create_fake_lelab

    state = LeLabState()
    app = create_fake_lelab(state)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield state, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _relay_app(tmp_path: Path, base_url: str, *, tokens="op=tok"):
    from starlette.testclient import TestClient

    ui_dir = tmp_path / "ui_dist"
    ui_dir.mkdir(exist_ok=True)
    (ui_dir / "index.html").write_text("<!doctype html><title>lelab</title>")
    plugin = _TestPlugin(
        "lelab",
        static_ui=StaticUi(directory=ui_dir, api_query_param="api"),
        http_api=HttpApi(base_url=base_url, routes=_RELAY_ROUTES),
    )
    os.environ["MCP_TOKENS"] = tokens
    return TestClient(create_gateway({"lelab": plugin}), base_url="http://127.0.0.1")


def _login(client) -> None:
    client.post("/lelab/ui/login", data={"token": "tok"})


def _saw(state, method: str, path: str) -> bool:
    return any(m == method and p == path for m, p, _b in state.calls)


def test_relay_requires_session(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        res = client.get("/lelab/api/teleoperation-status")
    assert res.status_code == 401
    assert res.json()["detail"] == "operator login required"


def test_get_status_relayed(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        res = client.get("/lelab/api/teleoperation-status")
    assert res.status_code == 200
    assert _saw(state, "GET", "/teleoperation-status")


def test_unlisted_routes_refused(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        assert client.post("/lelab/api/system/update", json={}).status_code == 403
        assert client.post("/lelab/api/hf-auth/login", json={}).status_code == 403
        assert client.delete("/lelab/api/jobs/x").status_code in (403, 404, 405)
    assert not _saw(state, "POST", "/system/update")
    assert not _saw(state, "POST", "/hf-auth/login")
    assert not any(m == "DELETE" for m, _p, _b in state.calls)


def test_cross_origin_post_refused(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        res = client.post(
            "/lelab/api/move-arm",
            json={"joints": []},
            headers={"origin": "http://evil.example"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "cross-origin request refused"
    assert not _saw(state, "POST", "/move-arm")


def test_control_route_reserves_for_operator(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        res = client.post("/lelab/api/move-arm", json={"joints": []})
        assert res.status_code == 200
        index = client.get("/").json()
    assert index["robots"]["lelab"]["reservation"]["holder"] == "op"


def test_control_route_409_when_agent_holds_robot(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url, tokens="op=tok,other=tok2") as client:
        # "other" reserves via a control call.
        client.post("/lelab/ui/login", data={"token": "tok2"})
        assert client.post("/lelab/api/move-arm", json={}).status_code == 200
        # "op" is refused.
        client.post("/lelab/ui/login", data={"token": "tok"})
        res = client.post("/lelab/api/move-arm", json={})
    assert res.status_code == 409
    assert "held by other" in res.json()["detail"]


def test_stop_route_passes_when_robot_held_by_other(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url, tokens="op=tok,other=tok2") as client:
        client.post("/lelab/ui/login", data={"token": "tok2"})
        assert client.post("/lelab/api/move-arm", json={}).status_code == 200
        client.post("/lelab/ui/login", data={"token": "tok"})
        res = client.post("/lelab/api/stop-recording", json={})
    assert res.status_code == 200
    assert _saw(state, "POST", "/stop-recording")


def test_read_renews_only_own_reservation(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url, tokens="op=tok,other=tok2") as client:
        client.post("/lelab/ui/login", data={"token": "tok"})
        assert client.post("/lelab/api/move-arm", json={}).status_code == 200
        # Own read renews; another client's read is still served but never acquires.
        assert client.get("/lelab/api/teleoperation-status").status_code == 200
        client.post("/lelab/ui/login", data={"token": "tok2"})
        assert client.get("/lelab/api/teleoperation-status").status_code == 200
        index = client.get("/").json()
    assert index["robots"]["lelab"]["reservation"]["holder"] == "op"


def test_camera_feed_streams_and_closes_on_disconnect(tmp_path, lelab_server):
    import threading

    import httpx
    import uvicorn

    state, base_url = lelab_server
    ui_dir = tmp_path / "ui_dist"
    ui_dir.mkdir(exist_ok=True)
    (ui_dir / "index.html").write_text("<!doctype html><title>lelab</title>")
    plugin = _TestPlugin(
        "lelab",
        static_ui=StaticUi(directory=ui_dir, api_query_param="api"),
        http_api=HttpApi(base_url=base_url, routes=_RELAY_ROUTES),
    )
    os.environ["MCP_TOKENS"] = "op=tok"
    app = create_gateway({"lelab": plugin})
    # Run the gateway as a real server so a client disconnect reaches uvicorn and
    # cancels the relay's streaming response (an in-process client cannot tear down an
    # endless MJPEG stream cleanly).
    port = _free_port()
    gw = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=gw.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not gw.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as hc:
            hc.post("/lelab/ui/login", data={"token": "tok"})
            with hc.stream("GET", "/lelab/api/camera-feed/front") as r:
                assert r.status_code == 200
                it = r.iter_raw()
                for _ in range(3):
                    next(it)
        # The client (and its stream) closed — the gateway must now close the upstream.
    finally:
        gw.should_exit = True
        thread.join(timeout=5)
    deadline = time.time() + 3
    while time.time() < deadline and not _saw(state, "DISCONNECT", "/camera-feed/front"):
        time.sleep(0.05)
    assert _saw(state, "DISCONNECT", "/camera-feed/front")


def test_range_request_returns_206(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        res = client.get("/lelab/api/dataset-video", headers={"range": "bytes=0-99"})
    assert res.status_code == 206
    assert "content-range" in {k.lower() for k in res.headers}


def test_request_body_limit_413(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        big = "x" * (1024 * 1024 + 1)
        res = client.post("/lelab/api/start-recording", content=big)
    assert res.status_code == 413


def test_upstream_down_is_502(tmp_path):
    with _relay_app(tmp_path, "http://127.0.0.1:1") as client:
        _login(client)
        res = client.get("/lelab/api/teleoperation-status")
    assert res.status_code == 502
    assert "unreachable" in res.json()["detail"]


def test_ws_joint_data_relayed(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        cookie = client.cookies.get("yk_ui")
        with client.websocket_connect(
            "/lelab/api/ws/joint-data", headers={"cookie": f"yk_ui={cookie}"}
        ) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "joint_update"
            ws.send_text("ping")


def test_ws_refused_without_session(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        with client.websocket_connect("/lelab/api/ws/joint-data") as ws:
            # The relay accepts then closes with 1008.
            with pytest.raises(Exception):
                ws.receive_json()


def test_ws_unlisted_path_refused(tmp_path, lelab_server):
    state, base_url = lelab_server
    with _relay_app(tmp_path, base_url) as client:
        _login(client)
        cookie = client.cookies.get("yk_ui")
        with client.websocket_connect(
            "/lelab/api/ws/other", headers={"cookie": f"yk_ui={cookie}"}
        ) as ws:
            with pytest.raises(Exception):
                ws.receive_json()
    assert not _saw(state, "WS", "/ws/other")

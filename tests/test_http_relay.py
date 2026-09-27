"""Tests for the plugin UI/HTTP hooks (execution plan §0.15) and, later in this file,
the operator session and relay (steps 7.3–7.4).

The §0.15 hook tests use tiny ``RobotPlugin`` subclasses defined here so no real robot
package or its dependencies are needed.
"""

import os
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

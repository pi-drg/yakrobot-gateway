"""LeLab SO-101 — a leader/follower teleoperation arm running Hugging Face's LeLab.

This plugin fronts the LeLab backend (``uvicorn lelab.server:app --host 127.0.0.1
--port 8001``) and serves its pre-built web UI at ``/{robot}/ui/`` behind the gateway's
operator session, with its HTTP/WS API allowlist-relayed at ``/{robot}/api/*`` (§0.18).

Deliberate opt-outs: no ``control_base_urls()`` (there are no realtime sockets to
proxy — the relay below carries the UI's calls instead) and no ``dataset_features()``
(LeLab records its own datasets, so the gateway's teleop recorder is not involved).
"""

import os
from pathlib import Path

from core.plugin import ApiRoute, HttpApi, RobotMetadata, RobotPlugin, StaticUi

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_UI_DIR = _REPO_ROOT / "src" / "plugins" / "lelab_so101" / "ui_dist"


def _lelab_url() -> str:
    return os.getenv("LELAB_URL", "http://127.0.0.1:8001").rstrip("/")


def _lelab_ui_dir() -> Path:
    raw = os.getenv("LELAB_UI_DIR", "").strip()
    if not raw:
        return _DEFAULT_UI_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path


# §0.18 — first match wins; anything unmatched is refused. Refused by omission: every
# DELETE and PUT; POST to system/…, hf-auth/login, jobs/training, jobs/import and
# delete-dataset.
ROUTES = (
    ApiRoute(
        "POST",
        r"stop-teleoperation|stop-recording|recording-exit-early|stop-calibration|stop-inference|jobs/[^/]+/stop",
        "stop",
    ),
    ApiRoute(
        "POST",
        r"move-arm|start-recording|recording-rerecord-episode|start-calibration|complete-calibration-step|start-inference|start-port-detection|detect-port-after-disconnect",
        "control",
    ),
    ApiRoute(
        "POST",
        r"save-robot-port|save-robot-config|upload-dataset|dataset-info|robots/[^/]+",
        "config",
    ),
    ApiRoute("WS", r"ws/joint-data", "read"),
    ApiRoute("GET", r".*", "read"),
)


class LelabSo101Plugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            name="LeLab SO-101",
            description=(
                "A SO-101 leader/follower teleoperation arm driven by Hugging Face's "
                "LeLab: record and replay demonstrations, run inference, and manage "
                "the recorded datasets."
            ),
            robot_type="articulated_arm",
            url_prefix="lelab_so101",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
        )

    def static_ui(self) -> StaticUi:
        return StaticUi(directory=_lelab_ui_dir(), api_query_param="api")

    def http_api(self) -> HttpApi:
        return HttpApi(base_url=_lelab_url(), routes=ROUTES)

    def dataset_features(self):
        # LeLab records its own datasets; the gateway's teleop recorder is not involved.
        return None

    def control_base_urls(self) -> list[str]:
        # No realtime sockets to proxy — the UI's calls go through http_api() instead.
        return []

    def tool_names(self) -> list[str]:
        from core.robot_marketplace_tools import MARKETPLACE_TOOL_NAMES

        return [
            "lelab_status",
            "lelab_joint_positions",
            "lelab_list_datasets",
            "lelab_available_cameras",
            "lelab_start_recording",
            "lelab_stop_recording",
            "lelab_exit_episode_early",
            "lelab_start_inference",
            "lelab_stop_inference",
            "lelab_upload_dataset",
            *MARKETPLACE_TOOL_NAMES,
        ]

    def register_tools(self, mcp):
        from .mcp_tools import register
        from .robot_adapter import LelabAdapter

        self.adapter = LelabAdapter()
        register(mcp, self.adapter)

"""Read and validate recording and dataset-delivery configuration from the env.

Configured per gateway, by its operator, in ``.env`` — see AGENTS.md and
2026-09-25-teleop-datasets-execution.md §0.1, the authoritative source for every name,
default and validation rule here.

Recording is **off by default** and is switched on per gateway with
``RECORDING_ENABLED``. When on, admitted ``/ws/control`` and ``/ws/video`` sockets on a
recordable robot are tapped to a staging directory on disk; the console and the index
disclose it. Off is exactly today's behaviour: no tap, no files.

Dataset delivery credentials (``HF_TOKEN`` and the ``R2_*`` variables) are read lazily.
They are only ever *required* once a listing in ``DATASETS_FILE`` actually uses that
store (§0.6), so a gateway that lists nothing can leave them all unset. This module
parses and validates what is present and nothing more: ``load_r2_config`` returns
``None`` when no ``R2_*`` variable is set, and refuses a *partial* set (all four must be
present together). ``datasets_listing`` enforces the per-store requirement at load time.

Read at call time (the ``video_enabled()`` pattern in ``core.ws_proxy``), never cached at
import, so tests and a running gateway both see live env changes.
"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}

_DEFAULT_RECORDINGS_DIR = "~/.cache/yakrobot/recordings"
_DEFAULT_RECORDINGS_MAX_GB = "20"
_DEFAULT_RECORDING_QUEUE_FRAMES = "256"
_DEFAULT_REDEEM_DAYS = "30"
_DEFAULT_R2_URL_TTL_S = "3600"

# RECORDINGS_MAX_GB and RECORDING_QUEUE_FRAMES are only read (and validated) when
# recording is on, mirroring load_payments_config: off is off, and a gateway that has
# never enabled recording should not refuse to boot over a stale value in .env.
_QUEUE_FRAMES_MIN = 16
_QUEUE_FRAMES_MAX = 65536
_REDEEM_DAYS_MIN = 1
_REDEEM_DAYS_MAX = 365
_R2_TTL_MIN = 60
_R2_TTL_MAX = 604800


class DatasetsConfigError(ValueError):
    """A recording or dataset-delivery configuration variable is missing or invalid."""


@dataclass(frozen=True)
class RecordingConfig:
    enabled: bool
    dir: Path
    max_bytes: int
    queue_frames: int
    disabled_reason: str | None = None


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    url_ttl_s: int = 3600
    endpoint: str = ""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def load_recording_config() -> RecordingConfig:
    """Read ``RECORDING_*`` and fold in ``VIDEO_ENABLED``.

    ``VIDEO_ENABLED`` off is not a startup error: recording is disabled with
    ``disabled_reason = "video disabled"``, because a dataset of commands with no images
    is worse than none (design §2.1). ``max_bytes`` is always populated (the default 20
    GB when recording is off), so callers can read the cap without re-deriving it.
    """
    enabled = _truthy("RECORDING_ENABLED")

    raw_dir = os.getenv("RECORDINGS_DIR", _DEFAULT_RECORDINGS_DIR).strip()
    directory = Path(os.path.expanduser(raw_dir or _DEFAULT_RECORDINGS_DIR))

    max_gb = float(_DEFAULT_RECORDINGS_MAX_GB)
    queue_frames = int(_DEFAULT_RECORDING_QUEUE_FRAMES)
    disabled_reason: str | None = None

    if enabled:
        raw_max = os.getenv("RECORDINGS_MAX_GB", _DEFAULT_RECORDINGS_MAX_GB).strip()
        try:
            max_gb = float(raw_max)
        except ValueError:
            raise DatasetsConfigError(
                "RECORDINGS_MAX_GB must be a positive number of GB"
            ) from None
        if not math.isfinite(max_gb) or max_gb <= 0:
            raise DatasetsConfigError("RECORDINGS_MAX_GB must be a positive number of GB")

        raw_frames = os.getenv(
            "RECORDING_QUEUE_FRAMES", _DEFAULT_RECORDING_QUEUE_FRAMES
        ).strip()
        if not raw_frames.isdigit() or not (
            _QUEUE_FRAMES_MIN <= int(raw_frames) <= _QUEUE_FRAMES_MAX
        ):
            raise DatasetsConfigError(
                f"RECORDING_QUEUE_FRAMES must be an integer from "
                f"{_QUEUE_FRAMES_MIN} to {_QUEUE_FRAMES_MAX}"
            )
        queue_frames = int(raw_frames)

        from core.ws_proxy import video_enabled

        if not video_enabled():
            disabled_reason = "video disabled"
            enabled = False

    return RecordingConfig(
        enabled=enabled,
        dir=directory,
        max_bytes=int(max_gb * (1024 ** 3)),
        queue_frames=queue_frames,
        disabled_reason=disabled_reason,
    )


def load_redeem_days() -> int:
    """``DATASET_REDEEM_DAYS``, validated, defaulted — the v2 capability window (§0.10).

    Always validated, like ``TELEOP_LEASE_MINUTES``: the index reports it on every
    gateway whether or not any dataset is listed.
    """
    raw = os.getenv("DATASET_REDEEM_DAYS", _DEFAULT_REDEEM_DAYS).strip()
    if not raw.isdigit() or not (_REDEEM_DAYS_MIN <= int(raw) <= _REDEEM_DAYS_MAX):
        raise DatasetsConfigError(
            f"DATASET_REDEEM_DAYS must be an integer from {_REDEEM_DAYS_MIN} to "
            f"{_REDEEM_DAYS_MAX}"
        )
    return int(raw)


def hf_token() -> str | None:
    """``HF_TOKEN``, or ``None``. Never validated here — required only for ``hf``
    listings, which is ``datasets_listing``'s job at load time (§0.6)."""
    return os.getenv("HF_TOKEN", "").strip() or None


_R2_VARS = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def load_r2_config() -> R2Config | None:
    """Read the ``R2_*`` variables, or return ``None`` when none are set.

    All four credential variables are required together (a partial set is an error, so a
    half-configured bucket can't presign from a missing secret); the two knobs default.
    ``R2_ENDPOINT`` defaults to Cloudflare's account host and is only slash-trimmed, so a
    test or local fake (``http://127.0.0.1:…``) can point it anywhere (§0.8).
    """
    values = {name: os.getenv(name, "").strip() for name in _R2_VARS}
    if not any(values.values()):
        return None
    missing = [name for name in _R2_VARS if not values[name]]
    if missing:
        raise DatasetsConfigError(
            "R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY "
            f"must all be set together (missing: {', '.join(missing)})"
        )

    raw_ttl = os.getenv("R2_URL_TTL_S", _DEFAULT_R2_URL_TTL_S).strip()
    if not raw_ttl.isdigit() or not (_R2_TTL_MIN <= int(raw_ttl) <= _R2_TTL_MAX):
        raise DatasetsConfigError(
            f"R2_URL_TTL_S must be an integer from {_R2_TTL_MIN} to {_R2_TTL_MAX}"
        )
    url_ttl_s = int(raw_ttl)

    endpoint = os.getenv("R2_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = f"https://{values['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    endpoint = endpoint.rstrip("/")

    return R2Config(
        account_id=values["R2_ACCOUNT_ID"],
        bucket=values["R2_BUCKET"],
        access_key_id=values["R2_ACCESS_KEY_ID"],
        secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        url_ttl_s=url_ttl_s,
        endpoint=endpoint,
    )

"""Robot-facing adapter for LeLab (§0.18)."""

import os

import httpx


class LelabAdapter:
    """An ``httpx.AsyncClient`` against the LeLab backend, exposing ``get``/``post`` and
    the ``saved_rig()`` port/calibration lookup the recording and inference tools need."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("LELAB_URL", "http://127.0.0.1:8001")).rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)

    async def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail") if isinstance(data, dict) else data
            except Exception:
                detail = resp.text
            # FastAPI's `detail` field is the error surface LeLab's callers expect.
            raise RuntimeError(str(detail))
        try:
            return resp.json()
        except Exception:
            return {}

    async def get(self, path: str) -> dict:
        return await self._check(await self.client.get(path))

    async def post(self, path: str, json: dict | None = None) -> dict:
        return await self._check(await self.client.post(path, json=json))

    async def saved_rig(self) -> dict:
        """The saved leader/follower ports and calibrations, or ``RuntimeError``.

        Reads LeLab's four rig endpoints and takes each field's ``saved_*`` value,
        falling back to ``default_*`` — and fails when any of the four is missing,
        because recording against an unconfigured rig is useless.
        """
        leader_port = await self.get("/robot-port/leader")
        follower_port = await self.get("/robot-port/follower")
        leader_cfg = await self.get("/robot-config/leader")
        follower_cfg = await self.get("/robot-config/follower")

        def pick(data: dict, saved_key: str, default_key: str):
            return data.get(saved_key) or data.get(default_key)

        rig = {
            "leader_port": pick(leader_port, "saved_port", "default_port"),
            "follower_port": pick(follower_port, "saved_port", "default_port"),
            "leader_config": pick(leader_cfg, "saved_config", "default_config"),
            "follower_config": pick(follower_cfg, "saved_config", "default_config"),
        }
        if not all(rig.values()):
            raise RuntimeError(
                "LeLab has no saved leader/follower port or calibration — set them up once in the UI"
            )
        return rig

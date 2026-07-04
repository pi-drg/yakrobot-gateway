import os
import httpx


class TemplateAdapter:
    """HTTP adapter for your robot.

    Adapt this to your robot's communication protocol (HTTP, UDP, serial, etc.).
    """

    def __init__(self):
        self.base_url = os.getenv("MYROBOT_URL", "http://localhost:8080")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=5.0)

    async def get(self, path: str) -> dict:
        resp = await self.client.get(path)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": "ok", "body": resp.text}

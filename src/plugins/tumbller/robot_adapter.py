import os

import httpx


class TumbllerAdapter:
    def __init__(self):
        self.base_url = os.getenv("TUMBLLER_URL", "http://finland-tumbller-01.local")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=5.0)

    async def get(self, path: str) -> dict:
        resp = await self.client.get(path)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": "ok", "body": resp.text}

"""A local fake R2: an S3-compatible object store that verifies SigV4 signatures.

Used by tests so the publish/fetch/preview paths run against real presigned URLs with no
Cloudflare account. It stores objects in a dict and checks every incoming request with
``core.r2_presign.verify`` — the exact inverse of the presigner — so a bad signature, an
expired link, or a PUT signed with a read-only key is refused with 403.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from fastapi import FastAPI, Request, Response

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.r2_presign import verify  # noqa: E402


class FakeR2State:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}   # object key -> bytes
        self.keys: dict[str, str] = {}        # access_key -> secret_key
        self.read_only: set[str] = set()      # access keys that may not PUT
        self.put_order: list[str] = []        # object keys, in the order PUT arrived


def create_fake_r2(state: FakeR2State, *, region: str = "auto") -> FastAPI:
    app = FastAPI()

    def _refuse(request: Request, method: str) -> Response | None:
        params = dict(parse_qsl(request.url.query, keep_blank_values=True))
        credential = params.get("X-Amz-Credential", "")
        access_key = credential.split("/", 1)[0] if "/" in credential else credential
        secret = state.keys.get(access_key)
        if secret is None:
            return Response(status_code=403, content="unknown access key")

        host_header = request.headers.get("host", "")
        host = (urlparse(f"//{host_header}").hostname or host_header).lower()
        if not verify(method, host, request.url.path, params, secret, region):
            return Response(status_code=403, content="bad signature")

        try:
            signed = datetime.strptime(params["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            expires = int(params["X-Amz-Expires"])
        except (KeyError, ValueError):
            return Response(status_code=403, content="bad X-Amz-Date/Expires")
        if (datetime.now(timezone.utc) - signed).total_seconds() > expires:
            return Response(status_code=403, content="expired link")

        if method == "PUT" and access_key in state.read_only:
            return Response(status_code=403, content="read-only key")
        return None

    @app.put("/{bucket}/{key:path}")
    async def put(bucket: str, key: str, request: Request):
        refused = _refuse(request, "PUT")
        if refused is not None:
            return refused
        state.objects[key] = await request.body()
        state.put_order.append(key)
        return Response(status_code=200)

    @app.get("/{bucket}/{key:path}")
    async def get(bucket: str, key: str, request: Request):
        refused = _refuse(request, "GET")
        if refused is not None:
            return refused
        obj = state.objects.get(key)
        if obj is None:
            return Response(status_code=404)
        return Response(content=obj, media_type="application/octet-stream")

    return app

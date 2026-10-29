"""A local fake JSON-RPC node answering ``CaptureFlag.lastCapture`` — for tests only.

``create_fake_rpc(state)`` returns the app. Set ``state.captures[(robot_id, player)] =
t`` (both lowercase) to say a capture happened at unix time ``t``; anything not set is
0, as on-chain. ``state.mode`` breaks it on purpose: ``"http500"``, ``"rpc_error"`` or
``"garbage"``. For "down", point the gateway at a port nothing listens on.
"""

from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SELECTOR = "0xaa6c2762"  # lastCapture(bytes32,address), from `cast sig`


@dataclass
class RpcState:
    contract: str
    captures: dict[tuple[str, str], int] = field(default_factory=dict)
    mode: str = "ok"
    calls: int = 0


def create_fake_rpc(state: RpcState) -> FastAPI:
    app = FastAPI()

    @app.post("/")
    async def rpc(request: Request):
        state.calls += 1
        if state.mode == "http500":
            return JSONResponse(status_code=500, content={"error": "boom"})
        body = await request.json()
        if state.mode == "rpc_error":
            return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "nope"}}
        if state.mode == "garbage":
            return {"jsonrpc": "2.0", "id": body["id"], "result": "0x"}
        assert body["method"] == "eth_call", body
        call = body["params"][0]
        assert call["to"].lower() == state.contract.lower(), call
        data = call["data"]
        assert data[:10] == SELECTOR, data
        robot_id = "0x" + data[10:74]
        player = "0x" + data[74 + 24 : 138]
        t = state.captures.get((robot_id, player), 0)
        return {"jsonrpc": "2.0", "id": body["id"], "result": "0x" + t.to_bytes(32, "big").hex()}

    return app

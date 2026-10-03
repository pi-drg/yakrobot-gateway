"""A local fake Razorpay, for tests and hands-on smoke runs — never a real Razorpay.

The gateway talks to Razorpay over exactly three REST endpoints (``POST /v1/orders``,
``GET /v1/orders/{id}``, ``GET /v1/payments/{id}``), all HTTP Basic with
``key_id:key_secret``. This module fakes them, plus a stub ``checkout.js`` and a fake
"buyer pays" page, so the whole UPI gate can run with no account, no key and no network.

Two ways to use it:

* **In tests** — ``create_fake_razorpay(state)`` returns the FastAPI app. Tests call
  ``pay(state, order_id, ...)`` to create a payment (captured by default, or
  authorized / wrong amount / old ``created``) and get back the three form fields
  Checkout would POST to the gateway's confirm.

* **By hand** — ``uv run python tests/tools/fake_razorpay.py --port 8194`` serves it on
  loopback. ``POST /__fake/pay`` (``order_id``, optional ``method``/``status``) makes a
  payment and returns the three fields as JSON, for the curl smoke test. In a browser,
  the stub ``checkout.js`` navigates to ``/__fake/checkout``, which "pays" and
  auto-POSTs the fields to the gateway's ``callback_url``.
"""

import argparse
import base64
import hashlib
import hmac
import html
import json
import random
import string
import time
from collections import Counter
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

_ALNUM = string.ascii_letters + string.digits


def _rand_id(prefix: str) -> str:
    return prefix + "".join(random.choices(_ALNUM, k=14))


@dataclass
class RazorpayState:
    """Everything the fake records, for tests to assert on and to pre-seed entities."""

    key_id: str = "rzp_test_fake"
    secret: str = "fakesecret"
    orders: dict[str, dict] = field(default_factory=dict)
    payments: dict[str, dict] = field(default_factory=dict)
    created_bodies: list[dict] = field(default_factory=list)
    fetch_calls: Counter = field(default_factory=Counter)  # keyed by request path
    _counter: int = field(default=0, repr=False)


def signature(secret: str, order_id: str, payment_id: str) -> str:
    """The hex HMAC Checkout returns: key = secret, message = ``order_id|payment_id``."""
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def pay(
    state: RazorpayState,
    order_id: str,
    *,
    method: str = "upi",
    status: str = "captured",
    amount: int | None = None,
    created: int | None = None,
    public_base: str | None = None,  # accepted for symmetry with the stub checkout page
) -> dict:
    """Create a payment on ``order_id``; return the three fields Checkout would POST."""
    order = state.orders[order_id]
    amt = order["amount"] if amount is None else amount
    pid = _rand_id("pay_")
    captured = status == "captured"
    payment = {
        "id": pid,
        "entity": "payment",
        "amount": amt,
        "currency": "INR",
        "status": status,
        "order_id": order_id,
        "method": method,
        "captured": captured,
        "created_at": created if created is not None else int(time.time()),
        "international": False,
    }
    if method == "upi":
        payment["vpa"] = "test@upi"
    state.payments[pid] = payment

    order["attempts"] += 1
    if captured:
        order["status"] = "paid"
        order["amount_paid"] = amt
        order["amount_due"] = 0
    else:
        order["status"] = "attempted"

    return {
        "razorpay_payment_id": pid,
        "razorpay_order_id": order_id,
        "razorpay_signature": signature(state.secret, order_id, pid),
    }


_ERR_UNKNOWN = {
    "error": {
        "code": "BAD_REQUEST_ERROR",
        "description": "The id provided does not exist",
    }
}
_ERR_AUTH = {
    "error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}
}


def _authorized(request: Request, state: RazorpayState) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    return decoded == f"{state.key_id}:{state.secret}"


def create_fake_razorpay(state: RazorpayState) -> FastAPI:
    """Build a FastAPI app that fakes the Razorpay endpoints the gateway uses."""

    app = FastAPI(title="fake-razorpay")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if request.url.path.startswith("/v1/") and request.url.path != "/v1/checkout.js":
            if not _authorized(request, state):
                return JSONResponse(_ERR_AUTH, status_code=401)
        return await call_next(request)

    @app.post("/v1/orders")
    async def create_order(request: Request):
        body = await request.json()
        state.created_bodies.append(body)
        oid = _rand_id("order_")
        order = {
            "id": oid,
            "entity": "order",
            "amount": body.get("amount"),
            "amount_paid": 0,
            "amount_due": body.get("amount"),
            "currency": body.get("currency"),
            "receipt": body.get("receipt"),
            "status": "created",
            "attempts": 0,
            "notes": body.get("notes", {}),
            "created_at": int(time.time()),
        }
        state.orders[oid] = order
        return order

    @app.get("/v1/orders/{oid}")
    async def fetch_order(oid: str, request: Request):
        state.fetch_calls[request.url.path] += 1
        order = state.orders.get(oid)
        if order is None:
            return JSONResponse(_ERR_UNKNOWN, status_code=400)
        return order

    @app.get("/v1/payments/{pid}")
    async def fetch_payment(pid: str, request: Request):
        state.fetch_calls[request.url.path] += 1
        payment = state.payments.get(pid)
        if payment is None:
            return JSONResponse(_ERR_UNKNOWN, status_code=400)
        return payment

    @app.get("/v1/checkout.js")
    async def checkout_js(request: Request):
        base = str(request.base_url).rstrip("/")
        js = (
            "window.Razorpay = function (opts) {\n"
            "  this.open = function () {\n"
            f"    location.href = {json.dumps(base)} + '/__fake/checkout?order_id=' +\n"
            "      encodeURIComponent(opts.order_id) + '&callback_url=' +\n"
            "      encodeURIComponent(opts.callback_url);\n"
            "  };\n"
            "};\n"
        )
        return Response(js, media_type="application/javascript")

    @app.get("/__fake/checkout")
    async def fake_checkout(order_id: str, callback_url: str):
        if order_id not in state.orders:
            return JSONResponse(_ERR_UNKNOWN, status_code=400)
        fields = pay(state, order_id)
        inputs = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
            for k, v in fields.items()
        )
        return HTMLResponse(
            f'<!doctype html><form id="f" method="post" '
            f'action="{html.escape(callback_url, quote=True)}">{inputs}</form>'
            '<script>document.getElementById("f").submit()</script>'
        )

    @app.post("/__fake/pay")
    async def fake_pay(request: Request):
        form = await request.form()
        get = lambda k, d=None: form.get(k) or request.query_params.get(k) or d  # noqa: E731
        oid = get("order_id")
        if oid not in state.orders:
            return JSONResponse(_ERR_UNKNOWN, status_code=400)
        return pay(
            state,
            oid,
            method=get("method", "upi"),
            status=get("status", "captured"),
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8194, help="Port to serve on (default: 8194)")
    parser.add_argument("--key-id", default="rzp_test_local")
    parser.add_argument("--secret", default="localsecret")
    args = parser.parse_args()

    state = RazorpayState(key_id=args.key_id, secret=args.secret)
    app = create_fake_razorpay(state)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

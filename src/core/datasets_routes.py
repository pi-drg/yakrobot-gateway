"""Dataset routes: preview, redeem, and the card-sale Stripe endpoints.

Registered in ``create_gateway`` **before** the mounts. Every route here serves any robot
with a listing — it must not filter plugins the way ``register_console`` and
``register_ws_proxy`` do, because a listing's robot may have no control sockets at all
(``lelab_so101``). The preview never returns dataset bytes: it is a redirect to the Hub
card or to a presigned R2 object, so the gateway's server never holds or proxies the
dataset itself (execution plan §0.11).
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from core.r2_presign import presign


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _preview_file(file: str) -> str | None:
    if file in ("README.md", "thumb_0.jpg", "thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"):
        return file
    return None


def register_dataset_routes(app: FastAPI, plugins, listings, r2, payments, stripe, redeem_days) -> None:
    # Preview only for now; redeem and the Stripe sale routes are added in later steps.

    @app.get("/{robot}/datasets/{dataset_id}/preview")
    @app.get("/{robot}/datasets/{dataset_id}/preview/{file}")
    def preview(robot: str, dataset_id: str, file: str = "README.md"):
        listing = listings.get(robot, dataset_id)
        if listing is None:
            return _error(404, "no_such_dataset", "no such dataset")
        if _preview_file(file) is None:
            return _error(404, "no_such_file", "no such preview file")

        if listing.store == "hf":
            return RedirectResponse(
                f"https://huggingface.co/datasets/{listing.hub_repo}", status_code=302
            )
        if r2 is None:
            return _error(404, "store_unavailable", "r2 is not configured")

        url = presign(
            "GET", r2.endpoint, r2.bucket,
            f"datasets/{dataset_id}/{listing.rev}/preview/{file}",
            access_key=r2.access_key_id, secret_key=r2.secret_access_key,
            expires=300, now=datetime.now(timezone.utc),
        )
        return RedirectResponse(url, status_code=302)

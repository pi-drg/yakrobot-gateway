"""Dataset routes: preview, the card-sale Stripe endpoints, and (later) redeem.

Registered in ``create_gateway`` **before** the mounts. Every route here serves any robot
with a listing — it must not filter plugins the way ``register_console`` and
``register_ws_proxy`` do, because a listing's robot may have no control sockets at all
(``lelab_so101``). The preview and the R2 delivery never return dataset bytes through the
gateway: they hand out redirects to the Hub card or to presigned R2 objects, so the
gateway's server never holds or proxies the dataset itself (execution plan §0.11, §0.12).
"""

import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.capability import normalize_host
from core.r2_presign import presign

logger = logging.getLogger(__name__)

# The username half of an hf recipient (without the "hf:" prefix), per §0.10.
_HF_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,95}")

# Parsed R2 manifests, keyed by rev — immutable per rev, so cached for the process.
_manifest_cache: dict[str, dict] = {}

# Confirmed dataset purchases, keyed by Checkout Session id, for revisit re-issue.
_confirm_cache: dict[str, dict] = {}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _preview_file(file: str) -> str | None:
    if file in ("README.md", "thumb_0.jpg", "thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"):
        return file
    return None


def _fetch_manifest(r2, dataset_id: str, rev: str) -> dict:
    """Fetch and cache ``manifest.json``, checking its sha256 **is** ``rev`` (§0.11)."""
    if rev in _manifest_cache:
        return _manifest_cache[rev]
    import httpx

    url = presign(
        "GET", r2.endpoint, r2.bucket, f"datasets/{dataset_id}/{rev}/manifest.json",
        access_key=r2.access_key_id, secret_key=r2.secret_access_key,
        expires=300, now=datetime.now(timezone.utc),
    )
    resp = httpx.get(url)
    if resp.status_code != 200:
        raise RuntimeError("store_unavailable")
    if hashlib.sha256(resp.content).hexdigest() != rev:
        raise RuntimeError("manifest hash mismatch")
    manifest = json.loads(resp.content)
    _manifest_cache[rev] = manifest
    return manifest


def _r2_links(r2, dataset_id: str, rev: str) -> dict:
    """Presign every file in the manifest; the §0.11 r2 success body."""
    manifest = _fetch_manifest(r2, dataset_id, rev)
    now = datetime.now(timezone.utc)
    expires = 3600
    files = []
    for entry in manifest["files"]:
        url = presign(
            "GET", r2.endpoint, r2.bucket,
            f"datasets/{dataset_id}/{rev}/{entry['path']}",
            access_key=r2.access_key_id, secret_key=r2.secret_access_key,
            expires=expires, now=now,
        )
        files.append({
            "path": entry["path"], "bytes": entry["bytes"],
            "sha256": entry["sha256"], "url": url,
        })
    return {
        "store": "r2",
        "manifest_url": presign(
            "GET", r2.endpoint, r2.bucket, f"datasets/{dataset_id}/{rev}/manifest.json",
            access_key=r2.access_key_id, secret_key=r2.secret_access_key,
            expires=expires, now=now,
        ),
        "files": files,
        "expires_at": int(now.timestamp()) + expires,
    }


async def _default_hf_grant(repo_id: str, user: str) -> None:
    import asyncio
    import os

    from huggingface_hub import HfApi, HfHubHTTPError

    api = HfApi(token=os.getenv("HF_TOKEN"))
    try:
        await asyncio.to_thread(api.grant_access, repo_id, user, repo_type="dataset")
    except HfHubHTTPError:
        accepted = await asyncio.to_thread(
            api.list_accepted_access_requests, repo_id, repo_type="dataset"
        )
        users = {u.get("user") if isinstance(u, dict) else u for u in accepted}
        if user not in users:
            raise


def register_dataset_routes(app: FastAPI, plugins, listings, r2, payments, stripe, redeem_days) -> None:
    from core.ws_proxy import (
        STRIPE_TIMEOUT_S,
        _SESSION_ID_RE,
        _public_domain,
        _public_origin,
        _stripe_headers,
        _stripe_page,
    )

    app.state.hf_grant = _default_hf_grant

    # -- preview ----------------------------------------------------------------

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

    # -- Stripe card sale -------------------------------------------------------

    def _hf_user_form(robot: str, dataset_id: str) -> HTMLResponse:
        action = html.escape(
            f"/{quote(robot, safe='')}/datasets/{quote(dataset_id, safe='')}/stripe/start",
            quote=True,
        )
        body = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Dataset delivery</title></head>'
            "<body>"
            "<p>Enter your Hugging Face username to receive the dataset:</p>"
            f'<form method="get" action="{action}">'
            '<input type="text" name="hf_user" required>'
            '<button type="submit">Continue</button>'
            "</form></body></html>"
        )
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/{robot}/datasets/{dataset_id}/stripe/start")
    async def dataset_stripe_start(robot: str, dataset_id: str, request: Request):
        listing = listings.get(robot, dataset_id)
        if listing is None:
            return _stripe_page(404, robot, "no such dataset")
        if not stripe.enabled or listing.price_cents is None:
            return _stripe_page(404, robot, "card purchase is not enabled for this dataset")

        recipient = "card"
        if listing.store == "hf":
            hf_user = request.query_params.get("hf_user", "").strip()
            if not _HF_USER_RE.fullmatch(hf_user):
                return _hf_user_form(robot, dataset_id)
            recipient = f"hf:{hf_user}"

        origin = _public_origin(request)
        form = {
            "mode": "payment",
            "payment_method_types[0]": "card",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": listing.currency,
            "line_items[0][price_data][unit_amount]": str(listing.price_cents),
            "line_items[0][price_data][product_data][name]": (
                f"Dataset {dataset_id} ({listing.episodes} episodes)"
            ),
            "metadata[robot]": robot,
            "metadata[gateway]": _public_domain(request),
            "metadata[dataset]": dataset_id,
            "metadata[rev]": listing.rev,
            "metadata[recipient]": recipient,
            "success_url": (
                f"{origin}/{robot}/datasets/{dataset_id}/stripe/confirm"
                "?session_id={CHECKOUT_SESSION_ID}"
            ),
            "cancel_url": f"{origin}/{robot}/ui",
        }
        if stripe.automatic_tax:
            form["automatic_tax[enabled]"] = "true"
            form["line_items[0][price_data][tax_behavior]"] = "inclusive"
            if stripe.tax_code:
                form["line_items[0][price_data][product_data][tax_code]"] = stripe.tax_code

        import httpx

        try:
            async with httpx.AsyncClient(timeout=STRIPE_TIMEOUT_S) as client:
                r = await client.post(
                    f"{stripe.api_base}/v1/checkout/sessions",
                    headers=_stripe_headers(stripe),
                    data=form,
                )
        except httpx.HTTPError:
            return _stripe_page(503, robot, "card payments are unavailable right now")
        if not 200 <= r.status_code < 300:
            return _stripe_page(503, robot, "card payments are unavailable right now")
        try:
            return RedirectResponse(r.json()["url"], status_code=303)
        except (ValueError, KeyError):
            return _stripe_page(503, robot, "card payments are unavailable right now")

    async def _deliver(listing, robot: str, dataset_id: str, recipient: str, store: str,
                       request: Request, fmt_json: bool):
        if store == "hf":
            user = recipient.split(":", 1)[1]
            try:
                await app.state.hf_grant(listing.hub_repo, user)
            except Exception as exc:  # noqa: BLE001 — HfHubHTTPError or a test stub
                return _stripe_page(502, robot, str(exc)[:200])
            return _stripe_page(
                200, robot,
                f"Dataset granted to {html.escape(user)} at "
                f"https://huggingface.co/datasets/{listing.hub_repo}",
            )
        # r2
        if r2 is None:
            return _stripe_page(404, robot, "r2 is not configured")
        try:
            links = _r2_links(r2, dataset_id, listing.rev)
        except RuntimeError as exc:
            return _stripe_page(502, robot, str(exc))
        if fmt_json:
            return JSONResponse(links)
        rows = "".join(
            f'<li><a href="{html.escape(f["url"], quote=True)}">{html.escape(f["path"])}</a> '
            f'({f["bytes"]} bytes)</li>'
            for f in links["files"]
        )
        command = (
            f"yakrobot-py dataset fetch --links '{_public_origin(request)}/{robot}/datasets/"
            f"{dataset_id}/stripe/confirm?session_id={request.query_params.get('session_id', '')}"
            f"&format=json' --out ./{dataset_id}"
        )
        body = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Dataset ready</title></head>'
            f"<body><p>Download links (expire at {links['expires_at']}):</p><ul>{rows}</ul>"
            f"<p><code>{html.escape(command)}</code></p></body></html>"
        )
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/{robot}/datasets/{dataset_id}/stripe/confirm")
    async def dataset_stripe_confirm(robot: str, dataset_id: str, request: Request):
        listing = listings.get(robot, dataset_id)
        if listing is None:
            return _stripe_page(404, robot, "no such dataset")
        if not stripe.enabled or listing.price_cents is None:
            return _stripe_page(404, robot, "card purchase is not enabled for this dataset")

        session_id = request.query_params.get("session_id", "")
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return _stripe_page(400, robot, "invalid checkout session")
        fmt_json = request.query_params.get("format") == "json"

        now = int(time.time())
        cached = _confirm_cache.get(session_id)
        if cached is not None:
            if cached["robot"] != robot or cached["dataset"] != dataset_id:
                return _stripe_page(400, robot, "session is for another dataset")
            if cached["created"] + redeem_days * 86400 < now:
                return _stripe_page(410, robot, "this purchase has expired")
            return await _deliver(
                listing, robot, dataset_id, cached["recipient"], cached["store"],
                request, fmt_json,
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=STRIPE_TIMEOUT_S) as client:
                r = await client.get(
                    f"{stripe.api_base}/v1/checkout/sessions/{session_id}",
                    headers=_stripe_headers(stripe),
                    params=[("expand[]", "payment_intent")],
                )
        except httpx.HTTPError:
            return _stripe_page(503, robot, "couldn't reach Stripe; refresh to retry")
        if r.status_code == 404:
            return _stripe_page(400, robot, "unknown checkout session")
        if r.status_code != 200:
            return _stripe_page(502, robot, "card payments are unavailable right now")
        try:
            session = r.json()
        except ValueError:
            return _stripe_page(502, robot, "card payments are unavailable right now")

        if session.get("payment_status") != "paid":
            return _stripe_page(402, robot, "payment not complete")
        metadata = session.get("metadata") or {}
        if session.get("amount_total") != listing.price_cents:
            logger.warning("dataset sale: %s/%s %s amount mismatch", robot, dataset_id, session_id[-6:])
            return _stripe_page(409, robot, "payment does not match this dataset's price")
        if session.get("currency") != listing.currency:
            logger.warning("dataset sale: %s/%s %s currency mismatch", robot, dataset_id, session_id[-6:])
            return _stripe_page(409, robot, "payment does not match this dataset's price")
        if not isinstance(metadata.get("gateway"), str) or normalize_host(
            metadata["gateway"]
        ) != normalize_host(_public_domain(request)):
            return _stripe_page(400, robot, "session is for another gateway")
        if metadata.get("robot") != robot or metadata.get("dataset") != dataset_id:
            return _stripe_page(400, robot, "session is for another dataset")
        if metadata.get("rev") != listing.rev:
            return _stripe_page(409, robot, "this dataset has been relisted")

        recipient = metadata.get("recipient") or "card"
        store = listing.store
        created = (session.get("payment_intent") or {}).get("created", now)
        _confirm_cache[session_id] = {
            "created": created, "robot": robot, "dataset": dataset_id,
            "recipient": recipient, "store": store,
        }
        logger.info("dataset sale: %s/%s %s…", robot, dataset_id, session_id[-6:])
        return await _deliver(listing, robot, dataset_id, recipient, store, request, fmt_json)

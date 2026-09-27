"""Presign (and verify) R2 (S3) object URLs with AWS Signature Version 4, query form.

R2 speaks the S3 API, so a **presigned URL** lets anyone holding it download (or, with a
write key, upload) one object and no one else, with no network call — signing is local
computation. A read-only bucket-scoped token is enough to presign GETs, so the gateway
never holds a key that can write (execution plan §0.8, design §0.5).

Stdlib only (``hmac``/``hashlib``/``urllib.parse``) — no boto3. The query form is what
the AWS docs call "Authenticating Requests: Using Query Parameters"; ``virtual_host``
exists only to reproduce AWS's published worked example (that example puts the bucket in
the host), while R2 delivery uses path-style URLs. ``verify`` is the inverse of
``presign`` and is what the local fake R2 uses to check incoming requests.
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, urlparse

_ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, msg: str | bytes) -> bytes:
    if isinstance(msg, str):
        msg = msg.encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


def _quote(value: str) -> str:
    return quote(value, safe="-_.~")


def _encode_key(key: str) -> str:
    """URI-encode each path segment, preserving the ``/`` separators (§0.8)."""
    return "/".join(_quote(seg) for seg in key.split("/"))


def _signature(
    method: str,
    canonical_uri: str,
    canonical_query: str,
    host: str,
    amz_date: str,
    scope: str,
    secret_key: str,
) -> str:
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query,
        f"host:{host}\n",
        "host",
        "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    date_stamp = scope.split("/")[0]
    region = scope.split("/")[1]
    k_date = _hmac(("AWS4" + secret_key).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    return _hmac(k_signing, string_to_sign).hex()


def presign(
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    *,
    access_key: str,
    secret_key: str,
    expires: int,
    now: datetime,
    region: str = "auto",
    virtual_host: bool = False,
) -> str:
    """Return a SigV4 query-string presigned URL for one object.

    ``method`` is ``GET`` (delivery, previews) or ``PUT`` (publish uploads). ``endpoint``
    is the bare host or full origin (trailing slash stripped by the caller); ``now`` is
    an aware datetime. Path-style URLs (the default) look like
    ``{endpoint}/{bucket}/{key}``; ``virtual_host=True`` puts the bucket in the host.
    """
    method = method.upper()
    if not isinstance(expires, int) or isinstance(expires, bool) or not (60 <= expires <= 604800):
        raise ValueError("expires must be an integer from 60 to 604800 seconds")

    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    scheme = parsed.scheme or "https"
    base_host = (parsed.hostname or endpoint).lower()
    host = f"{bucket}.{base_host}" if virtual_host else base_host

    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{now.strftime('%Y%m%d')}/{region}/s3/aws4_request"
    credential = f"{access_key}/{scope}"

    canonical_uri = (
        "/" + _encode_key(key)
        if virtual_host
        else "/" + _quote(bucket) + "/" + _encode_key(key)
    )

    params = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{_quote(k)}={_quote(v)}" for k, v in sorted(params.items())
    )
    params["X-Amz-Signature"] = _signature(
        method, canonical_uri, canonical_query, host, amz_date, scope, secret_key
    )

    query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(params.items()))
    if virtual_host:
        return f"{scheme}://{host}{canonical_uri}?{query}"
    return f"{scheme}://{base_host}{canonical_uri}?{query}"


def verify(
    method: str,
    host: str,
    path: str,
    query: dict,
    secret_key: str,
    region: str = "auto",
) -> bool:
    """Check an incoming request's query signature against the same canonical form.

    ``query`` is the decoded query-string dict; ``path`` is the request path as received
    (already percent-encoded, matching what ``presign`` produced). Returns True only when
    the signature, the scope's region, and the payload hash all line up.
    """
    signature = query.get("X-Amz-Signature")
    amz_date = query.get("X-Amz-Date")
    credential = query.get("X-Amz-Credential")
    if not signature or not amz_date or not credential:
        return False
    if "/" not in credential:
        return False
    scope = credential.split("/", 1)[1]

    canonical_params = {k: v for k, v in query.items() if k != "X-Amz-Signature"}
    canonical_query = "&".join(
        f"{_quote(k)}={_quote(v)}" for k, v in sorted(canonical_params.items())
    )
    expected = _signature(
        method, path, canonical_query, host, amz_date, scope, secret_key
    )
    return hmac.compare_digest(expected, signature)

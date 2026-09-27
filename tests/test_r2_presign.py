"""Tests for the stdlib SigV4 presigner (execution plan §0.8)."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.r2_presign import presign  # noqa: E402

_AWS_NOW = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)


def _aws_url(**overrides):
    kwargs: dict = dict(
        method="GET",
        endpoint="s3.amazonaws.com",
        bucket="examplebucket",
        key="test.txt",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        expires=86400,
        now=_AWS_NOW,
        region="us-east-1",
        virtual_host=True,
    )
    kwargs.update(overrides)
    return presign(**kwargs)


def test_aws_documented_example():
    """The worked example from AWS's 'Using Query Parameters' page (§0.8)."""
    url = _aws_url()
    params = dict(parse_qsl(urlparse(url).query))
    assert params["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert params["X-Amz-Expires"] == "86400"
    assert params["X-Amz-SignedHeaders"] == "host"
    assert params["X-Amz-Signature"] == "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"


def test_path_style_url_shape():
    url = presign(
        "GET", "https://acct.r2.cloudflarestorage.com", "bucket", "datasets/d1/rev1/manifest.json",
        access_key="k", secret_key="s", expires=3600, now=_AWS_NOW,
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "acct.r2.cloudflarestorage.com"
    assert parsed.path == "/bucket/datasets/d1/rev1/manifest.json"
    assert dict(parse_qsl(parsed.query))["X-Amz-Signature"]


def test_key_segments_encoded():
    url = presign(
        "GET", "https://acct.r2.cloudflarestorage.com", "bucket", "a b/c~d/e f",
        access_key="k", secret_key="s", expires=3600, now=_AWS_NOW,
    )
    parsed = urlparse(url)
    assert parsed.path == "/bucket/a%20b/c~d/e%20f"


def test_expires_bounds():
    with pytest.raises(ValueError):
        _aws_url(expires=59)
    with pytest.raises(ValueError):
        _aws_url(expires=604801)


def test_put_and_get_differ():
    get_url = presign(
        "GET", "https://acct.r2.cloudflarestorage.com", "bucket", "k",
        access_key="k", secret_key="s", expires=3600, now=_AWS_NOW,
    )
    put_url = presign(
        "PUT", "https://acct.r2.cloudflarestorage.com", "bucket", "k",
        access_key="k", secret_key="s", expires=3600, now=_AWS_NOW,
    )
    assert get_url != put_url
    get_sig = dict(parse_qsl(urlparse(get_url).query))["X-Amz-Signature"]
    put_sig = dict(parse_qsl(urlparse(put_url).query))["X-Amz-Signature"]
    assert get_sig != put_sig


def test_fake_r2_round_trip():
    """The fake R2 verifies presigned PUT/GET end to end, and refuses a read-only PUT."""
    from starlette.testclient import TestClient

    from tools.fake_r2 import FakeR2State, create_fake_r2

    state = FakeR2State()
    state.keys["write"] = "write_secret"
    state.keys["read"] = "read_secret"
    state.read_only.add("read")
    app = create_fake_r2(state)

    with TestClient(app) as client:
        put_url = presign(
            "PUT", "http://testserver", "bucket", "k",
            access_key="write", secret_key="write_secret", expires=3600,
            now=datetime.now(timezone.utc),
        )
        assert client.put(put_url, content=b"hello").status_code == 200
        assert state.objects["k"] == b"hello"

        get_url = presign(
            "GET", "http://testserver", "bucket", "k",
            access_key="read", secret_key="read_secret", expires=3600,
            now=datetime.now(timezone.utc),
        )
        resp = client.get(get_url)
        assert resp.status_code == 200
        assert resp.content == b"hello"

        # A read-only key may not PUT.
        bad_put = presign(
            "PUT", "http://testserver", "bucket", "k2",
            access_key="read", secret_key="read_secret", expires=3600,
            now=datetime.now(timezone.utc),
        )
        assert client.put(bad_put, content=b"x").status_code == 403

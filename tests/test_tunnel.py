"""Offline tests for core.tunnel (pyngrok is stubbed — no ngrok process, no network)."""

import pytest


def test_ngrok_token_goes_to_the_process_not_the_config_file(monkeypatch):
    """ngrok 3.39+ refuses to overwrite a saved authtoken, so writing it on every start
    (ngrok.set_auth_token) made every start after the first fail. The token must be
    handed to the ngrok process instead."""
    from pyngrok import ngrok

    from core.tunnel import start_tunnel

    calls = {}

    def fake_connect(**kwargs):
        calls["connect"] = kwargs

    def fake_set_auth_token(*args, **kwargs):
        calls["set_auth_token"] = args

    monkeypatch.setattr(ngrok, "connect", fake_connect)
    monkeypatch.setattr(ngrok, "set_auth_token", fake_set_auth_token)
    monkeypatch.setenv("NGROK_AUTHTOKEN", "tok_test")
    monkeypatch.setenv("NGROK_DOMAIN", "robot.example")

    assert start_tunnel(8000, provider="ngrok") == "https://robot.example"
    assert "set_auth_token" not in calls
    kw = calls["connect"]
    assert kw["hostname"] == "robot.example" and kw["addr"] == "8000"
    assert kw["pyngrok_config"].auth_token == "tok_test"


def test_ngrok_needs_token_and_domain(monkeypatch):
    from core.tunnel import start_tunnel

    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    monkeypatch.setenv("NGROK_DOMAIN", "robot.example")
    with pytest.raises(RuntimeError, match="NGROK_AUTHTOKEN"):
        start_tunnel(8000, provider="ngrok")

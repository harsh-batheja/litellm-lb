"""Tests for the v2 OAuth pass-through worker.

Covers the one thing the worker actually does: rewrite Authorization +
anthropic-beta, strip identifying headers, forward verbatim, and retry
once on 401 after re-reading the creds file.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import List

import httpx
import pytest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))


# ---------- fixtures ----------

@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    p = tmp_path / ".credentials.json"
    p.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-INITIAL",
                    "refreshToken": "sk-ant-ort-xxx",
                    "expiresAt": "2099-12-31T23:59:59.999Z",
                    "scopes": ["read", "write"],
                    "subscriptionType": "pro",
                }
            }
        )
    )
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(p))
    return p


@pytest.fixture
def app_with_upstream(monkeypatch, creds_file):
    """Return (TestClient, captured_requests_list). Upstream is mocked via
    httpx.MockTransport — whatever the worker POSTs gets captured so tests
    can assert exactly what was forwarded."""
    captured: List[httpx.Request] = []
    responders: List = []  # list of callables(req) -> httpx.Response

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if responders:
            resp = responders.pop(0)
            return resp(request) if callable(resp) else resp
        return httpx.Response(500, json={"error": "no responder queued"})

    # Force a fresh module import so the module-level config picks up the
    # monkeypatched env var from creds_file.
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy  # noqa: E402

    # Swap the client for one with MockTransport
    oauth_proxy._client = httpx.AsyncClient(
        base_url=oauth_proxy.UPSTREAM,
        transport=httpx.MockTransport(handler),
    )

    from fastapi.testclient import TestClient

    client = TestClient(oauth_proxy.app)
    # Trigger startup/shutdown so lifespan hooks run
    with client:
        yield client, captured, responders, oauth_proxy, creds_file


# ---------- header-rewrite tests ----------

def test_build_headers_adds_bearer_and_beta(monkeypatch, creds_file):
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy
    incoming = {
        "host": "worker1:4000",
        "authorization": "Bearer some-other-token",
        "x-api-key": "should-be-removed",
        "content-length": "123",
        "anthropic-version": "2023-06-01",
        "x-custom-passthrough": "keepme",
    }
    out = oauth_proxy._build_headers(incoming, "tok-123")
    assert out["Authorization"] == "Bearer tok-123"
    assert "x-api-key" not in {k.lower() for k in out}
    assert "host" not in {k.lower() for k in out}
    assert "content-length" not in {k.lower() for k in out}
    assert out["anthropic-version"] == "2023-06-01"
    assert oauth_proxy.OAUTH_BETA in out["anthropic-beta"]
    assert out["x-custom-passthrough"] == "keepme"


def test_build_headers_merges_existing_anthropic_beta(monkeypatch, creds_file):
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy
    incoming = {"anthropic-beta": "prompt-caching-2024-07-31,files-api-2025-04-14"}
    out = oauth_proxy._build_headers(incoming, "tok")
    betas = [b.strip() for b in out["anthropic-beta"].split(",")]
    assert "prompt-caching-2024-07-31" in betas
    assert "files-api-2025-04-14" in betas
    assert oauth_proxy.OAUTH_BETA in betas


def test_build_headers_deduplicates_our_beta(monkeypatch, creds_file):
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy
    incoming = {"anthropic-beta": oauth_proxy.OAUTH_BETA}
    out = oauth_proxy._build_headers(incoming, "tok")
    assert out["anthropic-beta"].count(oauth_proxy.OAUTH_BETA) == 1


def test_build_headers_default_anthropic_version_when_missing(monkeypatch, creds_file):
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy
    out = oauth_proxy._build_headers({}, "tok")
    assert out["anthropic-version"] == "2023-06-01"


def test_read_token_returns_current_value(monkeypatch, creds_file):
    for mod in list(sys.modules):
        if mod == "oauth_proxy":
            del sys.modules[mod]
    import oauth_proxy
    assert oauth_proxy._read_token() == "sk-ant-oat-INITIAL"
    # Simulate CLI-driven refresh rewriting the file
    creds_file.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-ROTATED",
                    "refreshToken": "r",
                    "expiresAt": "2099-12-31T23:59:59.999Z",
                    "scopes": ["read", "write"],
                    "subscriptionType": "pro",
                }
            }
        )
    )
    assert oauth_proxy._read_token() == "sk-ant-oat-ROTATED"


# ---------- endpoint behaviour ----------

def test_health_returns_ok_when_creds_readable(app_with_upstream):
    client, _, _, _, _ = app_with_upstream
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_returns_503_when_creds_missing(app_with_upstream):
    client, _, _, _, creds_file = app_with_upstream
    creds_file.unlink()
    r = client.get("/health")
    assert r.status_code == 503


def test_messages_forwards_body_verbatim(app_with_upstream):
    client, captured, responders, _, _ = app_with_upstream
    upstream_resp = {
        "id": "msg_abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    responders.append(httpx.Response(200, json=upstream_resp))

    body = {
        "model": "claude-haiku-4-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "say ok"}],
    }
    r = client.post(
        "/v1/messages",
        json=body,
        headers={"x-api-key": "router-master-key", "anthropic-version": "2023-06-01"},
    )
    assert r.status_code == 200
    assert r.json() == upstream_resp

    # Exactly one upstream call, body verbatim
    assert len(captured) == 1
    fwd = captured[0]
    assert json.loads(fwd.content) == body
    # Auth was rewritten
    assert fwd.headers["Authorization"] == "Bearer sk-ant-oat-INITIAL"
    # x-api-key did not survive
    assert "x-api-key" not in {k.lower() for k in fwd.headers.keys()}
    assert fwd.headers["anthropic-version"] == "2023-06-01"
    assert "oauth-2025-04-20" in fwd.headers["anthropic-beta"]


def test_messages_retries_once_on_401_with_fresh_token(app_with_upstream):
    client, captured, responders, _, creds_file = app_with_upstream
    # First call returns 401; second returns 200. Between them the file is
    # updated with a rotated token (simulating out-of-band CLI refresh).
    def first(req):
        # Simulate refresh happening right after the first request
        creds_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat-ROTATED",
                        "refreshToken": "r",
                        "expiresAt": "2099-12-31T23:59:59.999Z",
                        "scopes": ["read", "write"],
                        "subscriptionType": "pro",
                    }
                }
            )
        )
        return httpx.Response(401, json={"error": "expired"})

    responders.append(first)
    responders.append(httpx.Response(200, json={"ok": True}))

    r = client.post(
        "/v1/messages",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(captured) == 2
    # First attempt had the initial token, second the rotated one
    assert captured[0].headers["Authorization"] == "Bearer sk-ant-oat-INITIAL"
    assert captured[1].headers["Authorization"] == "Bearer sk-ant-oat-ROTATED"


def test_messages_passes_through_upstream_error_status(app_with_upstream):
    client, _, responders, _, _ = app_with_upstream
    responders.append(httpx.Response(429, json={"error": "rate_limited"}))
    r = client.post(
        "/v1/messages",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited"}


def test_messages_streaming_passthrough(app_with_upstream):
    client, captured, responders, _, _ = app_with_upstream
    sse_chunks = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":5}}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    def streaming_resp(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_chunks,
            headers={"content-type": "text/event-stream"},
        )

    responders.append(streaming_resp)

    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 10,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"accept": "text/event-stream"},
    )
    assert r.status_code == 200
    # Body came back intact
    assert b"content_block_delta" in r.content
    assert b'"text":"hi"' in r.content.replace(b" ", b"")

    assert len(captured) == 1
    # Stream detection fired: confirm body had `"stream":true`
    fwd_body = json.loads(captured[0].content)
    assert fwd_body["stream"] is True


def test_chat_completions_returns_501(app_with_upstream):
    client, _, _, _, _ = app_with_upstream
    r = client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 501
    assert "anthropic/" in r.json()["error"]["message"]

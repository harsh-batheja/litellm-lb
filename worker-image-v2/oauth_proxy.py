"""OAuth pass-through proxy for one Claude Code subscription.

Replaces the nested LiteLLM worker from v1. The router in front of this
service still does model aliasing, sticky routing, spend tracking, and
virtual-key auth — this service just rewrites Authorization and forwards
verbatim. No prisma dep, no LiteLLM in the worker path, no format
translation, fast cold start.

Reads the Claude CLI's OAuth access token from a credentials file on
every request (the CLI manages refresh by rewriting the file). On 401
from upstream we re-read once and retry, covering the window where the
CLI just refreshed but we cached the stale token.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
from typing import AsyncIterator, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("oauth-proxy")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

CREDENTIALS_PATH = pathlib.Path(
    os.environ.get("CLAUDE_CREDENTIALS_PATH", "/home/claude/.claude/.credentials.json")
)
UPSTREAM = os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
OAUTH_BETA = os.environ.get("CLAUDE_OAUTH_BETA", "oauth-2025-04-20")
USER_AGENT = os.environ.get("CLAUDE_CODE_USER_AGENT", "claude-cli/2.1.101 (external)")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "600"))

# Headers from the incoming request that we strip before forwarding.
# Everything else (anthropic-beta additions, model headers, etc.) passes through.
_STRIP_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
    }
)


def _read_token() -> str:
    return json.loads(CREDENTIALS_PATH.read_text())["claudeAiOauth"]["accessToken"]


def _build_headers(incoming: Dict[str, str], token: str) -> Dict[str, str]:
    fwd = {k: v for k, v in incoming.items() if k.lower() not in _STRIP_HEADERS}
    fwd["Authorization"] = f"Bearer {token}"
    fwd["anthropic-version"] = fwd.get("anthropic-version", "2023-06-01")
    # Merge/replace beta: include our OAuth beta + anything the caller sent
    existing_beta = fwd.pop("anthropic-beta", "")
    betas = [b.strip() for b in existing_beta.split(",") if b.strip()]
    if OAUTH_BETA not in betas:
        betas.append(OAUTH_BETA)
    fwd["anthropic-beta"] = ",".join(betas)
    fwd.setdefault("User-Agent", USER_AGENT)
    return fwd


# Shared client, long-lived. httpx reuses connections.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=UPSTREAM,
            timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        _read_token()
        log.info(
            "oauth-proxy ready — creds=%s upstream=%s beta=%s",
            CREDENTIALS_PATH,
            UPSTREAM,
            OAUTH_BETA,
        )
    except Exception as exc:
        log.error("startup: cannot read %s: %s", CREDENTIALS_PATH, exc)
        raise
    try:
        yield
    finally:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


app = FastAPI(title="claude-oauth-proxy", version="2.0.0", lifespan=_lifespan)


@app.get("/health/liveliness")
@app.get("/health")
async def health() -> Dict[str, str]:
    try:
        _read_token()
        return {"status": "ok"}
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


async def _forward_streaming(
    body: bytes, headers: Dict[str, str]
) -> AsyncIterator[bytes]:
    client = _get_client()
    async with client.stream("POST", "/v1/messages", content=body, headers=headers) as r:
        if r.status_code >= 400:
            text = (await r.aread()).decode(errors="replace")
            # Surface upstream error as an SSE error event so streaming clients see it
            yield f"event: error\ndata: {json.dumps({'error': {'status': r.status_code, 'message': text[:800]}})}\n\n".encode()
            return
        async for chunk in r.aiter_bytes():
            if chunk:
                yield chunk


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    body = await request.body()
    # Detect streaming without parsing the whole JSON — cheap and good enough
    wants_stream = b'"stream":true' in body.replace(b" ", b"")
    token = _read_token()
    headers = _build_headers(dict(request.headers), token)

    if wants_stream:
        headers.setdefault("Accept", "text/event-stream")
        return StreamingResponse(
            _forward_streaming(body, headers), media_type="text/event-stream"
        )

    client = _get_client()
    r = await client.post("/v1/messages", content=body, headers=headers)
    if r.status_code == 401:
        # Creds may have just been refreshed by the CLI
        token = _read_token()
        headers = _build_headers(dict(request.headers), token)
        r = await client.post("/v1/messages", content=body, headers=headers)

    # Pass through upstream body + status verbatim
    excluded_resp_headers = {"content-length", "transfer-encoding", "connection"}
    resp_headers = {
        k: v for k, v in r.headers.items() if k.lower() not in excluded_resp_headers
    }
    return Response(
        content=r.content, status_code=r.status_code, headers=resp_headers
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    # Intentionally not supported in v2. Upstream router should use anthropic/
    # provider, not openai/, so this path should never fire. Surface a clear
    # error if someone misconfigures.
    return JSONResponse(
        {
            "error": {
                "message": "v2 worker only accepts /v1/messages. Router should use 'anthropic/<model>' provider, not 'openai/<model>'.",
                "type": "configuration_error",
            }
        },
        status_code=501,
    )

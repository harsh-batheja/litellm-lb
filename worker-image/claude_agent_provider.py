"""OAuth pass-through provider for Claude Code subscription.

Replaces the previous subprocess-shell-out provider. Forwards requests
directly to `api.anthropic.com/v1/messages` using the worker's Claude Code
subscription OAuth access token, so the laptop's outer Claude Code system
prompt + tools flow through unchanged — no double ~25k-token overhead from
a fresh inner `claude -p` session.

Credential lifecycle: the file at /home/claude/.claude/.credentials.json is
already managed (written/refreshed) by the Claude CLI running elsewhere in
the container. This provider just reads the current access token on every
request. On 401 we re-read once (in case refresh happened between read and
POST) and retry.

The `claude_agent_provider` module-level name is preserved so the existing
`custom_provider_map` in config-worker.yaml keeps working without edits.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

import httpx
import litellm
from litellm import CustomLLM, ModelResponse, Usage
from litellm.types.utils import (
    Choices,
    GenericStreamingChunk,
    Message as LiteLLMMessage,
)

CREDENTIALS_PATH = os.environ.get(
    "CLAUDE_CREDENTIALS_PATH", "/home/claude/.claude/.credentials.json"
)
ANTHROPIC_API_BASE = os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
OAUTH_BETA = os.environ.get("CLAUDE_OAUTH_BETA", "oauth-2025-04-20")
USER_AGENT = os.environ.get("CLAUDE_CODE_USER_AGENT", "claude-cli/2.1.101 (external)")
DEFAULT_TIMEOUT_S = float(os.environ.get("CLAUDE_UPSTREAM_TIMEOUT", "600"))

# Short aliases that come in from the router map to full Anthropic model IDs.
# Keep in sync with config-worker.yaml.
MODEL_ALIAS_MAP: Dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5",
}


def _extract_model(model: str) -> str:
    short = model.split("/")[-1] if "/" in model else model
    return MODEL_ALIAS_MAP.get(short, short)


def _read_access_token() -> str:
    with open(CREDENTIALS_PATH) as f:
        return json.load(f)["claudeAiOauth"]["accessToken"]


# ------------------ OpenAI-chat -> Anthropic-messages translation ------------------

def _coerce_content(content: Any) -> Any:
    """Pass through Anthropic-style content blocks verbatim when the caller
    already sent them; collapse to plain string for simple text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # If already Anthropic blocks (have `type`), pass through.
        coerced: List[Dict[str, Any]] = []
        for b in content:
            if isinstance(b, dict):
                if "type" in b:
                    coerced.append(b)
                elif "text" in b:
                    coerced.append({"type": "text", "text": b["text"]})
                else:
                    coerced.append({"type": "text", "text": json.dumps(b)})
            elif isinstance(b, str):
                coerced.append({"type": "text", "text": b})
        return coerced
    return str(content)


def _translate_openai_tool_calls(tc_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tc in tc_list or []:
        if tc.get("type") != "function":
            continue
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:22]}",
                "name": fn.get("name", ""),
                "input": args,
            }
        )
    return out


def _split_system_and_messages(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """OpenAI: system lives inline. Anthropic: top-level `system`, no system role."""
    system_blocks: List[Dict[str, Any]] = []
    out: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            c = _coerce_content(content)
            if isinstance(c, str):
                system_blocks.append({"type": "text", "text": c})
            elif isinstance(c, list):
                system_blocks.extend(c)
            continue

        if role == "tool":
            # OpenAI tool response -> Anthropic tool_result content block,
            # carried as content of a user-role message.
            tc_id = m.get("tool_call_id") or m.get("id")
            text = _coerce_content(content)
            if isinstance(text, list):
                result_content: Any = text
            else:
                result_content = str(text)
            pending_tool_results.append(
                {"type": "tool_result", "tool_use_id": tc_id, "content": result_content}
            )
            continue

        flush_tool_results()

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            c = _coerce_content(content)
            if isinstance(c, str) and c:
                blocks.append({"type": "text", "text": c})
            elif isinstance(c, list):
                blocks.extend(c)
            if m.get("tool_calls"):
                blocks.extend(_translate_openai_tool_calls(m["tool_calls"]))
            out.append({"role": "assistant", "content": blocks or ""})
            continue

        if role == "user":
            out.append({"role": "user", "content": _coerce_content(content)})
            continue

        # Unknown roles: best-effort fallthrough
        out.append({"role": role or "user", "content": _coerce_content(content)})

    flush_tool_results()
    return system_blocks, out


def _translate_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            out.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        elif "input_schema" in t or "name" in t:
            # Already Anthropic shape
            out.append(t)
    return out or None


def _build_anthropic_body(
    model: str, messages: List[Dict[str, Any]], **kwargs: Any
) -> Dict[str, Any]:
    system_blocks, anth_messages = _split_system_and_messages(messages)
    body: Dict[str, Any] = {
        "model": model,
        "messages": anth_messages,
        "max_tokens": int(kwargs.get("max_tokens") or 4096),
    }
    if system_blocks:
        body["system"] = system_blocks

    for k_src, k_dst in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
        ("metadata", "metadata"),
    ):
        v = kwargs.get(k_src)
        if v is not None:
            body[k_dst] = v

    stop = kwargs.get("stop") or kwargs.get("stop_sequences")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    tools = _translate_openai_tools(kwargs.get("tools"))
    if tools:
        body["tools"] = tools

    tc = kwargs.get("tool_choice")
    if tc:
        if isinstance(tc, dict) and tc.get("type") == "function":
            body["tool_choice"] = {
                "type": "tool",
                "name": tc.get("function", {}).get("name", ""),
            }
        elif tc == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif tc == "required":
            body["tool_choice"] = {"type": "any"}

    return body


# ------------------ Anthropic-messages -> OpenAI-chat response translation -----------

def _anthropic_to_model_response(raw: Dict[str, Any], echoed_model: str) -> ModelResponse:
    content_blocks = raw.get("content", []) or []
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
    content_text = "".join(text_parts)

    usage = raw.get("usage") or {}
    in_tok = usage.get("input_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    prompt_tokens = in_tok + cache_create + cache_read
    completion_tokens = usage.get("output_tokens", 0) or 0

    stop_reason = raw.get("stop_reason") or "end_turn"
    finish_reason = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop_reason, "stop")

    msg_kwargs: Dict[str, Any] = {"content": content_text, "role": "assistant"}
    if tool_calls:
        msg_kwargs["tool_calls"] = tool_calls

    resp = ModelResponse()
    resp.id = raw.get("id") or f"chatcmpl-{uuid.uuid4().hex}"
    resp.object = "chat.completion"
    resp.created = int(time.time())
    resp.model = echoed_model
    resp.choices = [
        Choices(
            finish_reason=finish_reason,
            index=0,
            message=LiteLLMMessage(**msg_kwargs),
        )
    ]
    resp.usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return resp


# ------------------ Streaming SSE translator --------------------------------

async def _translate_anthropic_sse(
    sse_lines: AsyncIterator[Any], echoed_model: str
) -> AsyncIterator[GenericStreamingChunk]:
    """Convert Anthropic MessageStream SSE events to LiteLLM GenericStreamingChunks.

    httpx.Response.aiter_lines() yields str; some other async iterators yield
    bytes. Handle both.
    """
    buf_event: Optional[str] = None
    usage_in = 0
    usage_cache_create = 0
    usage_cache_read = 0
    usage_out = 0
    finish_reason: Optional[str] = None

    async for raw_line in sse_lines:
        if raw_line is None:
            continue
        if isinstance(raw_line, bytes):
            text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        else:
            text = str(raw_line).rstrip("\r\n")
        if not text:
            buf_event = None
            continue
        if text.startswith("event:"):
            buf_event = text[6:].strip()
            continue
        if not text.startswith("data:"):
            continue
        payload_s = text[5:].strip()
        try:
            payload = json.loads(payload_s)
        except json.JSONDecodeError:
            continue
        etype = payload.get("type") or buf_event
        if etype == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                t = delta.get("text", "")
                if t:
                    yield {
                        "text": t,
                        "is_finished": False,
                        "finish_reason": None,
                        "index": 0,
                        "tool_use": None,
                        "usage": None,
                    }
        elif etype == "message_start":
            msg = payload.get("message") or {}
            u = msg.get("usage") or {}
            usage_in = (u.get("input_tokens") or 0)
            usage_cache_create = (u.get("cache_creation_input_tokens") or 0)
            usage_cache_read = (u.get("cache_read_input_tokens") or 0)
        elif etype == "message_delta":
            u = payload.get("usage") or {}
            # message_delta usage has output_tokens totals
            usage_out = u.get("output_tokens", usage_out) or usage_out
            stop_reason = (payload.get("delta") or {}).get("stop_reason")
            if stop_reason:
                finish_reason = {
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                }.get(stop_reason, "stop")
        elif etype == "message_stop":
            break
        # content_block_start/content_block_stop for tool_use: emitted as
        # tool_use block assembly. For MVP we skip incremental tool_use SSE
        # translation — will surface via non-streaming retry if client needs.

    prompt_tokens = usage_in + usage_cache_create + usage_cache_read
    yield {
        "text": "",
        "is_finished": True,
        "finish_reason": finish_reason or "stop",
        "index": 0,
        "tool_use": None,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage_out,
            "total_tokens": prompt_tokens + usage_out,
        },
    }


# ------------------ Provider class -----------------------------------------

class ClaudeOAuthProxyProvider(CustomLLM):
    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        print(
            f"ClaudeOAuthProxyProvider initialized: "
            f"creds={CREDENTIALS_PATH} upstream={ANTHROPIC_API_BASE}"
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=ANTHROPIC_API_BASE,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=10.0),
            )
        return self._client

    @staticmethod
    def _headers(access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": OAUTH_BETA,
            "user-agent": USER_AGENT,
            "content-type": "application/json",
            "accept": "application/json",
        }

    async def _post_messages(self, body: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_client()
        for attempt in (1, 2):
            token = _read_access_token()
            r = await client.post("/v1/messages", json=body, headers=self._headers(token))
            if r.status_code == 401 and attempt == 1:
                # Creds may have just been refreshed by CLI — re-read + retry once
                await asyncio.sleep(0.2)
                continue
            if r.status_code >= 400:
                raise litellm.exceptions.APIError(
                    status_code=r.status_code,
                    message=f"Anthropic upstream {r.status_code}: {r.text[:600]}",
                    model=body.get("model", "unknown"),
                    llm_provider="claude-oauth-proxy",
                )
            return r.json()
        raise litellm.exceptions.APIError(
            status_code=401,
            message="Anthropic upstream returned 401 twice; token likely expired without CLI refresh",
            model=body.get("model", "unknown"),
            llm_provider="claude-oauth-proxy",
        )

    async def acompletion(self, model: str, messages: List[Dict[str, Any]], **kwargs: Any) -> ModelResponse:
        full_model = _extract_model(model)
        body = _build_anthropic_body(full_model, messages, **kwargs)
        raw = await self._post_messages(body)
        return _anthropic_to_model_response(raw, echoed_model=model)

    def completion(self, model: str, messages: List[Dict[str, Any]], **kwargs: Any) -> ModelResponse:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(
                    asyncio.run, self.acompletion(model, messages, **kwargs)
                ).result()
        return asyncio.run(self.acompletion(model, messages, **kwargs))

    def streaming(self, *args: Any, **kwargs: Any) -> Iterator[GenericStreamingChunk]:
        raise NotImplementedError("Sync streaming not supported")

    async def astreaming(
        self, model: str, messages: List[Dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[GenericStreamingChunk]:
        full_model = _extract_model(model)
        body = _build_anthropic_body(full_model, messages, **kwargs)
        body["stream"] = True

        client = self._get_client()
        token = _read_access_token()
        headers = self._headers(token)
        headers["accept"] = "text/event-stream"

        async with client.stream("POST", "/v1/messages", json=body, headers=headers) as r:
            if r.status_code == 401:
                # retry once with fresh token
                token = _read_access_token()
                headers = self._headers(token)
                headers["accept"] = "text/event-stream"
                async with client.stream("POST", "/v1/messages", json=body, headers=headers) as r2:
                    if r2.status_code >= 400:
                        body_txt = (await r2.aread()).decode(errors="replace")[:600]
                        raise litellm.exceptions.APIError(
                            status_code=r2.status_code,
                            message=f"Anthropic upstream {r2.status_code}: {body_txt}",
                            model=body["model"],
                            llm_provider="claude-oauth-proxy",
                        )
                    async for chunk in _translate_anthropic_sse(r2.aiter_lines(), model):
                        yield chunk
                    return

            if r.status_code >= 400:
                body_txt = (await r.aread()).decode(errors="replace")[:600]
                raise litellm.exceptions.APIError(
                    status_code=r.status_code,
                    message=f"Anthropic upstream {r.status_code}: {body_txt}",
                    model=body["model"],
                    llm_provider="claude-oauth-proxy",
                )
            async for chunk in _translate_anthropic_sse(r.aiter_lines(), model):
                yield chunk


# Public name preserved so the existing custom_provider_map keeps working
claude_agent_provider = ClaudeOAuthProxyProvider()

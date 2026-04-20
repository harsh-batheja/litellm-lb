# v2 — slim OAuth-proxy workers

**Status: prototype, not yet validated on hardware.** Everything here is wire-compatible with the v1 deployment's virtual keys, master key, and client-facing API surface — so if v2 works it's a drop-in swap — but nobody has run it end-to-end yet. Read through the trade-offs at the bottom before cutting over.

## What changed vs v1

| | v1 | v2 |
|---|---|---|
| Worker runtime | LiteLLM proxy (nested) | 150-line FastAPI app (`worker-image-v2/oauth_proxy.py`) |
| Worker image | ~1.5 GB | ~150 MB |
| Worker cold start | ~15 s | ~1 s |
| Worker translates formats | OpenAI chat ↔ Anthropic messages (custom LiteLLM provider) | None — pure Authorization header rewrite, byte-level passthrough |
| Router talks to worker as | `openai/<short>` chat-completions | `anthropic/<full model id>` messages API |
| Prisma in the worker? | Yes — caused the "regen after every restart" gotcha | No — router is the only thing with a DB |
| Router image | Custom (`litellm-lb-worker:local`, used for router too in v1) | Stock `ghcr.io/berriai/litellm:main-v1.83.10` |

## Why this is better

- **One format, one hop.** The worker never parses or re-emits a request body. Even vision, extended thinking, tool use, and prompt caching just work — whatever shape Anthropic accepts today, the worker forwards.
- **Prisma gotcha gone.** The router still has prisma, but we control the router image and can pin / regen in the Dockerfile if needed. Workers are prisma-free.
- **Image size + cold start.** Workers start in ~1 s (important when docker-compose recreates them or when the LXC reboots).
- **Fewer moving parts to debug.** Every 500 in v1 required reading LiteLLM's provider plumbing, streaming_iterator.py, and `_wrap_streaming_iterator_with_enrichment`. v2's worker is one file you can `cat`.

## Trade-offs (be aware)

1. **ChatGPT (`gpt/*`) routes are commented out** in `config-router.v2.yaml`. The stock LiteLLM image doesn't ship the `chatgpt` provider the v1 stack uses. To keep ChatGPT in v2 you either (a) switch back to a custom router image that bundles it, or (b) register it as a custom provider via the upstream LiteLLM mechanism. Not blocking for Claude + GLM usage.
2. **No LiteLLM in the worker** = no per-worker spend log, no per-worker `/metrics` endpoint. Spend is tracked by the router as before (against the virtual key). Per-worker success/failure rates can be added later with a FastAPI middleware that emits Prometheus counters — not in this prototype.
3. **Router depends on the upstream LiteLLM image staying published.** Stock `ghcr.io/berriai/litellm:main-v1.83.10` is what consumers would use; if Berri's registry goes away the user just needs to build from source. v1's `cabinlab/litellm-claude-code` base image had the same risk.
4. **Provider behavior around `anthropic/` + `api_base`.** This prototype assumes LiteLLM, when configured with `model: anthropic/<id>` and an `api_base`, sends requests in Anthropic `/v1/messages` format to that base URL. That should be the documented behavior but I haven't verified against LiteLLM 1.83.10 specifically — this is step 1 of the validation plan below.

## Side-by-side deployment (for validation)

The v2 compose file uses a separate named DB volume (`litellm-db-v2`) so v1 and v2 can coexist on the same host for testing. To run v2 on a different port (say 4001 for v2, keep 4000 for v1):

```bash
# In docker-compose.v2.yaml, change router.ports to ["4001:4000"] and bring up with a distinct project name
COMPOSE_PROJECT_NAME=litellm-lb-v2 \
  docker compose -f docker-compose.v2.yaml up -d

# Seed OAuth credentials into v2's own volumes — copy from v1 so both stacks share the same subscriptions
for w in worker1 worker2; do
  docker cp <(docker compose exec -T $w cat /home/claude/.claude/.credentials.json) \
            litellm-lb-v2-$w-1:/home/claude/.claude/.credentials.json
done
```

## Validation plan (before cutover)

1. Bring up v2 on port 4001 alongside v1 on 4000.
2. Mint a temporary v2 virtual key on the v2 router (master key + key aliases are separate per DB).
3. `curl` the v2 router with a known-good request; compare the Anthropic response against the v1 router response. Should be byte-identical.
4. Streaming test (`"stream": true`) — confirm SSE comes through unchanged, input_tokens are billed correctly.
5. Tool-use test — send a request with a `tools` array, check that `tool_use` blocks come back and cache_control works.
6. Sticky routing test — hit `claude-sonnet-4-6-a` 20 times, verify each hits worker1 (check worker access logs).
7. Run for 24 h; confirm no OAuth expirations or 401 loops.
8. Cutover: shut v1 down, remap v2's router to port 4000, mint the real virtual keys (or `pg_dump` from v1's DB and restore into v2).

## Open questions / TODOs before production

- [ ] Write pytest tests for the proxy: header rewrite correctness, 401 retry, SSE passthrough.
- [ ] Add a GitHub Action that builds the image and runs the tests on PR.
- [ ] Confirm LiteLLM router sends Anthropic-format bodies when config uses `anthropic/<model>` + `api_base` (documentation implies yes; validate empirically — step 1 above).
- [ ] Prometheus middleware for per-worker success/error counters.
- [ ] Token-refresh path: current assumption is that the CLI refreshes the credentials file out-of-band. If/when Anthropic shortens token lifetime, implement refresh using `https://platform.claude.com/v1/oauth/token` inside the proxy (background task + 401 re-read is already there).

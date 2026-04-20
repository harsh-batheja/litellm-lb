# litellm-lb

A self-hosted LiteLLM load balancer that pools **multiple Claude Code OAuth subscriptions** behind a single OpenAI- and Anthropic-compatible API, so you can point `claude`, `opencode`, or anything else that speaks OpenAI/Anthropic at one endpoint and fan out across as many Claude Pro/Team accounts as you have. Also routes GLM (Z.AI) and ChatGPT OAuth models via the same proxy for spend tracking and unified auth.

The interesting bit is `worker-image/oauth_proxy.py`: a ~150-line FastAPI service that rewrites the `Authorization` header on incoming `/v1/messages` requests to use a Claude Code subscription's OAuth token, then forwards the request body **verbatim** to `api.anthropic.com`. No format translation, no nested LiteLLM, no prisma. Each worker holds one subscription; run N of them for N accounts.

## Architecture

```
client  (claude CLI, opencode, hermes, n8n, etc.)
   │
   │  /v1/messages   OR   /v1/chat/completions  (OpenAI-format)
   ▼
┌──────────────────────────────────────────┐
│  router   LiteLLM proxy  (stock image)   │   :4000
│    · virtual-key auth + spend tracking    │
│    · model aliases, sticky -a/-b routes   │
│    · routes claude-* to workers,          │
│      glm-* to z.ai, gpt-* to ChatGPT      │
└───────────────┬──────────────────────────┘
                │  anthropic/<full-model-id>
                │  api_base: http://workerN:4000
                ▼
       ┌─────────────────┐   ┌─────────────────┐
       │  worker1        │   │  worker2        │
       │  (FastAPI       │   │  (FastAPI       │
       │   OAuth proxy)  │   │   OAuth proxy)  │
       │                 │   │                 │
       │  OAuth acct #1  │   │  OAuth acct #2  │
       └────────┬────────┘   └────────┬────────┘
                │                     │
                └──────────┬──────────┘
                           ▼
              api.anthropic.com/v1/messages
                 Authorization: Bearer <oauth>
                 anthropic-beta: oauth-2025-04-20
```

Postgres backs LiteLLM's virtual-key + spend tables. The `db` service uses stock `postgres:16`; nothing custom in the schema. The `router` service runs stock `ghcr.io/berriai/litellm:main-stable` — no custom LiteLLM image to maintain.

## Why it's structured this way

- **One format, one hop in the worker.** The worker never parses or re-emits the request body. Whatever shape Anthropic accepts today — vision, extended thinking, tool use, prompt caching — flows through unchanged.
- **Subscription OAuth tokens cost less than API keys** for equivalent throughput, and Claude Code subscribers already pay for them. This lets you expose those subscription rates to anything that can target an OpenAI-compatible URL.
- **Per-worker OAuth isolation.** Each worker has its own Docker volume (`worker1-claude`, `worker2-claude`, …) with exactly one subscription's `.credentials.json`. A compromise of one worker's token doesn't touch the others.
- **Sticky routing.** Model names suffixed with `-a` / `-b` (or as many letters as you have workers) pin specific consumers to specific subscriptions — useful when one user's usage shouldn't drag down another's rate limit.

## Components

| Path | What it is |
|---|---|
| `docker-compose.yaml` | 4-service stack: db + router + two workers |
| `config-router.yaml` | Model aliases: `{sonnet,opus,haiku}` round-robin, `claude-{opus-4-7,sonnet-4-6,haiku-4-5}` explicit, `-a` / `-b` sticky variants, `glm/*` direct to z.ai, `gpt/*` via ChatGPT OAuth |
| `worker-image/Dockerfile` | `python:3.11-slim` + fastapi + uvicorn + httpx (~150 MB) |
| `worker-image/oauth_proxy.py` | The OAuth pass-through. One file, ~150 lines |
| `worker-image/test_oauth_proxy.py` | 12 pytest cases covering header rewrite, 401 retry, SSE passthrough |
| `.github/workflows/ci.yml` | Runs the test suite + docker build on PRs touching `worker-image/` |

## Bootstrap on a fresh VM

### 1. Prereqs

```bash
curl -fsSL https://get.docker.com | sh
sudo apt-get install -y docker-compose-plugin jq
```

### 2. Place this repo at `/opt/litellm-lb`

```bash
sudo git clone <this-repo-url> /opt/litellm-lb
cd /opt/litellm-lb
```

### 3. Fill in `.env`

```bash
cp .env.example .env
# Edit .env:
#   LITELLM_MASTER_KEY — for /key/* admin routes. Generate: openssl rand -hex 32 | sed 's/^/sk-/'
#   POSTGRES_PASSWORD  — any strong random string
#   ZAI_API_KEY        — if you want GLM routes; leave empty otherwise
```

### 4. Build the worker image and boot the stack

```bash
docker compose up -d --build
# wait for router health
until curl -fsS http://localhost:4000/health/liveliness >/dev/null; do sleep 1; done
```

### 5. Seed OAuth credentials (one per worker)

Each worker needs a `.credentials.json` at `/home/claude/.claude/.credentials.json`. Three options:

**(a) Interactive login inside the container** — simplest if you have one subscription per account:

```bash
docker compose exec -it worker1 \
  sh -c "mkdir -p /home/claude/.claude && cd /home/claude && \
  python -c 'print(\"Run \\`claude setup-token\\` here after installing claude-code CLI\")'"
# Or install claude-code CLI in a sidecar and paste tokens into /home/claude/.claude/.credentials.json
```

The file format (all values are placeholders):

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat-…",
    "refreshToken": "sk-ant-ort-…",
    "expiresAt": "2099-12-31T23:59:59.999Z",
    "scopes": ["read", "write"],
    "subscriptionType": "pro"
  }
}
```

**(b) Copy from another machine** where you've already run `claude setup-token`:

```bash
# on the source machine
cat ~/.claude/.credentials.json

# on this host
docker run --rm \
  -v litellm-lb_worker1-claude:/data \
  -v /path/to/creds.json:/src.json:ro \
  alpine:3 sh -c "cp /src.json /data/.credentials.json && \
    chown 1000:1000 /data/.credentials.json && \
    chmod 600 /data/.credentials.json"
```

**(c) Porting from an existing deployment** — use `docker cp` out of the source worker, `docker cp` into the destination worker, then `docker compose restart workerN`.

### 6. Mint virtual keys for your consumers

```bash
source .env
mint() {
  local alias=$1 models=$2
  curl -sS -X POST http://localhost:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"$alias\",\"models\":$models}" | jq -r .key
}

mint claude-code   '["claude-opus-4-7","claude-sonnet-4-6","claude-haiku-4-5"]'
mint claude-code-a '["claude-opus-4-7-a","claude-sonnet-4-6-a","claude-haiku-4-5-a"]'
mint claude-code-b '["claude-opus-4-7-b","claude-sonnet-4-6-b","claude-haiku-4-5-b"]'
```

Store the returned `sk-…` values in your secret manager — the router's Postgres keeps them too, but clients can't look them up after the fact.

## Using the LB as a Claude Code endpoint

```bash
export ANTHROPIC_BASE_URL=http://<host>:4000
export ANTHROPIC_AUTH_TOKEN=<sk-… from a claude-code key>
export ANTHROPIC_MODEL=claude-sonnet-4-6-a          # or -b, or no suffix for round-robin
export ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5-a
claude
```

Token cost per turn matches a direct Claude Code → Anthropic call (~7.5 k baseline from Claude Code's system prompt + tool definitions). There's no 2× multiplier from a subprocess CLI wrapper.

## Using the LB with opencode

In `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "litellm-lb": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM LB",
      "options": {
        "baseURL": "http://<host>:4000/v1",
        "apiKey": "sk-…"
      },
      "models": {
        "claude-sonnet-4-6": { "name": "Claude Sonnet (OAuth pool)" },
        "claude-opus-4-7": { "name": "Claude Opus (OAuth pool)"   },
        "claude-haiku-4-5": { "name": "Claude Haiku (OAuth pool)"  },
        "glm-4.5-flash":     { "name": "GLM 4.5 Flash" },
        "gpt-5.4":           { "name": "GPT 5.4 (ChatGPT OAuth)" }
      }
    }
  }
}
```

Models become available as `litellm-lb/sonnet`, `litellm-lb/glm-4.5-flash`, etc.

## Sticky routing

When you want one consumer to always land on the same subscription (e.g. to observe rate-limit behavior, or to isolate a heavy user), mint a key whitelisted to `claude-*-a` (→ worker1) or `claude-*-b` (→ worker2). Consumers set `ANTHROPIC_MODEL` (or whatever their config calls the model) to the suffixed variant.

For fleets bigger than 2 workers, add more entries in `config-router.yaml` with `-c`, `-d`, etc. suffixes pointing to `http://worker3:4000`, `http://worker4:4000`, and so on.

## Tests

```bash
cd worker-image
pip install -r requirements-dev.txt
pytest
```

Covers:
- Header rewrite (Authorization, x-api-key, anthropic-version, anthropic-beta merging/dedup)
- Credentials file re-read after out-of-band rotation
- Non-streaming `/v1/messages` body passthrough
- 401 retry with fresh token after CLI refresh
- Upstream error status passthrough (429 etc.)
- SSE streaming passthrough
- `/v1/chat/completions` returns 501 (wrong endpoint for this worker)

CI (`.github/workflows/ci.yml`) runs these on every push/PR touching `worker-image/`, plus builds the docker image.

## Known edge cases

- **ChatGPT `gpt/*` non-streaming returns 500** with `Unknown items in responses API response: []`. This is an upstream LiteLLM bug, not specific to this stack. Force `stream: true` at the client or route non-streaming needs to a different model (e.g. `glm-4.5-flash`).
- **Streaming emits a duplicate `message_start` event** at the top of the SSE stream — LiteLLM's `/v1/messages` endpoint adds its own before the worker's passthrough starts. Clients ignore duplicates; not a functional issue.
- **Subscription OAuth tokens are long-lived** (`expiresAt: 2099-12-31`), so the worker has no refresh logic. If Anthropic starts rotating them, implement refresh by POSTing to `https://platform.claude.com/v1/oauth/token` using the `refreshToken` from `.credentials.json` — the 401-retry hook in the proxy will pick up the new value on the next request.
- **LiteLLM version pin.** The router uses `ghcr.io/berriai/litellm:main-stable` by default. For production reproducibility, pin to a specific digest in `docker-compose.yaml`.

## License

MIT.

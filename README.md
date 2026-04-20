# litellm-lb

A LiteLLM load-balancer stack that pools multiple Claude Code OAuth subscriptions behind a single API surface, routes `claude-*` requests through a **direct OAuth pass-through** to `api.anthropic.com`, and also exposes Z.AI (`glm/*`) and ChatGPT (`gpt/*`) models.

The interesting custom bit is `worker-image/claude_agent_provider.py`: a LiteLLM `CustomLLM` that forwards requests to Anthropic using the Claude Code subscription's OAuth token, so there's **no subprocess-wrapped inner `claude -p` session** adding ~25 k system-prompt tokens to every turn. This makes the LB usable as a drop-in Anthropic endpoint for your own `claude` CLI (via `ANTHROPIC_BASE_URL`) without doubling your token bill.

## Architecture

```
client (e.g. laptop `claude`)
    │  /v1/messages (Anthropic format)
    ▼
┌───────────┐
│  router   │  LiteLLM proxy (:4000)
│           │   · round-robin or sticky -a/-b routes
│           │   · Anthropic / OpenAI / Z.AI upstream splits
│           │   · virtual-key auth + spend tracking
└─────┬─────┘
      │ openai/{sonnet,opus,haiku}
      ▼
┌───────────┐        ┌───────────┐
│  worker1  │        │  worker2  │   each holds ONE Claude Code
│           │        │           │   OAuth subscription in
│ claude_   │        │ claude_   │   /home/claude/.claude/
│ agent_    │        │ agent_    │   .credentials.json
│ provider  │        │ provider  │
└─────┬─────┘        └─────┬─────┘
      │                    │
      └──────────┬─────────┘
                 ▼
         api.anthropic.com/v1/messages
            Authorization: Bearer <oauth>
            anthropic-beta: oauth-2025-04-20
```

Postgres backs LiteLLM's virtual-key and spend tables. The `db` service uses the stock `postgres:16` image; nothing custom in the schema.

## Components and custom pieces

| Path | What it is | Custom? |
|---|---|---|
| `docker-compose.yaml` | 4-service stack: db + router + worker1 + worker2 | custom layout |
| `config-router.yaml` | Model aliases: `{sonnet,opus,haiku}` round-robin, `claude-{opus-4-7,sonnet-4-6,haiku-4-5}` explicit, `-a` / `-b` sticky-per-worker variants, `glm/*` direct to z.ai, `gpt/*` via ChatGPT OAuth | custom |
| `config-worker.yaml` | Worker registers the `claude-agent-sdk` custom provider and exposes `sonnet`/`opus`/`haiku` aliases that map to it | custom |
| `worker-image/Dockerfile` | Base `ghcr.io/cabinlab/litellm-claude-code:latest` + `litellm[proxy]==1.83.10` upgrade + `prometheus_client` + `@anthropic-ai/claude-code@latest` npm | custom |
| `worker-image/claude_agent_provider.py` | The OAuth pass-through provider — **the main custom code** | custom |

Upstream bits we depend on but don't modify: `postgres:16`, the `cabinlab` base image (provides the worker entrypoint that seeds `.credentials.json` from `CLAUDE_CODE_OAUTH_TOKEN`), and the `@anthropic-ai/claude-code` npm package.

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

### 3. Fill in the `.env` files

```bash
cp .env.example               .env
cp .env.worker1.example       .env.worker1
cp .env.worker2.example       .env.worker2
# Edit each file and fill in the values.
# For LITELLM_MASTER_KEY: `openssl rand -hex 32 | sed 's/^/sk-/'`
```

### 4. Seed OAuth tokens

There are two ways to populate each worker's OAuth credentials. Pick one per worker.

**(a) Start the worker once with `CLAUDE_CODE_OAUTH_TOKEN`** set in `.env.workerN`. The upstream entrypoint writes `.credentials.json` on first boot. Long-lived tokens are issued by `claude setup-token` from an already-authenticated machine.

**(b) Interactively `claude setup-token` inside the container.** Start the stack with `CLAUDE_CODE_OAUTH_TOKEN` unset; the worker's entrypoint will warn about missing creds but still start. Then:

```bash
docker compose exec -it worker1 claude setup-token
# paste token when prompted, follow any browser steps
docker compose restart worker1
# repeat for worker2 with the second Claude Code subscription
```

**(c) Copy credentials from an existing deployment** (porting case):

```bash
# on the old host
for w in worker1 worker2; do
  docker compose exec -T $w \
    cat /home/claude/.claude/.credentials.json > creds.$w.json
done

# on the new host, after first `docker compose up -d`:
docker compose stop worker1 worker2
for w in worker1 worker2; do
  docker cp creds.$w.json litellm-lb-$w-1:/home/claude/.claude/.credentials.json
  docker exec -u root litellm-lb-$w-1 chown claude:claude \
    /home/claude/.claude/.credentials.json
  docker exec -u root litellm-lb-$w-1 chmod 600 \
    /home/claude/.claude/.credentials.json
done
docker compose start worker1 worker2
```

### 5. Build and boot

```bash
docker build -t litellm-lb-worker:local ./worker-image
docker compose up -d
# wait for router health
until curl -fsS http://localhost:4000/health/liveliness >/dev/null; do sleep 1; done
```

### 6. Prisma regen (**required after every router start**)

LiteLLM 1.83.10's schema is newer than the prisma client bindings shipped in the base image. Without this, `/key/generate` and similar admin routes 500 with `FieldNotFoundError: Could not find field at upsertOneLiteLLM_VerificationToken.create.agent_id`.

```bash
docker compose exec -T router bash -c \
  "cd /opt/venv/lib/python3.11/site-packages/litellm/proxy && prisma generate"
docker compose restart router
```

> Attempts to bake `RUN prisma generate` into the Dockerfile broke the engine-binary lookup at runtime (`NotConnectedError: Not connected to the query engine`). If you find a cleaner solution (e.g. a startup hook running as the `claude` user that regenerates into `$HOME/.cache/prisma-python/`), it'd be worth a PR.

### 7. Mint virtual keys

```bash
source .env

mint() {
  local alias=$1 models=$2
  curl -sS -X POST http://localhost:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"$alias\",\"models\":$models}" | jq -r .key
}

mint claude-code-a '["claude-opus-4-7-a","claude-sonnet-4-6-a","claude-haiku-4-5-a"]'
mint claude-code-b '["claude-opus-4-7-b","claude-sonnet-4-6-b","claude-haiku-4-5-b"]'
mint claude-code   '["claude-opus-4-7","claude-sonnet-4-6","claude-haiku-4-5"]'
# ...plus any other aliases your consumers expect.
```

Store the returned `sk-...` strings wherever you keep secrets (Infisical, 1Password, a vault, etc.) — the router's Postgres DB can regenerate them but users of the keys won't learn the new value without help.

## Using the LB as a Claude Code endpoint

```bash
export ANTHROPIC_BASE_URL=http://<host>:4000
export ANTHROPIC_AUTH_TOKEN=<sk-... from claude-code-a>
export ANTHROPIC_MODEL=claude-sonnet-4-6-a
export ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5-a
claude
```

Token cost per turn matches a direct Claude Code → Anthropic call (~7.5 k baseline from Claude Code's system prompt + tool definitions; no `claude -p` subprocess 2× multiplier).

## Sticky routing vs round-robin

- **Round-robin** (`claude-sonnet-4-6` with no suffix) — LiteLLM alternates across worker1 and worker2 per request. Good for uniform workloads.
- **Sticky `-a` / `-b`** — every request for `claude-sonnet-4-6-a` lands on worker1, every `-b` on worker2. Use when one user dominates or when you want to observe which subscription is rate-limiting without noise.

A virtual key's `models` whitelist decides which workers a caller can reach.

## Model catalog (summary)

See `config-router.yaml` for the full list. Top-level families:

- **Claude** — `{sonnet, opus, haiku}` (round-robin), `claude-{opus-4-7, sonnet-4-6, haiku-4-5}` (round-robin), plus `-a` / `-b` sticky variants.
- **Z.AI GLM** — `glm`, `glm-5.1`, `glm-5`, `glm-4.7{,-flash,-flashx}`, `glm-4.6{,v}`, `glm-4.5{,-air,-flash,v}`, `glm-5-turbo`.
- **ChatGPT** — `gpt`, `gpt-5.4` (requires the ChatGPT OAuth token volume; see the `router` service's env).

## Known gotchas

- **Prisma schema drift** — see step 6. Happens on every LiteLLM version bump. Manual regen for now.
- **Streaming** — works, but LiteLLM's `/v1/messages` endpoint emits a duplicate `message_start` event at the top of the SSE stream. Clients ignore it; not a functional issue.
- **Tokens are long-lived** — the `.credentials.json` file from `claude setup-token` has `expiresAt: 2099-12-31`, i.e. doesn't rotate. If Anthropic ever changes that policy, the provider will need refresh logic (the standard OAuth2 refresh-token grant against `https://platform.claude.com/v1/oauth/token`).
- **Disk on small LXCs** — fresh image builds need ~2 GB free. If you run this in an LXC with a 4 GB root, raise to 8 GB first.

## Rollback

The old subprocess-based provider (shells out to `claude -p`) is preserved for each deployment as `worker-image/claude_agent_provider.py.subprocess.bak.<ts>`. To revert:

```bash
cp worker-image/claude_agent_provider.py.subprocess.bak.<ts> \
   worker-image/claude_agent_provider.py
docker build -t litellm-lb-worker:local ./worker-image
docker compose up -d --force-recreate
```

## License

MIT — do what you want with it.

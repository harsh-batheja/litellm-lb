# Team LiteLLM deployment — `aoagents` (91.107.194.138)

End-to-end architecture, deployment recipe, operations runbook, and open items
for the LiteLLM instance that serves the team. The `litellm-lb` repo holds the
code; this document describes how that code is wired into a production stack on
the Hetzner host at `91.107.194.138` (DNS: `llm.91-107-194-138.nip.io`).

_Last reviewed: 2026-04-24._

> **Runtime dependencies.** This stack runs entirely out of files on the
> Hetzner host — Docker, the four stacks under `/opt` (§2.2), and the
> local Postgres. It has no dependency on any external secret store or internal
> tooling. The external endpoints the running system reaches are:
>
> - `api.anthropic.com` — Claude inference (every `claude-*` call)
> - `api.z.ai` — GLM inference (every `glm-*` call)
> - `chatgpt.com/backend-api/codex/` and `auth.openai.com` — ChatGPT /
>   Codex inference + OAuth refresh (every `gpt-*` call)
> - `api.github.com` — the gh-collab-gate policy call on each SSO login
> - `github.com/login/oauth/*` — GitHub OAuth App callback during SSO
> - `acme-v02.api.letsencrypt.org` — Caddy's TLS certificate issuance /
>   renewal
>
> All secrets the runtime needs are on-disk in the container `.env` files
> and the Authentik policy expression.

## 1. High-level picture

Two distinct flows run through this system — the one-off **SSO dance** that
issues a virtual key, and the **hot path** taken by every subsequent API
call. The diagram shows both:

```
┌─ SSO flow (first visit, or when a user's session cookie has expired) ─┐
│                                                                       │
│  user ─► Caddy ─► LiteLLM ─► Authentik ─► GitHub OAuth App            │
│          (TLS)    /sso        (OIDC +         │                       │
│                   redirect    gh-collab-      ▼                       │
│                               gate)     api.github.com                │
│                                         (collaborator?)               │
│                                         204→pass, 404→deny            │
│                                                                       │
│  result: a long-lived virtual key (sk-…) is issued to the browser     │
└───────────────────────────────────────────────────────────────────────┘

┌─ Hot path (every API call from a client) ─────────────────────────────┐
│                                                                       │
│  client ─► Caddy ─► LiteLLM router ─► sticky_router ─► workerN ──►    │
│   sk-…    (TLS)     auth, scope,      hook pins        (swap auth     │
│                     budget, spend     one user to      for OAuth      │
│                             │         one worker)      bearer)        │
│                             ▼                             │           │
│                        PostgreSQL                         ▼           │
│                        (state)                   api.anthropic.com    │
└───────────────────────────────────────────────────────────────────────┘
```

- **Caddy** terminates TLS and reverse-proxies three vhosts (§2.4).
- **Authentik** is the OIDC provider; an expression policy gates by GitHub
  repository collaborator status (§4.2). Authentik sits on the SSO path only,
  **not** on the hot path — once a virtual key is issued, the client talks
  straight to LiteLLM.
- **LiteLLM router** owns the public model API (`/v1/messages`,
  `/v1/chat/completions`, `/v1/models`, `/ui/`). Does auth, access scoping,
  routing, budget tracking, spend logs.
- **Workers** are thin FastAPI processes (191 LOC in
  `worker-image/oauth_proxy.py`) that hold one Claude OAuth subscription each
  and forward verbatim to `api.anthropic.com`, swapping the Authorization
  header for the OAuth Bearer. Capacity scales with the number of workers.
- **Postgres** stores LiteLLM users / virtual keys / budgets / spend. Not
  shared with anything else.

See `README.md` for the router / worker design rationale (slim workers, no
nested LiteLLM, prompt-cache benefits).

### 1.1 Terminology

| Term | Meaning in this doc |
|---|---|
| **virtual key** | The `sk-…` LiteLLM-issued API key a client uses. Long-lived by default. Stored in LiteLLM's Postgres, not in Authentik. |
| **internal user** | LiteLLM's own user row, auto-created during SSO for each Authentik identity. Owns one or more virtual keys. |
| **gh-collab-gate policy** | The Authentik expression policy that checks `GET /repos/…/collaborators/<login>` against the GitHub API. |
| **worker** | One `oauth_proxy` container holding one Claude subscription. `worker1` and `worker2` are the two instances deployed. |
| **`-a` / `-b`** | Internal suffixed `model_name`s that pin a model to a specific worker. Not exposed to clients. |

## 2. Deployed components

### 2.1 Host
- IP `91.107.194.138`, Hetzner Cloud vServer, KVM-virtualised
- 4 vCPU (AMD EPYC), 7.6 GiB RAM, 150 GB disk
- Ubuntu 24.04 LTS, kernel 6.8, Docker 28.x
- OS users: `aoagent` (app / operations, owns `/opt/*` contents); an
  admin user in `sudo` / `docker` / `adm` for anyone who needs to edit
  config or restart containers
- Live deploy directory is `/opt/litellm-lb/` — a plain copy of this repo
  with no `.git/` metadata (see §6.3), so `git status` on the server can't
  show drift.

### 2.2 Docker stacks under `/opt`
| Stack | Purpose | Public vhost(s) |
|---|---|---|
| `gateway`    | Caddy edge + TLS; proxies to the stacks below. | (edge for all three vhosts) |
| `authentik`  | OIDC IdP, policy engine                         | `authentik.91-107-194-138.nip.io` |
| `litellm-lb` | Router + workers + postgres                     | `llm.91-107-194-138.nip.io` |
| `plane`      | Team project management                         | `plane.91-107-194-138.nip.io` |

External listeners: 22 (SSH), 80/443 (Caddy), 9000/9443 (Portainer). All
inter-container traffic runs on internal Docker networks.

### 2.3 LiteLLM router stack (`litellm-lb`)
Containers (all internal-only; Caddy is the only externally-reachable
listener):
- `litellm-lb-router-1` — `ghcr.io/berriai/litellm:main-stable`, port `:4000`
- `litellm-lb-worker1-1`, `litellm-lb-worker2-1` —
  `litellm-lb-oauth-proxy:local` built from `worker-image/`, each port `:4000`
- `litellm-lb-db-1` — `postgres:16`

Key router mounts (`docker-compose.yaml`):

| Mount | Purpose |
|---|---|
| `./config-router.yaml` → `/app/config.yaml` | Model list, routing strategy, fallbacks, callbacks, default user scope. The main operator-editable file. |
| `./sticky_router.py` → `/app/sticky_router.py` | Registered as `litellm_settings.callbacks`. Runs before every request (§3). |
| `./chatgpt-data` → `/app/chatgpt-data` (directory) | Holds `auth.json`, the ChatGPT OAuth token used by the `chatgpt/*` models (Codex backend). Git-ignored. |
| `./ui-btn-patched.js` → `/usr/lib/python3.13/site-packages/litellm/proxy/_experimental/out/_next/static/chunks/80899acb7e1a7640.js` | In-place patch to the LiteLLM UI bundle. Server-side only; not tracked in git (§6.3). |

Each worker mounts a named volume `workerN-claude` at `/home/claude/.claude/`
containing that subscription's `.credentials.json`. Tokens have
`expiresAt: 2099-12-31` and are effectively non-expiring.

### 2.4 Caddy vhosts (`/opt/gateway/Caddyfile`)
Three externally-reachable hostnames, all automatically TLS-terminated via
Let's Encrypt. Routing is a pure reverse proxy:

```
authentik.91-107-194-138.nip.io  →  authentik-server-1:9000
llm.91-107-194-138.nip.io        →  router:4000
plane.91-107-194-138.nip.io      →  plane-plane-proxy-1:80
```

`llm.` additionally has a dedicated route that forces `Cache-Control:
no-cache, must-revalidate` on the one UI bundle chunk we patch in place
(§2.3 `ui-btn-patched.js`), so stale browser caches refresh after a deploy.

Caddy joins the three backend Docker networks via `external: true`
declarations in `/opt/gateway/docker-compose.yaml` (`litellm_backend`,
`plane_backend`, `authentik_backend`).

## 3. Sticky routing (`sticky_router.py`)

A `CustomLogger` subclass whose `async_pre_call_hook` runs before every
request. It rewrites the three unsuffixed Claude model names into their
`-a` / `-b` siblings based on a stable hash of the caller's identity:

```
if model in {claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5}:
    identity = first non-None of (user_id, team_id, api_key, "anon")
    h        = int(md5(identity).hexdigest(), 16)   # hex → int
    suffix   = "-a" if (h & 1) == 0 else "-b"       # low bit picks worker
    data["model"] = model + suffix
```

Effect:
- Same user consistently lands on the same worker / OAuth subscription →
  Anthropic's per-org prompt cache hits across multi-turn sessions.
- Over many users the hash spreads ~50/50, so aggregate subscription quota
  is used evenly on average. At small team sizes the variance matters:
  with three members there's roughly a 1-in-4 chance all three hash to the
  same worker until a fourth user joins.
- `-a` routes to `worker1`, `-b` routes to `worker2` (hard-coded in
  `model_list`).

Historical note: an earlier attempt used `model_info.id` +
`specific_deployment=True` to avoid exposing `-a`/`-b` at all; the parameter
is honoured by LiteLLM's Python SDK but not by its HTTP proxy in 1.82.3, so
suffixed routing targets remain the simplest working shape.

### 3.1 Failover

`router_settings.fallbacks` lists six `-a ↔ -b` cross-pairs. On 429 / 5xx /
timeout, a request hashed to `-a` transparently retries on `-b` (and vice
versa). Verified by stopping `worker1` and firing an unsuffixed request —
the fallback on `worker2` returned correctly in ~4 s. The fallback response
is a cache miss on the alternate subscription, so the first fallback request
pays full input cost, but the user is unblocked.

`cooldown_time: 1800` in `router_settings` pulls a failed deployment out of
the pool for 30 minutes once `allowed_fails` (currently 1) is tripped —
prevents rapid hot-looping against a rate-limited sub.

## 4. Access control

### 4.1 SSO login flow

```
user → llm.91-107-194-138.nip.io/sso/key/generate
  ↓ 303
Authentik: /application/o/authorize/
  ↓
Authentik GitHub source (OAuth App id Ov23lixln1F8tDVh4dJJ)
  ↓
GitHub: user authorises the app
  ↓
Authentik callback: receives GitHub identity
  ↓
gh-collab-gate policy evaluates:
    GET /repos/ComposioHQ/agent-orchestrator/collaborators/<gh-login>
      204 → pass
      404 → deny with user-facing message; flow terminates
  ↓ pass
enrollment flow (if new user) or link to existing user by email
  ↓
LiteLLM receives OIDC token
  ↓
LiteLLM creates an internal user + virtual key, scoped to the 19 public
  models (§4.3) via litellm_settings.default_internal_user_params.models
  ↓
user redirected to /ui/ with session cookie + virtual key
```

### 4.2 gh-collab-gate policy

Expression policy in Authentik (`name: gh-collab-gate`). On every evaluation,
calls `GET /repos/ComposioHQ/agent-orchestrator/collaborators/{username}`:
- **204** → direct collaborator → allow
- **404** → not a collaborator → deny with user-facing message
- other → deny + log warning (rare — typically a GitHub API outage)

Bound at two points:
1. **LiteLLM application** (order 0) — runs on each SSO login to LiteLLM. If
   someone is removed as a collaborator, their next login is denied.
   Existing sessions and issued virtual keys still work until explicitly
   revoked (§5.4). See §6.2 for the plan to close this gap.
2. **GitHub enrollment flow** (order 0) — runs before user creation. A
   non-collaborator never gets an Authentik user, so there's no leaked
   account to clean up later.

**The PAT used for the API call is inlined in the policy expression itself**
— the Authentik DB is the system of record. Any Authentik admin can view /
rotate it through the admin UI or `PATCH /api/v3/policies/expression/<pk>/`.
See §5.9 for the rotation recipe.

### 4.3 Scoped virtual keys — the 19 public models

Two key-level settings control what a team member sees in `/v1/models`:

1. **`TEAM_KEY_HARSH`** (the bootstrap operator key) has its `models` list
   pinned to the same 19 names via a one-time `/key/update`.
2. **`default_internal_user_params.models`** in `config-router.yaml` sets the
   same list for auto-provisioned users — when an SSO user is created, their
   key inherits the 19-model scope.

The complete public set (also visible via `GET /v1/models` with any team
key):

- **Claude (3)**: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`
- **GLM via z.ai (12)**: `glm-5.1`, `glm-5`, `glm-5-turbo`, `glm-4.7`,
  `glm-4.7-flash`, `glm-4.7-flashx`, `glm-4.6`, `glm-4.6v`, `glm-4.5`,
  `glm-4.5-air`, `glm-4.5-flash`, `glm-4.5v`
- **ChatGPT Codex OAuth (4)**: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`,
  `gpt-5.2`

Internal `claude-*-a` / `claude-*-b` entries exist in `model_list` as sticky
routing targets but are **not** in any team member's `allowed_models`.
Direct requests for them return HTTP 403 `key_model_access_denied`. The
sticky hook's rewrite survives the access check because LiteLLM evaluates
access *before* `async_pre_call_hook`.

## 5. Operations

### 5.1 Where secrets live on the server
The runtime reads all its secrets from on-disk locations owned by the host
itself. An operator does **not** need to configure an external secret store.

| Location | Keys |
|---|---|
| `/opt/litellm-lb/.env` (root-owned, mode 600) | `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `ZAI_API_KEY`, and the OIDC connection settings that point LiteLLM at Authentik (endpoints, client id, client secret). |
| `/opt/authentik/.env` (root-owned, mode 600) | Authentik's bootstrap secrets (admin password, postgres password, `AUTHENTIK_SECRET_KEY`). |
| `/opt/litellm-lb/chatgpt-data/auth.json` (aoagent-owned, mode 600, git-ignored) | Codex CLI OAuth token used by the `chatgpt/*` models. |
| Inline in the Authentik `gh-collab-gate` policy expression | The GitHub PAT the policy uses at login time. Source of truth; rotate via §5.9. |
| Docker named volumes `worker1-claude`, `worker2-claude` | Each worker's Claude subscription credentials (`/home/claude/.claude/.credentials.json`). |

An operator needs, separately, their own copies of:
- SSH access to `91.107.194.138` as an admin user (to edit anything)
- `LITELLM_MASTER_KEY` (for every admin API call to LiteLLM in this section)
- Authentik admin token (for policy changes — §5.9)

These can live in whatever secret store the operator prefers; how they're
kept is not the deployment's concern.

The rest of this section assumes the operator has `$LITELLM_MASTER_KEY`
exported in their shell. Substitute however you retrieve it.

### 5.2 Add a new team member
The only action required is **adding them as a collaborator on
`ComposioHQ/agent-orchestrator`**. On their next visit to
`https://llm.91-107-194-138.nip.io/`:
1. Authentik gates them through the gh-collab-gate policy → pass.
2. Enrollment flow creates an Authentik user matching their GitHub login.
3. LiteLLM creates an internal user + virtual key using
   `default_internal_user_params.models` (§4.3) — scoped to the 19 public
   models.
4. They land in the LiteLLM UI where they can copy their key.

No manual provisioning, no per-user keys to stash anywhere.

### 5.3 Client configuration (what the team member does with the key)

**Claude Code** (most common):
```bash
export ANTHROPIC_BASE_URL=https://llm.91-107-194-138.nip.io
export ANTHROPIC_AUTH_TOKEN=sk-…         # the virtual key from the UI
export ANTHROPIC_MODEL=claude-opus-4-7
export ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5
claude -p "hello"
```

**opencode** — provider block in `~/.config/opencode/opencode.json`:
```json
{
  "provider": {
    "team-lb": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://llm.91-107-194-138.nip.io/v1",
        "apiKey": "sk-…"
      },
      "models": {
        "claude-opus-4-7": {},
        "claude-sonnet-4-6": {},
        "claude-haiku-4-5": {}
      }
    }
  }
}
```

**Raw `curl`** for debugging:
```bash
curl https://llm.91-107-194-138.nip.io/v1/messages \
  -H "Authorization: Bearer sk-…" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":64,
       "messages":[{"role":"user","content":"ping"}]}'
```

### 5.4 Add a new upstream model
Edit `/opt/litellm-lb/config-router.yaml`. For a simple direct-to-provider
model, append one block to `model_list:` (see existing `glm-*` or `gpt-*`
entries as templates). For a Claude model that should get sticky-routing +
failover, add **four** entries: two with the unsuffixed `model_name` (one
per worker, for the hook to route between), one `-a` entry pinned to
`worker1`, and one `-b` entry pinned to `worker2`. If the new provider
needs fresh credentials, add them to `/opt/litellm-lb/.env`.

Also extend `litellm_settings.default_internal_user_params.models` so
existing SSO users see the new model on their next `/v1/models` call.
Already-issued virtual keys are unaffected unless updated via `/key/update`.

Apply with `sudo docker compose up -d --force-recreate router` (full
restart recipe in §5.7).

### 5.5 Revoke a user
```bash
curl -X POST https://llm.91-107-194-138.nip.io/user/block \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -d '{"user_id":"<UUID>"}'
```
The `user_id` is the UUID returned by `/user/list` (below) — the internal
user's `pk`, not their email.

LiteLLM's in-memory user-auth cache has a **60 s TTL** (default, set at
`litellm/proxy/proxy_server.py:1114`; override via
`general_settings.user_api_key_cache_ttl`). A block propagates within about
one minute. Unblock via `/user/unblock`. See §6.2 for the plan to trigger
this automatically on GitHub `member.removed`.

### 5.6 Inspect state
- **List users**: `GET /user/list` with the master key → all internal users,
  UUIDs, their virtual keys, scopes, budget state.
- **Spend per key**: `GET /spend/logs?api_key=<sk-…>` → time-series of
  requests with model, in/out tokens, cost per row.
- **Key info**: `GET /key/info?key=<sk-…>` → one key's `models`,
  `max_budget`, `expires`, `blocked` state.
- **Model list for a key**: `GET /v1/models` with that key as Bearer →
  filtered by the key's `allowed_models`.
- **Live metrics** (once a scraper is configured, §6.5): `GET /metrics` on
  the router.

**Cost in spend logs is cosmetic on the Claude path.** The workers forward
through an OAuth subscription, so there's no per-request dollar cost to
Anthropic — the `$` column in `/spend/logs` is LiteLLM applying the public
pay-as-you-go pricing against token counts. It's useful for comparing users'
relative consumption and for budget-cap accounting, not for forecasting
real spend. GLM (z.ai) and ChatGPT (Codex) are similarly subscription-billed
today, so the same caveat applies.

### 5.7 Edit live config
The server's `/opt/litellm-lb/` is not a git checkout (§6.3). Edits are made
in-place and will show up as drift on the next deploy. `sudo` is needed for
`docker compose` because `.env` is root-owned (mode 600). After any edit:
```bash
cd /opt/litellm-lb
sudo docker compose up -d --force-recreate router
docker logs --tail 100 litellm-lb-router-1
curl https://llm.91-107-194-138.nip.io/health/readiness | jq
```
The `/health/readiness` response should show `status: healthy` with
`StickyRouter` among `success_callbacks`.

### 5.8 Rollback a broken config
When a config change causes the router to fail to start, it keeps
crash-looping. Today's recovery path (§6.3 will improve this once the server
becomes a git checkout):
```bash
# On the laptop — the repo is the only source of a known-good version:
git show HEAD:config-router.yaml > /tmp/good.yaml
scp /tmp/good.yaml aoagents:/tmp/

# On the server:
ssh aoagents 'cd /opt/litellm-lb && sudo cp /tmp/good.yaml config-router.yaml \
    && sudo docker compose up -d --force-recreate router'
```

### 5.9 Rotate the gh-collab-gate PAT
1. Generate a new GitHub PAT — fine-grained, scoped to
   `ComposioHQ/agent-orchestrator`, **Collaborators: Read** only (resolves
   §6.1).
2. `PATCH /api/v3/policies/expression/<pk>/` on Authentik with the new
   expression body — the PAT is a Python string constant near the top of
   the policy. Use the Authentik admin token for authorisation.
3. Test via `POST /api/v3/policies/all/<pk>/test/` with a known collaborator
   and a known non-collaborator (`octocat` is the canonical negative probe —
   GitHub mascot, not a collaborator on the repo).
4. Record the new PAT wherever you keep operational secrets — there is no
   runtime dependency on any particular secret store, but the operator
   still needs a copy for the next rotation.

## 6. Known gaps / what's left

### 6.1 Policy PAT is tied to a personal CLI token
The GitHub PAT inside the `gh-collab-gate` policy is currently a personal
`gh auth token` that rotates whenever the GitHub CLI refreshes its session.
Rotate to a fine-grained repo-scoped PAT or a GitHub App installation
token — narrower blast radius and decoupled from any individual's CLI
session. Rotation recipe in §5.9.

### 6.2 No instant revocation on collaborator removal
Removing a collaborator on GitHub blocks their **next** SSO login, but
their existing virtual key keeps working until a manual `/user/block` call.
Plan:
- Deploy `adnanh/webhook` as a systemd service on `aoagents`.
- One hook handler: `github:member.removed` → shell script → `POST
  /user/block`.
- HMAC secret stored in `/opt/webhook/.env` (root-owned, mode 600) —
  matches the rest of the stack's pattern.
- Public endpoint at `hooks.91-107-194-138.nip.io`, fronted by the existing
  Caddy.
- LiteLLM's 60 s user-auth cache picks up the block automatically.
- Defence-in-depth: a daily observe-only cron that **logs** (but does not
  act on) drift between GitHub collaborators and LiteLLM users — alerts on
  silent webhook failure without risking wrongful blocks from cron-side
  bugs.

### 6.3 `/opt/litellm-lb` drift vs. the repo
The server directory is a copy of this repo without a `.git/` inside.
PR #1 (`deploy/sticky-router`, open at the time of writing) brings the repo
in sync with the server for the pieces that belong in version control.
Next steps:
- Merge PR #1 into `main`.
- Convert `/opt/litellm-lb` into a tracking checkout (`git init` + `git
  remote add` + `git reset --mixed origin/main`), OR switch to a
  pull-based deploy from the laptop. Either makes `git status` a
  drift-detector and `git pull` a deploy primitive.
- `ui-btn-patched.js` exists on the server but is not in the repo — either
  vendor it in or document it as a one-off runtime patch. Right now it's
  neither.

### 6.4 Onboarding untested end-to-end
Every step has been verified in isolation:
- Authentik policy test endpoint returns `passing=False` for `octocat`.
- `/user/new` inherits the right `allowed_models` from
  `default_internal_user_params`.
- SSO redirect from LiteLLM returns a valid Authentik URL.

Not yet verified: a GitHub account other than the current maintainer's
going through the full SSO flow — only one human user exists in Authentik
at the time of writing. First real onboarding will exercise:
- The Authentik "access denied" page rendering for a policy-denied user.
- The enrollment flow creating a fresh Authentik user when the gate passes.
- LiteLLM auto-provisioning with the scoped model list.

### 6.5 No monitoring / alerting yet
Prometheus callbacks are wired in `config-router.yaml` (`success_callback`,
`failure_callback`, `service_callback`) and the router exposes `/metrics`.
**Nothing scrapes it today.** Before the team actually relies on this, at
minimum add:
- A Prometheus scraper (or push-gateway client) reading `/metrics`.
- Alerts on: router container down, worker 429 rate above threshold,
  Anthropic 5xx rate above threshold, per-user spend approaching a budget
  cap.

### 6.6 No budget caps
`default_internal_user_params` doesn't set `max_budget`. Today a compromised
virtual key could burn unlimited quota on any subscription. Add per-user
`max_budget` with a reset window once normal-usage baseline is known.

### 6.7 Upstream issues we're tolerating

Persistent (known bugs / design limits):
- **GPT non-streaming 500** — documented LiteLLM bug; clients must use
  `stream: true` with any `gpt-*` model.

Transient (may self-heal):
- **GLM long-output latency** — `glm-*` models on `api.z.ai` occasionally
  time out when `max_tokens` is around 50+. Unrelated to this LB's wiring;
  upstream provider issue.

## 7. Repo ↔ server relationship

- **Repo** (this one): canonical. Owns the worker image build, the router
  config template, the sticky hook, the compose file, this document.
- **Server** (`/opt/litellm-lb/`): the same files plus deployment-specific
  artefacts — `.env` with real secrets, `chatgpt-data/auth.json` with the
  Codex OAuth token, `ui-btn-patched.js` for a router UI patch.
  **Not a git checkout today** (§6.3).

PR #1 (`deploy/sticky-router`) brings the repo in sync with server reality
for the pieces that belong in version control. Once merged and §6.3 is
done, `git pull` on the server becomes the deploy primitive and
`git status` becomes the drift-detector.

---

Collaborators on `ComposioHQ/agent-orchestrator` inherit LiteLLM access via
the gh-collab-gate policy; the access list lives in GitHub, not in this
repo or Authentik.

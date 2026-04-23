# Team LiteLLM deployment — `aoagents` (91.107.194.138)

End-to-end architecture, deployment recipe, operations runbook, and open items
for the LiteLLM instance that serves the team. The `litellm-lb` repo holds the
code; this document describes how that code is wired into a production stack on
the Hetzner host at `91.107.194.138` (DNS: `llm.91-107-194-138.nip.io`).

## 1. High-level picture

```
  team member (browser / CLI)
     │
     ▼  TLS :443
  Caddy edge ─────────────┐
     │                    │
     │                    ▼
     │              Authentik  ──► GitHub OAuth App
     │              (OIDC + gh-collab-gate policy)
     │
     ▼  :4000
  LiteLLM router
     ├─ auth           : virtual-key + scoped allowed_models
     ├─ sticky_router  : hash(identity) → worker (cache stickiness)
     ├─ fallbacks      : -a ↔ -b on 429 / 5xx / timeout
     │
     │   routed to ┌─► worker1 ──► api.anthropic.com (subscription A)
     │             └─► worker2 ──► api.anthropic.com (subscription B)
     │
     ▼
  PostgreSQL: users, keys, budgets, spend
```

- **Caddy** terminates TLS and reverse-proxies three vhosts (§2.4).
- **Authentik** is the OIDC provider; an expression policy gates by GitHub
  repository collaborator status (§4.2).
- **LiteLLM router** owns the public model API (`/v1/messages`,
  `/v1/chat/completions`, `/v1/models`, `/ui/`). Does auth, access scoping,
  routing, budget tracking, spend logs.
- **Workers** are thin FastAPI processes (191 LOC in
  `worker-image/oauth_proxy.py`) that hold one Claude OAuth subscription each
  and forward verbatim to `api.anthropic.com`, swapping the Authorization
  header for the OAuth Bearer. Horizontal capacity scales with the number of
  subscriptions.
- **Postgres** stores LiteLLM users / keys / budgets / spend. Not shared.

See `README.md` for the router / worker design rationale (slim workers, no
nested LiteLLM, prompt-cache benefits).

## 2. Deployed components

### 2.1 Host
- IP `91.107.194.138`, Hetzner Cloud vServer, KVM-virtualised
- 4 vCPU (AMD EPYC), 7.6 GiB RAM, 150 GB disk
- Ubuntu 24.04 LTS, kernel 6.8, Docker 28.x
- Two human users: `aoagent` (app/operations owner), `harsh` (admin, in
  `sudo` / `docker` / `adm`)
- Live deploy directory is `/opt/litellm-lb/` — a loose copy of this repo, not
  a git checkout. See §7 for the implication.

### 2.2 Docker stacks under `/opt`
| Stack | Purpose | Public vhost |
|---|---|---|
| `gateway`    | Caddy edge + TLS (Let's Encrypt) | `*.91-107-194-138.nip.io` |
| `authentik`  | OIDC IdP, policy engine          | `authentik.91-107-194-138.nip.io` |
| `litellm-lb` | Router + workers + postgres      | `llm.91-107-194-138.nip.io` |
| `plane`      | Team project management          | `plane.91-107-194-138.nip.io` |

External listeners: 22 (SSH), 80/443 (Caddy), 9000/9443 (Portainer). All
inter-container traffic runs on internal Docker networks.

### 2.3 LiteLLM router stack (`litellm-lb`)
Containers:
- `litellm-lb-router-1` — `ghcr.io/berriai/litellm:main-stable`, listens :4000
- `litellm-lb-worker1-1`, `litellm-lb-worker2-1` — `litellm-lb-oauth-proxy:local`
  (built from `worker-image/`), each :4000 internal-only
- `litellm-lb-db-1` — `postgres:16`

Key router mounts (`docker-compose.yaml`):

| Mount | Purpose |
|---|---|
| `./config-router.yaml` → `/app/config.yaml` | Model list, routing strategy, fallbacks, callbacks, default user scope. The main operator-editable file. |
| `./sticky_router.py` → `/app/sticky_router.py` | Registered as `litellm_settings.callbacks` — the per-user hook (§3) |
| `./chatgpt-data/auth.json` → `/app/chatgpt-data/auth.json` | ChatGPT OAuth token used by the `chatgpt/*` models (Codex backend). Git-ignored. |
| `./ui-btn-patched.js` → `/usr/lib/…/80899acb7e1a7640.js` | In-place patch to the LiteLLM UI bundle. Server-side-only; not tracked in git (§6.3). |

Each worker mounts a named volume `workerN-claude` at `/home/claude/.claude/`
containing that subscription's `.credentials.json`. Tokens have
`expiresAt: 2099-12-31`, so effectively non-expiring.

### 2.4 Caddy vhosts (`/opt/gateway/Caddyfile`)
Three externally-reachable hostnames, all automatically TLS-terminated via
Let's Encrypt (email `lifeos@pkarnal.com`). Routing is a pure reverse proxy:

```
authentik.91-107-194-138.nip.io  →  authentik-server-1:9000
llm.91-107-194-138.nip.io        →  router:4000
                                    (plus a route that forces no-cache on the
                                    one in-place-patched UI chunk so stale
                                    browser caches refresh after deploys)
plane.91-107-194-138.nip.io      →  plane-plane-proxy-1:80
```

Caddy joins the three backend Docker networks via `external: true`
declarations in `/opt/gateway/docker-compose.yaml` (`litellm_backend`,
`plane_backend`, `authentik_backend`).

## 3. Sticky routing (`sticky_router.py`)

A `CustomLogger` subclass whose `async_pre_call_hook` runs before every
request. It rewrites the three unsuffixed Claude model names into their
`-a` / `-b` siblings based on a stable hash of the caller's identity:

```
model in {claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5}
  ↓
identity = request.user_id | team_id | api_key | "anon"
  ↓
suffix   = hash(identity) % 2 ? "-b" : "-a"
  ↓
data["model"] = model + suffix
```

Effect:
- Same user consistently lands on the same worker / OAuth subscription →
  Anthropic's prompt cache (per-org) hits across multi-turn sessions.
- Different users deterministically spread across the two workers → aggregate
  subscription quota is used evenly.
- `-a` routes to `worker1`, `-b` routes to `worker2` (hard-coded in
  `model_list`).

Historical note: we tried using `model_info.id` + `specific_deployment=True`
to avoid exposing `-a`/`-b` at all; the parameter is honoured by LiteLLM's
Python SDK but not by its HTTP proxy in 1.82.3, so suffixed routing targets
remain the simplest working shape.

### 3.1 Failover

`router_settings.fallbacks` lists six `-a ↔ -b` cross-pairs. On 429 / 5xx /
timeout, a request hashed to `-a` transparently retries on `-b` (and vice
versa). Verified by stopping `worker1` and firing an unsuffixed request — the
fallback on `worker2` returned correctly in ~4 s. The fallback response is a
cache miss on the alternate subscription, so the first fallback request pays
full input cost, but the user is unblocked.

`cooldown_time: 1800` in `router_settings` pulls a failed deployment out of
the pool for 30 minutes after `allowed_fails: 1` failures — prevents rapid
hot-looping against a rate-limited sub.

## 4. Access control

### 4.1 SSO login flow

```
user → llm.91-107-194-138.nip.io/sso/key/generate
  ↓ 303
Authentik: /application/o/authorize/
  ↓
Authentik GitHub source (OAuth App Ov23lixln1F8tDVh4dJJ)
  ↓
GitHub: user authorizes
  ↓
Authentik callback: receive GitHub identity
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
LiteLLM creates internal user + virtual key using
  litellm_settings.default_internal_user_params.models → scoped to
  the 19 public models (no -a/-b leakage)
  ↓
user redirected to /ui/ with session cookie + virtual key
```

### 4.2 gh-collab-gate policy

Expression policy in Authentik (`name: gh-collab-gate`). On every evaluation:
`GET /repos/ComposioHQ/agent-orchestrator/collaborators/{username}`:
- **204** → direct collaborator → allow
- **404** → not a collaborator → deny with user-facing message
- other → deny + log warning (rare — typically a GitHub API outage)

Bound at two points:
1. **LiteLLM application** (order 0) — runs on each SSO login to LiteLLM. If
   someone is removed as a collaborator, their next login is denied. Existing
   sessions and issued virtual keys still work until explicitly revoked
   (§5.4). See §6.2 for the plan to close this gap.
2. **GitHub enrollment flow** (order 0) — runs before user creation. A
   non-collaborator never gets an Authentik user in the first place, which
   means there's no leaked account to clean up later.

The PAT used for the API call is currently `gh auth token`
(`harsh-batheja`'s GitHub CLI token). Stored inline in the policy expression
(Authentik admins can view it) and mirrored to Infisical at
`/servers/91-107-194-138 GITHUB_COLLAB_PAT` for continuity.

### 4.3 Scoped virtual keys

Two key-level changes make `/v1/models` show only the unsuffixed public names
to a team member:

1. **`TEAM_KEY_HARSH`** was updated with
   `models: [3 claude + 12 glm + 4 gpt]` via `/key/update`.
2. **`default_internal_user_params.models`** in `config-router.yaml` sets the
   same list for auto-provisioned users — when an SSO user is created, their
   key inherits the 19-model scope.

The hook's rewrite from `claude-haiku-4-5` → `claude-haiku-4-5-a` survives
because LiteLLM's access check runs *before* `async_pre_call_hook`. Direct
requests for `-a`/`-b` from a scoped key return HTTP 403
`key_model_access_denied`.

## 5. Operations

### 5.1 Credentials & secrets

| Location | Keys |
|---|---|
| Infisical `/servers/91-107-194-138` | `LITELLM_MASTER_KEY`, `TEAM_KEY_HARSH`, `HARSH_PASSWORD`, `ROOT_PASSWORD`, `AUTHENTIK_ADMIN_TOKEN`, `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_ADMIN_PASSWORD`, `AUTHENTIK_POSTGRES_PASSWORD`, `AUTHENTIK_LITELLM_CLIENT_SECRET`, `AUTHENTIK_PLANE_CLIENT_SECRET`, `POSTGRES_PASSWORD`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_COLLAB_PAT`, `DEPLOY_SSH_PRIVATE_KEY` |
| `/opt/litellm-lb/.env` (root-owned, mode 600) | `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `ZAI_API_KEY`, OIDC connection settings for Authentik (endpoints, client id, client secret) |
| `/opt/authentik/.env` (root-owned, mode 600) | Authentik's bootstrap secrets (admin password, postgres, secret key) |
| `/opt/litellm-lb/chatgpt-data/auth.json` (aoagent-owned, mode 600, git-ignored) | Codex CLI OAuth token used by `chatgpt/*` models |

Retrieve any Infisical secret with `iget /servers/91-107-194-138 <KEY>` from
the laptop.

### 5.2 Add a new team member
The only action required is **adding them as a collaborator on
`ComposioHQ/agent-orchestrator`**. On their next visit to
`https://llm.91-107-194-138.nip.io/`:
1. Authentik gates them through the gh-collab-gate policy → pass.
2. Enrollment flow creates an Authentik user matching their GitHub login.
3. LiteLLM creates an internal user + virtual key using
   `default_internal_user_params.models` (§4.3) — scoped to the 19 public
   models.
4. They land in the LiteLLM UI where they can copy their key for Claude Code
   / opencode / etc.

No manual provisioning, no per-user keys to stash anywhere.

### 5.3 Add a new upstream model
Edit `/opt/litellm-lb/config-router.yaml` (`harsh`-owned). For a simple
direct-to-provider model, append one block to `model_list:` (see existing
`glm-*` or `gpt-*` entries as templates). For a Claude model that should get
sticky-routing + failover, add **four** entries: two unsuffixed (routed by
the hook) and two `-a`/`-b` pairs pinned to the workers.

Also extend `litellm_settings.default_internal_user_params.models` so
existing SSO users see the new model on their next `/v1/models` call.
Existing issued keys are unaffected unless explicitly updated via
`/key/update`.

Restart path in §5.6.

### 5.4 Revoke a user
Today (explicit):
```bash
curl -X POST https://llm.91-107-194-138.nip.io/user/block \
    -H "Authorization: Bearer $(iget /servers/91-107-194-138 LITELLM_MASTER_KEY)" \
    -d '{"user_id":"<pk-or-email>"}'
```
LiteLLM's in-memory user-auth cache has a **60 s TTL** (default, set at
`litellm/proxy/proxy_server.py:1114`; configurable via
`general_settings.user_api_key_cache_ttl`). So a block propagates within one
minute. Unblock via `/user/unblock`. See §6.2 for the plan to trigger this
automatically on GitHub `member.removed`.

### 5.5 Inspect state
- **List users**: `GET /user/list` with the master key → returns all internal
  users, their keys, scopes, budget state.
- **Spend per key**: `GET /spend/logs?api_key=<sk-...>` → time-series of
  requests with model, in/out tokens, cost per row.
- **Model list for a key**: `GET /v1/models` with that key as Bearer → filters
  by the key's `allowed_models`.
- **Live metrics** (when a Prometheus scraper is configured, §6.6):
  `GET /metrics` on the router.

### 5.6 Edit live config
The server's `/opt/litellm-lb/` is not a git checkout (§7). Config edits are
made in-place and show up as drift on next deploy. After any edit:
```bash
cd /opt/litellm-lb
sudo docker compose up -d --force-recreate router
docker logs --tail 100 litellm-lb-router-1
curl https://llm.91-107-194-138.nip.io/health/readiness | jq
```
The `/health/readiness` response should show `status: healthy` with
`StickyRouter` among `success_callbacks`.

### 5.7 Rollback a broken config
When a config change causes the router to fail to start, it keeps crash-
looping with the new config. Recovery:
```bash
cd /opt/litellm-lb
git log --oneline -- config-router.yaml            # pick a prior known-good sha
git show <sha>:config-router.yaml > /tmp/good.yaml # extract it
# Review /tmp/good.yaml, then:
sudo cp /tmp/good.yaml config-router.yaml
sudo docker compose up -d --force-recreate router
```
(Prerequisite: `/opt/litellm-lb` must have been converted to a tracking git
checkout — §6.3.) Until then, fall back to `git show HEAD:config-router.yaml
> /tmp/good.yaml` from the laptop repo, then `scp` it across.

### 5.8 Rotate `GITHUB_COLLAB_PAT`
1. Generate a fine-grained PAT on `ComposioHQ/agent-orchestrator` scoped to
   **Collaborators: Read** only (resolves §6.1).
2. Update Infisical:
   `iset /servers/91-107-194-138 GITHUB_COLLAB_PAT <new-value>`.
3. Update the Authentik policy expression (PATCH
   `/api/v3/policies/expression/<pk>/`) — the PAT is inlined near the top.
4. Test via `POST /api/v3/policies/all/<pk>/test/` with a known collaborator
   and a known non-collaborator (`octocat` — GitHub mascot, not a collaborator
   on the repo — is the canonical negative probe).

## 6. Known gaps / what's left

### 6.1 Personal CLI token inside the Authentik policy
`gh auth token` (harsh-batheja's) is pinned inline today. Rotate to a
dedicated fine-grained repo-scoped PAT or a GitHub App installation token —
reduces blast radius and removes the dependency on one person's CLI session.
Rotation recipe in §5.8.

### 6.2 No instant revocation on collaborator removal
Removing a collaborator on GitHub blocks their **next** SSO login, but their
existing virtual key keeps working until a manual `/user/block` call. Plan:
- Deploy `adnanh/webhook` as a systemd service on `aoagents`.
- One hook handler: `github:member.removed` → shell script → `POST
  /user/block`.
- HMAC secret in Infisical at `/servers/91-107-194-138 GITHUB_WEBHOOK_SECRET`.
- Public endpoint at `hooks.91-107-194-138.nip.io`, fronted by existing
  Caddy.
- LiteLLM's 60 s user-auth cache picks up the block automatically.
- Defence-in-depth: a daily observe-only cron that **logs** (but does not act
  on) drift between GitHub collaborators and LiteLLM users — alerts on silent
  webhook failure without risking wrongful blocks from cron-side bugs.

### 6.3 `/opt/litellm-lb` drift vs. the repo
The server directory is a loose copy of `github.com/harsh-batheja/litellm-lb`
without a `.git/` inside. All changes we pushed to the branch
`deploy/sticky-router` were lifted from the server by `scp`. Going forward:
- Merge PR #1 (`deploy/sticky-router`) into `main`.
- Convert `/opt/litellm-lb` into a tracking checkout (`git init` + `git
  remote add` + `git reset --mixed origin/main`), OR switch to a
  pull-based deploy from the laptop. Either makes `git status` a
  drift-detector and `git pull` a deploy primitive.
- `ui-btn-patched.js` is on the server but not in the repo — either vendor
  it in or document it as a one-off runtime patch (right now it's neither).

### 6.4 Onboarding untested end-to-end
Every step has been verified in isolation:
- Authentik policy test endpoint returns `passing=False` for `octocat`.
- `/user/new` inherits the right `allowed_models` from
  `default_internal_user_params`.
- SSO redirect from LiteLLM returns a valid Authentik URL.

Not yet verified: a GitHub account **other than** `harsh-batheja` going
through the full SSO flow (the only human user in Authentik today is me).
First real onboarding (Adil / Dhruv) will exercise:
- The Authentik "access denied" page rendering for a policy-denied user.
- The enrollment flow creating a fresh Authentik user when the gate passes.
- LiteLLM auto-provisioning with the scoped model list.

### 6.5 No monitoring / alerting yet
Prometheus callbacks are wired in `config-router.yaml`
(`success_callback`, `failure_callback`, `service_callback`) and the router
exposes `/metrics`. **Nothing scrapes it today.** Before the team actually
relies on this, at minimum add:
- A Prometheus scraper (or push-gateway client) reading `/metrics`.
- Alerts on: router container down, worker 429 rate above threshold,
  Anthropic 5xx rate above threshold, per-user spend approaching a budget
  cap.

### 6.6 No budget caps
`default_internal_user_params` doesn't set `max_budget`. Today a compromised
key could burn unlimited quota on any subscription. Add per-user
`max_budget` with a reset window once we have signal on normal usage.

### 6.7 Upstream issues we're tolerating

Persistent (known bugs / design limits):
- **GPT non-streaming 500** — documented LiteLLM bug; clients must use
  `stream: true` with any `gpt-*` model.
- **GPT prompt inflation** — LiteLLM's `chatgpt/` provider unconditionally
  prepends the Codex CLI system prompt (~1.6 k tokens, 1.28 k of which are
  cached). No user dollar cost (subscription path), but Codex subscription
  quota burns faster. A real Codex CLI client pointed at this LB would pay
  the prompt roughly **twice** — once by LiteLLM's unconditional injection,
  once by its own. Recommendation: don't point the Codex CLI here; use
  it directly against OpenAI. Fix would require forking the chatgpt provider
  or waiting for an upstream patch.

Transient (may self-heal):
- **GLM long-output latency** — `glm-*` models on `api.z.ai` occasionally
  time out when `max_tokens` is around 50+. Unrelated to this LB's wiring;
  upstream provider issue.

## 7. Repo ↔ server relationship

- **Repo** (`github.com/harsh-batheja/litellm-lb`): canonical. Owns the
  worker image build, the router config template, the sticky hook, the
  compose file, this document.
- **Server** (`/opt/litellm-lb/`): same files plus deployment-specific
  artefacts — `.env` with real secrets, `chatgpt-data/auth.json` with the
  Codex OAuth token, `ui-btn-patched.js` for a router UI patch.
  **Not a git checkout today.**

PR #1 (`deploy/sticky-router`) brings the repo in sync with the server for
the pieces that belong in version control. Once merged and §6.3 is done,
`git pull` on the server becomes the deploy primitive and `git status`
becomes the drift-detector.

---

**Maintainers**: `harsh-batheja` (primary). Collaborators on
`ComposioHQ/agent-orchestrator` inherit LiteLLM access via the gh-collab-gate
policy; the access list lives in GitHub, not in this repo or Authentik.

# Team LiteLLM deployment — `aoagents` (91.107.194.138)

End-to-end architecture, deployment recipe, operations runbook, and open items
for the LiteLLM instance that serves the team. The `litellm-lb` repo holds the
code; this document describes how that code is wired into a production stack on
the Hetzner host at `91.107.194.138` (DNS: `llm.91-107-194-138.nip.io`).

## 1. High-level picture

```
                                                                 ┌───────────────────────────────┐
                            GitHub                               │   api.anthropic.com           │
                               │                                 │   (Claude OAuth subs)         │
        (1) SSO login:         │                                 └──────────▲──────────▲─────────┘
        /sso/key/generate  ────┼──► Authentik ─┐                            │          │
                               │  (OIDC, gate) │                            │ oauth    │ oauth
                               │               │                            │          │
 team member ──browser/CLI──► Caddy ──────► LiteLLM ◄────────►  worker1   worker2
                    (tls)     edge   (router, /v1/*)  specific  (FastAPI    (FastAPI
                              :443                    deployment shim)       shim)
                                                      selected by           
                                                      sticky_router hook     
                                                                              
                                            PostgreSQL (LiteLLM state: users, keys, budgets, spend)
```

- **Caddy** terminates TLS and does plain L4 reverse-proxying on three vhosts
  (`llm.`, `authentik.`, `plane.`).
- **Authentik** is the OIDC provider; a custom expression policy gates access
  to repository collaborators.
- **LiteLLM router** owns the public model API (`/v1/messages`,
  `/v1/chat/completions`, `/v1/models`, `/ui/`). It does auth, routing, budget
  tracking, spend logs.
- **Workers** are thin FastAPI processes (~150 lines each) that hold a single
  Claude OAuth subscription and forward verbatim to `api.anthropic.com` with
  the OAuth Bearer header swapped in. One worker per subscription.
- **Postgres** stores LiteLLM users/keys/budgets/spend and is not shared with
  anything else.

See `README.md` for the router/worker design rationale (slim workers, no
nested LiteLLM, prompt-cache benefits).

## 2. Deployed components

### 2.1 Host
- `91.107.194.138`, Hetzner Cloud CPX31 shape (4 vCPU EPYC, 8 GB RAM, 160 GB)
- Ubuntu 24.04 LTS, kernel 6.8, Docker 28.x
- Two human users: `aoagent` (app/operations), `harsh` (admin, in `sudo`/`docker`/`adm`)
- `/opt/litellm-lb/` is the live deploy directory (not a git checkout — see §7)

### 2.2 Docker stacks under `/opt`
| Stack | Purpose | Public vhost |
|---|---|---|
| `gateway` | Caddy edge + TLS | `*.91-107-194-138.nip.io` |
| `authentik` | OIDC IdP, policy engine | `authentik.91-107-194-138.nip.io` |
| `litellm-lb` | Router + workers + postgres | `llm.91-107-194-138.nip.io` |
| `plane` | Team project management | `plane.91-107-194-138.nip.io` |

Only ports 22, 80, 443 (plus 9000/9443 for Portainer) are exposed externally.
All inter-container traffic is on internal docker networks.

### 2.3 LiteLLM router stack (`litellm-lb`)
Containers:
- `litellm-lb-router-1` — `ghcr.io/berriai/litellm:main-stable`, port 4000
- `litellm-lb-worker1-1`, `litellm-lb-worker2-1` — `litellm-lb-oauth-proxy:local` (built from `worker-image/`)
- `litellm-lb-db-1` — `postgres:16`

Key mounts into the router (`docker-compose.yaml`):
- `./config-router.yaml` → `/app/config.yaml` — model list, routing, callbacks
- `./sticky_router.py` → `/app/sticky_router.py` — per-user hook (§3)
- `./chatgpt-data/auth.json` — ChatGPT OAuth token dir for the `chatgpt/*` models
- `./ui-btn-patched.js` — in-place UI patch; not tracked in git (see §7)

Each worker mounts a `workerN-claude` named volume at `/home/claude/.claude/`
containing its subscription's `.credentials.json`. Tokens are effectively
non-expiring (`expiresAt` set to year 2099).

## 3. Sticky routing (`sticky_router.py`)

A `CustomLogger` subclass whose `async_pre_call_hook` runs before every
request. It rewrites three unsuffixed model names into their `-a` / `-b`
siblings based on a stable hash of the caller's identity:

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

### 3.1 Failover

`router_settings.fallbacks` lists six `-a ↔ -b` cross-pairs. If worker1 is
rate-limited (429) or down, a request hashed to `-a` transparently retries on
`-b`. Verified end-to-end by stopping `worker1` and firing an unsuffixed
request — served by `worker2` in ~4 s. The fallback response is a Anthropic
cache miss on the alternate subscription, so the first fallback request pays
full input cost, but the user is unblocked.

### 3.2 Why this shape (and not something "cleaner")

We attempted the more elegant design — remove `-a`/`-b` entirely, put every
entry under the unsuffixed name with a unique `model_info.id`, pin via
`specific_deployment=True`. It doesn't work in LiteLLM 1.82.3: the
`specific_deployment` parameter is honoured by the Python SDK but **not by the
HTTP proxy layer** — the router does a model-group lookup by the `model`
string and fails with "No deployments available". The current shape
(unsuffixed as public names, `-a`/`-b` as routing targets, hook bridging them)
is the simplest working architecture in LiteLLM OSS today.

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

Expression policy in Authentik (`name: gh-collab-gate`). Queries
`GET /repos/ComposioHQ/agent-orchestrator/collaborators/{username}` on every
evaluation:
- **204** → collaborator → allow
- **404** → not a collaborator → deny with user-facing message
- other → deny + log warning (rare — typically GitHub API outage)

Bound at two points:
1. **LiteLLM application** (`target=<app_pk>`, order=0) — runs on each SSO
   login to LiteLLM. If someone is removed as a collaborator, their next
   login is denied. Existing sessions/keys still work until revoked (§5.2).
2. **GitHub enrollment flow** (`target=<flow_pk>`, order=0) — runs before
   user creation. A non-collaborator never gets an Authentik user, which
   means the first-time-login path is fully gated.

The PAT used for the API call is currently `gh auth token` (harsh-batheja's
GitHub CLI token). Stored inline in the policy expression (Authentik admins
can view) and mirrored to Infisical at
`/servers/91-107-194-138 GITHUB_COLLAB_PAT` for continuity. **TODO: rotate to
a fine-grained repo-scoped PAT or a GitHub App installation token**.

### 4.3 Scoped virtual keys

Two key-level changes make `/v1/models` show only the unsuffixed public names:

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
| `/opt/litellm-lb/.env` (root-owned, mode 600) | `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `ZAI_API_KEY`, `AUTHENTIK_*` OIDC settings (see §4.1 — endpoints, client_id, client_secret) |
| `/opt/authentik/.env` (root-owned, mode 600) | Authentik's own bootstrap secrets |
| `/opt/litellm-lb/chatgpt-data/auth.json` | Codex CLI OAuth token (`aoagent`-owned, mode 600, git-ignored) |

Retrieve any Infisical secret with `iget /servers/91-107-194-138 <KEY>` from
the laptop.

### 5.2 Revoking a user

Today (explicit):
```
curl -X POST https://llm.91-107-194-138.nip.io/user/block \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -d '{"user_id":"<pk-or-email>"}'
```
LiteLLM re-reads user state on every request with a ~60 s in-memory cache, so
blocks propagate within about a minute. Unblock via `/user/unblock`.

Planned (§6.2): a GitHub `member` webhook triggers this automatically.

### 5.3 Adding a new team member

The only action required is **adding them as a collaborator on
`ComposioHQ/agent-orchestrator`**. On their next visit to
`https://llm.91-107-194-138.nip.io/`:
1. Authentik gates them through the collab check (§4.2) → pass.
2. Enrollment flow creates an Authentik user matching their GitHub login.
3. LiteLLM creates an internal user + virtual key with the scoped
   `default_internal_user_params.models`.
4. They get the LiteLLM UI where they can copy their key for Claude Code /
   opencode / etc.

No manual provisioning, no per-user keys to stash in Infisical.

### 5.4 Rotating `GITHUB_COLLAB_PAT`

1. Generate a fine-grained PAT on `ComposioHQ/agent-orchestrator` with
   **Collaborators: Read** only.
2. Update Infisical:
   `iset /servers/91-107-194-138 GITHUB_COLLAB_PAT <new>`
3. Update the Authentik policy expression (PATCH
   `/api/v3/policies/expression/<pk>/`) — the PAT is inlined near the top.
4. Test: `POST /api/v3/policies/all/<pk>/test/` with a known collaborator and
   a known non-collaborator (see `octocat` as the canonical negative probe —
   GitHub mascot, not a collaborator on the repo).

### 5.5 Editing the live config

The server's `/opt/litellm-lb/` is not a git checkout (§7). Config edits are
made in-place and will show up as drift on next deploy. After any edit:
```
cd /opt/litellm-lb
sudo docker compose up -d --force-recreate router
docker logs --tail 100 litellm-lb-router-1
```
Sanity check: `curl https://llm.91-107-194-138.nip.io/health/readiness`
should report `healthy` with `StickyRouter` among the `success_callbacks`.

## 6. Known gaps / what's left

### 6.1 PAT in expression policy is a personal CLI token
`gh auth token` (harsh-batheja's) is pinned inline today. Rotate to a
dedicated fine-grained repo-scoped PAT or a GitHub App installation token —
reduces blast radius and isn't tied to one person's CLI session.

### 6.2 Webhook-based instant revocation
Removing a collaborator on GitHub blocks their **next** SSO login (via the
Authentik gate), but their existing virtual key keeps working until a manual
`/user/block` call. Plan:
- Deploy `adnanh/webhook` as a systemd service on `aoagents`.
- One hook handler: `github:member.removed` → shell script →
  `POST /user/block`.
- HMAC secret in Infisical at
  `/servers/91-107-194-138 GITHUB_WEBHOOK_SECRET`.
- Public endpoint at `hooks.91-107-194-138.nip.io`, fronted by existing Caddy.
- LiteLLM's ~60 s user cache picks up the block automatically; no polling
  needed for correctness.
- Defence-in-depth: an observe-only daily cron that logs (but doesn't act on)
  drift between GitHub collaborators and LiteLLM users. Alerts on silent
  webhook failure without risking wrongful blocks from cron-side bugs.

### 6.3 `/opt/litellm-lb` drift vs. the repo
The server directory is a loose copy of `github.com/harsh-batheja/litellm-lb`
without a `.git/` inside. All changes we pushed to the branch
`deploy/sticky-router` were lifted from the server by `scp`. Going forward:
- Merge `deploy/sticky-router` into `main` once reviewed.
- Either init `/opt/litellm-lb` as a tracking checkout (`git init` + `git
  remote add` + `git reset --mixed origin/main`) so future edits show as
  diff, OR adopt a rsync/pull-based deploy from the laptop.
- `ui-btn-patched.js` is on the server but not in the repo — decide whether
  to vendor it in or document it as a one-off runtime patch.

### 6.4 Team onboarding is untested end-to-end
I've verified every step in isolation:
- Authentik policy test endpoint returns `passing=False` for `octocat`.
- Scoped-key `/user/new` inherits the right `models` list.
- SSO redirect from LiteLLM returns a valid Authentik URL.

What **hasn't** been verified: an actual GitHub account that isn't
`harsh-batheja` going through the full SSO flow. First real onboarding
(Adil / Dhruv) will validate:
- Authentik error page rendering for a denied user.
- Enrollment flow creating the user when the gate passes.
- LiteLLM auto-provisioning with the scoped model list.

### 6.5 Other upstream issues we're tolerating
- **GPT prompt inflation** — LiteLLM's `chatgpt/` provider unconditionally
  prepends the Codex CLI system prompt (~1.6 k tokens, 1.28 k of which are
  cached). No user dollar cost (subscription path), but Codex subscription
  quota burns faster. Any real Codex CLI client pointed at this LB would pay
  the prompt roughly **twice** — once by LiteLLM's unconditional injection,
  once by its own. Recommendation: document "don't point Codex CLI here, use
  it directly against OpenAI" until we fork the chatgpt provider or wait for
  an upstream fix.
- **GLM long-output latency** — `glm-*` models on `api.z.ai` occasionally
  time out with `max_tokens > 50` or so. Unrelated to the LB wiring.
- **GPT non-streaming 500** — documented LiteLLM bug; clients must use
  `stream: true` with any `gpt-*` model.

### 6.6 No monitoring / alerting yet
Prometheus callbacks are wired in `config-router.yaml`
(`success_callback: [prometheus]`, `failure_callback: [prometheus]`,
`service_callback: [prometheus_system]`), but no scraper or alert rules are
configured. Before the team actually relies on this, at minimum add:
- A Prometheus scraper reading `/metrics` on the router.
- Alert on: router container down, worker 429 rate > threshold, Anthropic 5xx
  rate > threshold, spend per user approaching a budget cap.

### 6.7 No budget caps
`default_internal_user_params` doesn't set `max_budget`. Today a compromised
key could burn unlimited quota on any subscription. Add per-user
`max_budget: <reasonable-$-or-tokens>` with a reset window once we have
signal on normal usage.

## 7. Repo ↔ server relationship

- **Repo** (`github.com/harsh-batheja/litellm-lb`, canonical): owns the
  worker image build, the router config template, the sticky hook, the
  compose file.
- **Server** (`/opt/litellm-lb/`): contains the same files plus
  deployment-specific artefacts (`.env` with real secrets, `chatgpt-data/`
  with the Codex OAuth token, `ui-btn-patched.js` for a router UI patch).
  Not a git checkout today.

The currently-open PR (`deploy/sticky-router`, PR #1) brings the repo in sync
with server reality for the pieces that belong in version control. Once
merged, the "convert `/opt/litellm-lb` into a tracking checkout" task in
§6.3 closes the loop — `git pull` becomes the deploy primitive, `git status`
becomes the drift-detector.

---

**Maintainers**: `harsh-batheja` (primary). Collaborators on
`ComposioHQ/agent-orchestrator` inherit LiteLLM access via the gh-collab-gate
policy; the access list lives in GitHub, not in this repo or Authentik.

# bot-posts-linkedin

> Telegram-driven publishing pipeline for LinkedIn. A `[GERAR-POST]` message
> kicks off a fully durable workflow: web research with Claude, optional
> GitHub project lookup, bilingual post generation (PT + EN), Flux 1.1 Pro
> image, human approval, and one-click publish to a personal LinkedIn
> profile — all running serverless on Google Cloud Run.

## What it does

Send a message in Telegram:

```
[GERAR-POST] [GITHUB] my experience training a DistilBERT router
```

Behind the scenes, in roughly one minute:

1. Claude searches the web for fresh context on the topic.
2. If `[GITHUB]` flag is present, the bot lists the author's public repos,
   asks Claude to rank the most relevant ones, and pulls their READMEs.
3. Both signals plus a custom persona-tuned system prompt are sent to
   Claude to produce a single bilingual post — Portuguese first, then
   English, idioms culturally adapted (not literal translations).
4. Replicate's Flux 1.1 Pro generates a matching image; it lands in GCS
   with a signed URL.
5. Telegram receives an inspection message with the gathered signals,
   the photo, and the final bilingual text with `✅ Approve` / `❌ Reject`
   buttons.
6. **On approve:** the post is published on the author's LinkedIn with
   the URN persisted in Firestore and a link sent back on Telegram.
7. **On reject:** the bot asks for the reason as free text and regenerates
   the body + image. Up to 5 revisions; the 6th forces a final
   `✅ Approve last version` / `🚫 Cancel all` choice.

## Live URLs

| | URL |
|---|---|
| **Cloud Run service** | `bot-posts-linkedin` (`southamerica-east1`) |
| **Health check** | [`GET /healthz`](https://bot-posts-linkedin-tpbjdzokhq-rj.a.run.app/healthz) |
| **Repo** | https://github.com/raulmn00/bot-posts-linkedin |

> The Telegram webhook is gated by `X-Telegram-Bot-Api-Secret-Token` and the
> bot only acts on a single authorized `chat_id`. Random requests to the
> public URL return 200 silent — the service does not reveal its purpose
> to unauthenticated visitors.

## Architecture

```
┌─────────────┐   webhook (~50ms)   ┌──────────────────────────────────────┐
│             │ ─────────────────►  │  FastAPI on Cloud Run                │
│  Telegram   │                     │   ├─ dedup by update_id (Firestore)  │
│             │ ◄────────────────── │   ├─ command parser + chat-state     │
└─────────────┘   sendPhoto + post  │   └─ enqueue Cloud Task ──┐          │
       ▲                             └───────────────────────────┼──────────┘
       │                                                         │
       │ post + link                                             │ POST /internal/process-task
       │                                                         │ (OIDC-signed by Cloud Tasks)
       │                                                         ▼
       │                             ┌──────────────────────────────────────┐
       │                             │  Worker (same Cloud Run service)     │
       │                             │   ├─ Anthropic web_search            │
       │                             │   ├─ GitHub repos + READMEs          │
       │                             │   ├─ Claude bilingual body           │
       │                             │   ├─ Flux 1.1 Pro → GCS signed URL   │
       │                             │   └─ LinkedIn REST API publish       │
       │                             └──────────────┬───────────────────────┘
       │                                            │
       │                                            ▼
       │                             ┌──────────────────────────────────────┐
       └─────────────────────────────│  Firestore (audit + chat state)      │
                                     │   posts/  chat_states/  processed_updates/
                                     └──────────────────────────────────────┘
```

Two Cloud Run paths share the same container:

- **`POST /telegram/webhook`** — fast path. Dedupes by `update_id`, persists
  state, enqueues a Cloud Task, returns 200 in ~50 ms. Any user that
  somehow lands here is silently ignored unless authorized.
- **`POST /internal/process-task`** — slow path. Cloud Tasks invokes it
  with an OIDC token signed by the service's own service account; the
  endpoint verifies the token's audience and email before dispatching.
  This is where research, generation, image creation, and LinkedIn
  publication actually run — durable to scale-to-zero hibernations.

## State machine

```
DRAFT ──► RESEARCHING ──► GENERATING ──► AWAITING_APPROVAL ─┬─► APPROVED ─► PUBLISHED
                                                            ├─► REVISING ──► GENERATING (loop, max 5)
                                                            └─► REJECTED
                                                                            ▲
                                                                            │ (cancel at limit / discard / publication failure)
```

Terminals: `PUBLISHED`, `SIMULATED` (dry-run), `REJECTED`. Each transition
is gated by an explicit transition table; invalid moves raise.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 | Pydantic v2, async-native APIs |
| Web | FastAPI + Uvicorn | Async webhook, lifespan validation |
| LLM | Anthropic Claude Sonnet 4.6 | Bilingual generation + native web search tool |
| Image | Replicate Flux 1.1 Pro | Quality vs cost balance |
| Object storage | Google Cloud Storage | Signed URLs (7d TTL) for LinkedIn photo upload |
| Database | Firestore (Native) | Async client, per-collection TTL, no schema migration |
| Job queue | Google Cloud Tasks | Durable retry, OIDC-authenticated worker |
| Telegram | Bot API via httpx | Webhook + inline keyboards |
| LinkedIn | REST API `LinkedIn-Version: 202506` | `w_member_social` scope |
| Deploy | Cloud Run (managed) | Scale-to-zero, single revision |
| Tooling | `uv` + `ruff` + `pytest` | Fast, deterministic |

## Setup local

Requires Python 3.13+ and the [`uv`](https://github.com/astral-sh/uv) package
manager. See `.env.example` for the full list of variables; copy it to
`.env` and fill in credentials.

```bash
# Install dependencies (production + dev)
make install

# Run the full test suite (139 cases, ~2s)
make test

# Lint
make lint

# Start the FastAPI app on port 8080
make dev
```

For end-to-end local testing, expose the local port via `ngrok` and
register the webhook with Telegram:

```bash
ngrok http 8080                                       # in another terminal
uv run python scripts/register_telegram_webhook.py https://abc.ngrok-free.app
```

In dev mode `LINKEDIN_DRY_RUN=true` is recommended — approving a post
sends the full LinkedIn payload as a Telegram message instead of actually
publishing, so you can iterate on copy without burning a real post.

## Deploy to Google Cloud Run

The deploy is fully scripted. From a fresh project (with the required
Google Cloud APIs enabled) the sequence is:

```bash
# One-time per project:
make gcp-create-sa           # service account + IAM roles
make gcp-secrets-sync        # secrets from .env → Secret Manager
make gcp-firestore-indexes   # composite index for find_active_for_chat
make gcp-firestore-ttl       # native TTL on processed_updates
make gcp-tasks-queue         # Cloud Tasks queue config

# Every deploy:
make gcp-deploy              # Cloud Build + Cloud Run (~2 min)
make gcp-register-webhook    # point Telegram at the new URL
```

Other helpers:

```bash
make gcp-logs                # tail Cloud Run logs
make gcp-toggle-dry-run      # safe mode — no real LinkedIn publication
make gcp-toggle-real         # live publishing
```

The service runs with `--max-instances=1` and `--no-cpu-throttling` —
sufficient for personal use and matches the queue's `max-concurrent-dispatches=1`.

## Repository structure

```
.
├── prompts/
│   └── post_generation_system.txt   System prompt — author's voice, bilingual rules
├── scripts/
│   ├── gcp_*.sh                     One-shot GCP setup helpers
│   └── register_telegram_webhook.py
├── src/bot_posts_linkedin/
│   ├── config.py                    pydantic-settings with .env validation
│   ├── domain/                      Post, ChatState, PostStatus, RejectionCause
│   ├── store/                       In-memory + Firestore implementations
│   ├── services/
│   │   ├── anthropic_client.py      Claude with native web search tool
│   │   ├── github_search.py         List repos + LLM ranking + README fetch
│   │   ├── post_generator.py        Bilingual body + image prompt
│   │   ├── image_generator.py       Replicate (Flux 1.1 Pro) + GCS storage
│   │   ├── linkedin_publisher.py    REST publication with dry-run safety
│   │   ├── post_flow.py             Orchestrates the entire state machine
│   │   ├── task_queue.py            Cloud Tasks client + OIDC
│   │   └── update_dedup.py          Firestore-backed update_id dedup
│   ├── telegram/
│   │   ├── client.py                Bot API wrapper (Protocol + httpx impl)
│   │   ├── parser.py                [GERAR-POST] + [GITHUB] parser
│   │   ├── messages.py              All user-facing texts
│   │   ├── keyboards.py             Approval / discard / limit keyboards
│   │   ├── webhook.py               /telegram/webhook — dedup + dispatch
│   │   └── worker.py                /internal/process-task — OIDC-validated
│   └── main.py                      FastAPI app factory + boot validation
└── tests/                           139 cases, all isolated via fakes (no IO)
```

## Project status

Built in seven small phases, each landing with tests and a smoke test
against production before moving on:

| Phase | What landed |
|---|---|
| A | Skeleton — config, Post + state machine, in-memory store, `/healthz` |
| B | Telegram webhook + parser + mock generation flow |
| C | Anthropic web search + GitHub repo lookup + insights message |
| D | Real bilingual generation + Flux image + GCS storage |
| E | Revision loop with hard limit + cancel UX |
| F | LinkedIn publication with `LINKEDIN_DRY_RUN` safety |
| G.1 | Dockerfile + Cloud Run + Secret Manager |
| G.2 | Firestore persistence (replaces in-memory stores) |
| G.3 | Cloud Tasks + update_id dedup |

Open items intentionally left for later: Cloud Scheduler with a proactive
token-expiry warning, GCS lifecycle to clear orphan post images, and
raising `max-instances` if usage ever justifies it.

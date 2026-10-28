# DukaanPilot

**A shop assistant that talks first.** Every morning it tells a shopkeeper what happened yesterday, *why*, and what to order today — in Telugu, Hindi or English, on Telegram. The owner can reply by voice. Nothing is ever ordered without a tap on **Approve**.

Built for the Google Gen AI Academy challenge *Track 3 — Automate Daily Operations with a Productivity Agent* (checking sales, tracking inventory, deployed on Cloud Run).

> "Yesterday's sales were ₹7,500 lower than a normal Thursday. Milk, curd, bread and eggs ran out because the dairy delivery didn't come. Dussehra is in 4 days and rice is down to 3 days of stock. I've drafted two orders — tap to approve."

## Why it is different from a chatbot over a spreadsheet

| Typical "sales assistant" | DukaanPilot |
|---|---|
| Waits to be asked | **Proactive**: morning briefing and stock alerts via Cloud Scheduler |
| Reports numbers | **Explains causes** ("sales fell because 4 items were out of stock") |
| Forecasts from raw history | **Ignores stock-out days** so empty shelves don't teach it that demand is low |
| Ignores the calendar | **Festival-aware** reordering (Dussehra, Diwali…) |
| English dashboards | **Voice notes** and replies in the owner's language |
| Acts on its own or does nothing | **Prepares** the supplier order; the owner approves with one tap, then it opens WhatsApp with the message ready |

## How it works

```
 Owner (Telegram, text or voice)            Cloud Scheduler (hourly, OIDC)
            │                                          │
            ▼                                          ▼
   POST /webhooks/telegram                  POST /internal/jobs/run
            │  secret header                           │  verified Google identity
            └───────────────┬──────────────────────────┘
                            ▼
                Cloud Run  (FastAPI + Google ADK 2.12)
      ┌─────────────────────┼──────────────────────────┐
      ▼                     ▼                          ▼
  ADK agent           Briefing writer            Plain Python services
  17 typed tools      (Gemini, then a            forecast · anomalies · inventory
  Gemini on Vertex     numbers check; falls       festival calendar · reorder drafts
      │                back to a template)
      └──────────────► tools ──► Firestore  (shops, products, sales, drafts, audit log)
```

**Design rule: the model talks, the code counts.** Every number comes from deterministic code (`dukaanpilot/services`). The model chooses which tool to call and phrases the answer. The morning briefing is checked afterwards: if the model writes a number that is not in the computed facts, the message is replaced with a plain template built from those facts.

**Design rule: the model can propose, only a person can approve.** The agent can create reorder drafts but has no tool that approves one. Approval is a Telegram button handled by code, using an atomic compare-and-set (pending → approved/rejected/expired), so double-taps and retries cannot send an order twice.

### Google technologies
Agent Development Kit (`Agent`, `Runner`, `FunctionTool`, sessions, `adk eval`) · Gemini on Vertex AI (text and inline audio) · Cloud Run · Cloud Scheduler (OIDC) · Firestore · Secret Manager · Cloud Build · Cloud Logging/Trace.

## Security model

- Production **fails closed**: no webhook secret → webhook returns 503; no scheduler identity or shared secret → `/internal/*` and all ADK routes return 401.
- Telegram requests must carry the secret token header (constant-time compare). Scheduler calls must carry a Google-signed OIDC token with the expected audience and service-account email.
- A chat that has not joined a shop **never reaches the model**. Joining uses a single-use code; a linked shop cannot be taken over by another chat.
- The shop is taken from the verified chat, never from model arguments. A prompt-injected "show me shop X" cannot cross tenants (tested).
- Per-chat rate limit before any model call; Telegram retries are de-duplicated by `update_id`.
- Every write is recorded in an audit log. Secrets live in Secret Manager. The container runs as a non-root user with a dedicated least-privilege service account.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                      # 100+ tests, no network or API key needed
DUKAANPILOT_SEED_DEMO=true python main.py   # http://localhost:8080 (demo shop, local SQLite)
curl -X POST "localhost:8080/internal/jobs/run?job=briefing&force=true"
```

Without credentials the app still runs: chat answers with a friendly "try again" and the briefing falls back to the deterministic template. To use Gemini locally set `GOOGLE_API_KEY`, or use Vertex AI (see `.env.example`). Set `DUKAANPILOT_WEB_UI=true` to get ADK's dev UI.

## Deploy to Google Cloud

```bash
gcloud auth login && gcloud auth application-default login
export PROJECT_ID=your-project
export TELEGRAM_BOT_TOKEN=...          # from @BotFather
./deploy/deploy.sh                     # APIs, Firestore, service accounts, secrets, Cloud Run, webhook, Scheduler
DUKAANPILOT_STORE=firestore GOOGLE_CLOUD_PROJECT=$PROJECT_ID python scripts/seed_demo.py   # prints /start CODE
```

Then send `/start CODE` to your bot. Commands: `/briefing`, `/pending`, `/help`. `cloudbuild.yaml` runs lint, tests, build and deploy for CI/CD.

## Putting a real shop's data in

DukaanPilot is only as good as its data, so it is honest about where the data comes from:

1. **Day one — spreadsheet import.** Export products and sales from Tally, Vyapar, Excel or any POS as CSV and run `scripts/create_shop.py --products products.csv --sales sales.csv`. Bad rows are reported by line number and nothing is written.
2. **Every day — the owner talks to it.** "Sold 5 packets of milk", a voice note, or "got 100 kg rice" updates sales and stock.
3. **Roadmap:** direct POS/UPI/Account Aggregator feeds, photographed supplier bills.

## Tests and evaluation

- `pytest` — unit, tool, API and agent-wiring tests. The agent tests drive a scripted model through the real ADK `Runner`, so tool schemas, state and callbacks are exercised without network access.
- Key guards were mutation-checked: removing the double-approval guard, the production auth gate, the number-grounding check, the stock-out exclusion or the Telegram secret check each makes a test fail.
- `tests/test_repository_contract.py` runs the same storage tests on SQLite and, with `FIRESTORE_EMULATOR_HOST` set, on Firestore.
- `evals/` holds an ADK evaluation set (tool choice, the approval boundary, a Hindi query) for `adk eval` against a live model.

## What has and has not been verified

Verified in development: all unit/integration tests, lint, and a live local HTTP run of the production-mode server (health, auth gates, forced jobs, graceful fallback without a model key).

**Not yet verified — do these before the demo:**
- Behaviour with a real Gemini model, including Telugu quality and voice notes (run `adk eval`, then chat with the bot). The default model id `gemini-3.5-flash` should be confirmed available in your project and region.
- The Firestore repository against a live database or emulator (the contract tests are written but skipped without an emulator).
- The Telegram client and the Scheduler OIDC verification against the real services, and the `deploy.sh` script end to end.

## Known limits

- Session memory uses SQLite on Cloud Run's temporary disk: after a restart the chat forgets the *conversation*, not the shop data (that is in Firestore). Use Cloud SQL via `DUKAANPILOT_SESSION_DB_URL` if that matters.
- The rate limiter is per instance (`--max-instances 3`).
- Voice notes are stored inside ADK session events; delete sessions if audio privacy matters.
- Festival dates are in `dukaanpilot/data/festivals.json` with source links; the uplift factors are estimates, not learned yet.
- Photo bills are acknowledged but not read yet.

## Roadmap

WhatsApp Cloud API as a second channel · POS/UPI/Account Aggregator ingestion · photographed bill reading with Gemini vision · learned festival uplift per shop · multi-agent supplier negotiation over A2A · price-comparison across suppliers.

## Layout

```
main.py                    Cloud Run entry point (ADK API server + our routes)
dukaanpilot/agent.py       root_agent + briefing agent
dukaanpilot/tools/         17 typed tools (read_tools, write_tools)
dukaanpilot/services/      forecast, analytics, inventory, reorders, briefing, messaging
dukaanpilot/data/          models, SQLite + Firestore repositories, importers, demo seed
dukaanpilot/api/           auth gate, Telegram handler, routes
deploy/  cloudbuild.yaml   deployment
scripts/  evals/  tests/   onboarding, ADK evals, tests
```

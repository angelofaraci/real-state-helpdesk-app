# Real Estate Helpdesk

Backend service for the real estate helpdesk project (Stage 1 core, plus
Stage 2 ticket classification).

## Development

```bash
pip install -e ".[dev]"
docker compose up -d
uvicorn app.main:app --reload
```

Copy `env.example` to `.env` and adjust values for your local environment.

## Stage 2 — ticket classification

Tickets created without `category_id`/`urgency_id` are saved as
`classification_status="pending"` and a background job classifies them
asynchronously (see `app.api.v1.tickets.create_ticket` and
`app.workers.classification`):

1. `POST /tickets` with taxonomy omitted -> ticket is created `pending`,
   and a `classify_ticket` job is enqueued on Redis (arq) right after the
   creating request commits.
2. The `worker` service (see `docker-compose.yml`, running
   `arq app.workers.settings.WorkerSettings`) picks up the job, embeds the
   ticket text, predicts category + urgency, and writes the result back
   (row-locked via `FOR UPDATE` so a duplicate job delivery is a no-op).
3. A cron job, `sweep_pending_classifications`, periodically re-enqueues
   any ticket still `pending` after 5 minutes — a safety net for jobs lost
   across a worker crash/restart or a failed enqueue.

Run the worker alongside the app and Redis:

```bash
docker compose up -d redis
docker compose up worker
```

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | Used by the API to enqueue jobs and by the worker to consume them. |
| `EMBEDDING_PROVIDER` | `local` | `local` uses `sentence-transformers` (requires the `ml` extra); `openai` calls the OpenAI API instead. |
| `OPENAI_API_KEY` | unset | Only required when `EMBEDDING_PROVIDER=openai`. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat/embedding model used when `EMBEDDING_PROVIDER=openai`. |
| `CLASSIFIER_CONFIDENCE_THRESHOLD` | `0.6` | Minimum confidence to auto-apply a predicted category/urgency. |
| `ML_ARTIFACTS_DIR` | `./ml_artifacts` | Directory containing the trained joblib artifacts loaded by `app.services.classifier`. |

### The `ml` optional extra

`pip install -e ".[ml]"` (or `".[dev,ml]"`) installs `sentence-transformers`,
which is only needed when `EMBEDDING_PROVIDER=local`. If you run with
`EMBEDDING_PROVIDER=openai`, the `ml` extra is not required.

## Stage 4 — chatbot

A widget-facing chat API lets visitors (anonymous or identified) talk to an
LLM grounded in the knowledge base, with tool-calling for ticket status
lookups, ticket creation, visit scheduling, and human escalation (see
`app.services.chat`/`app.services.chat_tools`). Every turn-taking request
after session creation authenticates via either a real Bearer access token
or the `X-Chat-Session` header — the session-scoped token
`POST /sessions` returns, which must match the `session_id` path segment
it is used against (see `app.api.deps_chat` for the full resolution
order):

- `POST /api/v1/chat/sessions` — start a session for a `widget_key`.
- `POST /api/v1/chat/sessions/{session_id}/messages` — send a visitor turn.
- `GET /api/v1/chat/sessions/{session_id}/messages` — chronological history.

### CI and retraining

`.github/workflows/ci.yml` runs the test suite, retrains the stage-2
classifiers via `scripts/train_classifier.py`, and gates the build on a
macro-F1 threshold via `scripts/evaluate_classifier.py`. If you change the
training data or feature pipeline, re-run
`python scripts/train_classifier.py` locally and commit the updated
joblib artifacts under `ml_artifacts/` — the app loads pre-trained
artifacts at runtime, it does not train on the fly.

## Stage 5 — multichannel (WhatsApp + email)

WhatsApp routes through the Stage 4 chatbot pipeline (`app.services.chat`);
email routes directly into the Stage 1 ticket model. The two channels are
intentionally not unified into one abstraction.

- `GET/POST /api/v1/webhooks/whatsapp` — Meta verification handshake and
  inbound message webhook. Every POST is signature-verified
  (`X-Hub-Signature-256`) before any database read; a valid payload is
  acked `200` immediately and processed asynchronously by the
  `process_whatsapp_message` / `send_whatsapp_reply` arq jobs.
- `POST /api/v1/webhooks/email` — inbound email webhook (Mailgun by
  default). Signature-verified before any database read; matches an
  existing ticket thread or creates a new one and enqueues classification
  the same way `POST /tickets` does.

An unrecognized sender on either channel is auto-provisioned as a
`pending`-status user (see `app.services.channel_identity`) so a ticket can
always be created — `tickets.user_id` is not nullable. A sender whose
address/phone number already belongs to a *different* organization is
silently rejected (logged, never linked), since both `users.email` and
`users.phone_number` are globally unique.

### Organization channel configuration

A platform super admin configures a tenant's channels via
`PATCH /organizations/:id`:

| Field | Notes |
| --- | --- |
| `whatsapp_phone_number_id` | Meta's identifier for the org's WhatsApp Business number. |
| `whatsapp_access_token` | Write-only on input; encrypted at rest (Fernet) as `whatsapp_access_token_encrypted`. Never returned by any response — `GET`/`PATCH` responses expose only `whatsapp_access_token_set: bool`. |
| `support_email_address` | The inbound address assigned to this org by the email provider. |

WhatsApp config is all-or-nothing: `whatsapp_phone_number_id` and the
access token must be set together, never one without the other.

### Environment variables (Stage 5)

| Variable | Default | Notes |
| --- | --- | --- |
| `SECRET_ENCRYPTION_KEY` | unset | Comma-separated Fernet keys (urlsafe-base64, 32 bytes); the first encrypts, all decrypt. Required before any `whatsapp_access_token` can be stored. |
| `WHATSAPP_VERIFY_TOKEN` | unset | Platform-level token used for Meta's `GET` webhook verification handshake. |
| `WHATSAPP_APP_SECRET` | unset | Platform-level HMAC key used to validate `X-Hub-Signature-256`. |
| `WHATSAPP_API_BASE_URL` | `https://graph.facebook.com` | WhatsApp Cloud API base URL. |
| `WHATSAPP_API_VERSION` | `v21.0` | Graph API version used when sending replies. |
| `WHATSAPP_CUSTOMER_WINDOW_HOURS` | `24` | Meta's free-form reply window; a reply outside it is recorded as `deferred`, not sent (template messages are out of scope for this stage). |
| `WHATSAPP_SESSION_IDLE_MINUTES` | `1440` | How long a `chat_sessions` row for a given `wa_id` is reused before a new session starts. |
| `MAILGUN_SIGNING_KEY` | unset | HMAC key used to validate inbound Mailgun webhook signatures. |
| `EMAIL_WEBHOOK_PROVIDER` | `mailgun` | Selects the inbound email provider implementation. |
| `EMAIL_WEBHOOK_MAX_AGE_SECONDS` | `300` | Rejects an inbound email webhook whose signed timestamp is older than this — Mailgun's signature does not bind the body, so this bounds the replay window. |

### Known limitations (deferred)

- WhatsApp template messages for replies outside the 24-hour window are not
  implemented — such replies are persisted as `deferred` on the
  `chat_sessions` row and logged, not sent.
- IMAP polling is not implemented as an email ingestion method; inbound
  email is webhook-only.
- Attachments/media on either channel are not supported.

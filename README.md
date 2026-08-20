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

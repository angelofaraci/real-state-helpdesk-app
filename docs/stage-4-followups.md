# Stage 4 (chatbot) — deferred follow-ups

Tracking note for the one item deliberately left out of stage 4's scope
(see the stage-4 design doc). Not a design doc itself — just a pointer for
whoever picks this up next.

## No rate limit on session creation itself

`POST /api/v1/chat/sessions` (`app.api.v1.chat.create_chat_session`) has no
rate limiting of its own. `app.services.chat_rate_limit.check_and_increment`
only throttles *messages* on an already-created session
(`POST /sessions/{id}/messages`), not session creation. Since
`widget_key` is public (embedded in the widget snippet) and session
creation requires no credential, anonymous session minting is currently
unbounded — a single caller can mint an arbitrary number of `chat_sessions`
rows per organization with no cost.

This was explicitly out of scope for stage 4 (CONFIRMED design). It needs
a follow-up — most likely a per-`widget_key` or per-IP rate limit on
`POST /sessions`, reusing the same rolling-window Redis pattern
`chat_rate_limit` already establishes — before this ships to a
high-traffic production org.

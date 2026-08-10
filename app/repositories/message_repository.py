"""`MessageRepository` — structurally join-scoped through `Ticket`, but
DELIBERATELY NOT relied upon alone for authorization.

`Message` has no `organization_id` column, and unlike `ContractRepository`
(which joins through `properties.organization_id`, a plain org-column
join), messages need ticket-level VISIBILITY, not just org membership:
`TicketRepository.select()` fuses org scoping with role-based narrowing
(see that module's docstring for Rule C — a tenant only sees their own
tickets, an owner only their own properties' tickets, etc.). The generic
`ScopedRepository.scope_path` join mechanism can only express "join to a
model that has `organization_id`" — it has no way to express "and also
apply this other model's role-fused `select()` override". Forcing
`MessageRepository` to reimplement `TicketRepository`'s role logic itself
would duplicate Rule C in two places and risk them drifting apart.

So the authorization split here is deliberate:

- The SERVICE layer (`app.services.message_service`) is the actual
  authorization boundary: it resolves the parent ticket via
  `TicketRepository(session, scope).get_or_404(ticket_id)` FIRST — which
  already gives correct org+role visibility — and only then queries
  messages for that already-authorized `ticket_id`.
- `MessageRepository` itself still sets `scope_path = ((Ticket,
  "ticket_id"),)` so it satisfies `ScopedRepository.__init_subclass__`'s
  safety net (every concrete repository must be scoped SOMEHOW) and gets a
  real, if incomplete, org-level backstop for free: `scope_clause()` still
  excludes messages whose ticket belongs to a different organization. It
  just cannot express ticket-level ROLE visibility on its own — that part
  only exists because the service layer's explicit `get_or_404` pre-check
  adds it on top, before this repository is ever queried.

Do not call `MessageRepository.select()`/`get_or_404()` directly from a
route or service without first validating ticket visibility through
`TicketRepository` — doing so would leak messages across roles within the
same organization (e.g. one tenant reading another tenant's ticket
thread).
"""

from __future__ import annotations

from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories.base import ScopedRepository


class MessageRepository(ScopedRepository[Message]):
    model = Message
    scope_path = ((Ticket, "ticket_id"),)

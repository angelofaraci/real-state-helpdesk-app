"""`ContractRepository` — join-scoped (`Contract` has no `organization_id`
column of its own; scoping goes through `properties.organization_id`).

This is the first production `ScopedRepository` subclass to exercise the
join-path branch of `scope_clause()` — it validates Work Unit 3's join
design against a real model, not just the throwaway fixtures in
`tests/unit/test_scoped_repository.py`.
"""

from __future__ import annotations

from app.models.contract import Contract
from app.models.property import Property
from app.repositories.base import ScopedRepository


class ContractRepository(ScopedRepository[Contract]):
    model = Contract
    scope_path = ((Property, "property_id"),)

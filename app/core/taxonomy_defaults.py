"""Default taxonomy seed data — every organization is bootstrapped with
these categories and urgency levels at creation time (see
`app.services.taxonomy_seed.seed_default_taxonomy`).

Deliberately pure data with NO session/repository imports: a future
classifier (Stage 2) can import the label names here without pulling in
the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CategorySeed:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class UrgencySeed:
    name: str
    sla_hours: int
    sort_order: int


DEFAULT_CATEGORIES: tuple[CategorySeed, ...] = (
    CategorySeed("Maintenance", "Repairs or failures inside the unit or common areas"),
    CategorySeed("Billing", "Payments, invoices, due dates, arrears"),
    CategorySeed("Contract", "Renewals, terminations, clauses, deposits"),
    CategorySeed("Administrative", "Documents, receipts, certificates, ownership changes"),
    CategorySeed("Complaints", "Noise, coexistence issues, breaches"),
    CategorySeed("Sales inquiry", "Prospective tenants/owners, new listings, appraisals"),
)

DEFAULT_URGENCY_LEVELS: tuple[UrgencySeed, ...] = (
    UrgencySeed("Critical", sla_hours=2, sort_order=1),
    UrgencySeed("High", sla_hours=24, sort_order=2),
    UrgencySeed("Medium", sla_hours=48, sort_order=3),
    UrgencySeed("Low", sla_hours=120, sort_order=4),
)

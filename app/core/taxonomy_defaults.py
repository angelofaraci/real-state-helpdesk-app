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
    # Stage 6 — queues + SLA: whether this level's SLA clock pauses outside
    # business hours. Critical/High are effectively 24/7 (False); Medium/Low
    # pause overnight/weekends (True).
    respects_business_hours: bool


DEFAULT_CATEGORIES: tuple[CategorySeed, ...] = (
    CategorySeed("Maintenance", "Repairs or failures inside the unit or common areas"),
    CategorySeed("Billing", "Payments, invoices, due dates, arrears"),
    CategorySeed("Contract", "Renewals, terminations, clauses, deposits"),
    CategorySeed("Administrative", "Documents, receipts, certificates, ownership changes"),
    CategorySeed("Complaints", "Noise, coexistence issues, breaches"),
    CategorySeed("Sales inquiry", "Prospective tenants/owners, new listings, appraisals"),
)

DEFAULT_URGENCY_LEVELS: tuple[UrgencySeed, ...] = (
    UrgencySeed("Critical", sla_hours=2, sort_order=1, respects_business_hours=False),
    UrgencySeed("High", sla_hours=24, sort_order=2, respects_business_hours=False),
    UrgencySeed("Medium", sla_hours=48, sort_order=3, respects_business_hours=True),
    UrgencySeed("Low", sla_hours=120, sort_order=4, respects_business_hours=True),
)

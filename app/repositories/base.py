"""`ScopedRepository` — the generic base for every org-scoped repository.

This is the structural core of tenant isolation in the app: any repository
that reads or writes rows belonging to an organization should subclass this
instead of writing ad-hoc `WHERE organization_id = ...` clauses by hand.

Two scoping strategies are supported:

- **Direct**: the model has its own `organization_id` column (the common
  case, e.g. `properties`, `tickets`). `scope_path` stays empty (`()`).
- **Join path**: the model has no `organization_id` column of its own and
  is scoped transitively through a chain of foreign keys to a model that
  does (e.g. `contracts` -> `properties.organization_id`). `scope_path`
  describes that chain as `((JoinModel, "fk_column_name_on_the_previous_
  model"), ...)`, ending at the model that owns `organization_id`.

`__init_subclass__` enforces at class-creation time that every concrete
subclass picks one of the two strategies — the explicit safety net against
a future repository silently forgetting to scope itself at all.
"""

from __future__ import annotations

from typing import ClassVar, Generic, TypeVar
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope

ModelT = TypeVar("ModelT", bound=DeclarativeBase)

# A single hop in a join-based `scope_path`: the model being joined to, and
# the FK column name (on the *previous* model in the chain) that points at
# that model's primary key.
ScopePathStep = tuple[type[DeclarativeBase], str]


class ScopedRepository(Generic[ModelT]):
    """Generic org-scoped repository base. Subclasses set `model` (and,
    for join-scoped models, `scope_path`)."""

    model: ClassVar[type[DeclarativeBase]]
    scope_path: ClassVar[tuple[ScopePathStep, ...]] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.__dict__.get("model", getattr(cls, "model", None))
        if model is None:
            # An intermediate subclass that doesn't set `model` yet (e.g. a
            # further mixin) — nothing to validate until a concrete
            # subclass sets it.
            return

        has_org_column = hasattr(model, "organization_id")
        has_scope_path = bool(cls.scope_path)
        if not has_org_column and not has_scope_path:
            raise TypeError(
                f"{cls.__name__} scopes `{model.__name__}`, which has no "
                "`organization_id` column, but defines no `scope_path` "
                "either — this repository would be silently unscoped. Set "
                "`scope_path` to the join chain leading to the model that "
                "owns `organization_id`."
            )

    def __init__(self, session: AsyncSession, scope: OrgScope) -> None:
        self._session = session
        self._scope = scope

    def scope_clause(self) -> ColumnElement[bool]:
        """The `WHERE`-clause condition restricting `model` rows to the
        current `OrgScope`."""
        if not self.scope_path:
            return self.model.organization_id == self._scope.organization_id
        return self._join_exists_clause(self.model, self.scope_path)

    def _join_exists_clause(
        self, current_model: type[DeclarativeBase], path: tuple[ScopePathStep, ...]
    ) -> ColumnElement[bool]:
        join_model, fk_name = path[0]
        fk_column = getattr(current_model, fk_name)
        join_condition = join_model.id == fk_column

        remaining = path[1:]
        if remaining:
            inner_condition = self._join_exists_clause(join_model, remaining)
            condition = and_(join_condition, inner_condition)
        else:
            condition = and_(
                join_condition,
                join_model.organization_id == self._scope.organization_id,
            )
        return exists().where(condition)

    def select(self) -> Select[tuple[ModelT]]:
        """A `SELECT` of `model` already restricted to the current scope."""
        return select(self.model).where(self.scope_clause())

    async def get_or_404(self, id_: UUID, *, for_update: bool = False) -> ModelT:
        """Fetch a single row by id, raising `NotFoundError` if it does not
        exist or is not visible to the current scope."""
        stmt = self.select().where(self.model.id == id_)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        instance = result.scalar_one_or_none()
        if instance is None:
            raise NotFoundError(self.model.__name__, id_)
        return instance

    def add(self, **fields: object) -> ModelT:
        """Construct a new `model` instance and stage it on the session.

        For the direct (own `organization_id` column) case, any
        caller-supplied `organization_id` is discarded and forcibly
        replaced by the current scope's — a tenant can never create a row
        in another organization, even by accident. For the join-path case
        the model has no `organization_id` column, so none is set.
        """
        if not self.scope_path:
            fields = dict(fields)
            fields.pop("organization_id", None)
            fields["organization_id"] = self._scope.organization_id
        instance = self.model(**fields)
        self._session.add(instance)
        return instance

    async def update(self, id_: UUID, **fields: object) -> ModelT:
        """Fetch a row in scope and apply `fields` to it.

        `organization_id` is always stripped from `fields` first — org
        membership is immutable in stage 1, regardless of scoping strategy.
        """
        fields = dict(fields)
        fields.pop("organization_id", None)
        instance = await self.get_or_404(id_)
        for key, value in fields.items():
            setattr(instance, key, value)
        return instance

    async def delete(self, id_: UUID) -> None:
        """Fetch a row in scope and delete it."""
        instance = await self.get_or_404(id_)
        await self._session.delete(instance)

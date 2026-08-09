"""Org-scoped contract CRUD endpoints (join-scoped via property).

Write access (`POST`/`PATCH`) is admin-only, via `require_org_admin`. Read
access (`GET`) follows the same admin+agent staff policy as properties, via
`require_org_staff`. There is deliberately no `DELETE` endpoint: a
contract's lifecycle is managed via `status` transitions on `PATCH`, not
deletion, matching the original brief.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin, require_org_staff
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.contract import Contract
from app.schemas.contract import ContractCreate, ContractResponse, ContractUpdate
from app.services import contract_service
from app.services.contract_service import InvalidTenantRoleError

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"])


@router.post("", response_model=ContractResponse, status_code=status.HTTP_201_CREATED)
async def create_contract(
    payload: ContractCreate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Contract:
    """Create a contract linking `payload.tenant_id` to
    `payload.property_id`, both scoped to the caller's organization. A
    cross-org/soft-deleted property or a cross-org tenant surfaces as 404;
    a same-org tenant with the wrong role surfaces as 422."""
    try:
        return await contract_service.create_contract(
            session,
            scope=scope,
            property_id=payload.property_id,
            tenant_id=payload.tenant_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
    except InvalidTenantRoleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("", response_model=list[ContractResponse])
async def list_contracts(
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> list[Contract]:
    """List every contract in the caller's organization."""
    return await contract_service.list_contracts(session, scope=scope)


@router.get("/{contract_id}", response_model=ContractResponse)
async def get_contract(
    contract_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> Contract:
    """Fetch a single contract in the caller's organization. A cross-org id
    is indistinguishable from a missing one and surfaces as 404."""
    return await contract_service.get_contract(session, scope=scope, contract_id=contract_id)


@router.patch("/{contract_id}", response_model=ContractResponse)
async def update_contract(
    contract_id: UUID,
    payload: ContractUpdate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Contract:
    """Update `status` on a contract in the caller's organization. Start/end
    dates and the parties are immutable — this stage has no amendment
    concept."""
    fields = payload.model_dump(exclude_unset=True)
    return await contract_service.update_contract(
        session, scope=scope, contract_id=contract_id, **fields
    )

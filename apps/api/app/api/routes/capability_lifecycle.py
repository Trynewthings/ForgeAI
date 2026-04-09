from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.capability_registry import (
    CapabilityLifecycleEntryResponse,
    CapabilityLifecycleSummaryResponse,
    CapabilityLifecycleUpdateRequest,
)
from app.services.capabilities import CapabilityLifecycleService

router = APIRouter(tags=["capability_lifecycle"])


@router.get("/projects/{project_id}/capability-lifecycle", response_model=list[CapabilityLifecycleEntryResponse])
def list_project_capability_lifecycle(project_id: str, db: Session = Depends(get_db)) -> list[CapabilityLifecycleEntryResponse]:
    return CapabilityLifecycleService(db).list_project_entries(project_id)


@router.get("/projects/{project_id}/capability-lifecycle/summary", response_model=CapabilityLifecycleSummaryResponse)
def summarize_project_capability_lifecycle(project_id: str, db: Session = Depends(get_db)) -> CapabilityLifecycleSummaryResponse:
    return CapabilityLifecycleService(db).summarize_project_entries(project_id)


@router.get("/capability-lifecycle/{entry_id}", response_model=CapabilityLifecycleEntryResponse)
def get_capability_lifecycle_entry(entry_id: str, db: Session = Depends(get_db)) -> CapabilityLifecycleEntryResponse:
    return CapabilityLifecycleService(db).get_entry(entry_id)


@router.patch("/capability-lifecycle/{entry_id}", response_model=CapabilityLifecycleEntryResponse)
def update_capability_lifecycle_entry(
    entry_id: str,
    payload: CapabilityLifecycleUpdateRequest,
    db: Session = Depends(get_db),
) -> CapabilityLifecycleEntryResponse:
    return CapabilityLifecycleService(db).update_entry(entry_id, payload)

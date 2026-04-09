from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.capability_registry import (
    CapabilityProposalResponse,
    CapabilityRequestConversionResponse,
    CapabilityRequestProjectSummaryResponse,
    CapabilityRequestResponse,
    CapabilityRequestReviewUpdateRequest,
)
from app.services.capabilities import CapabilityLifecycleService, CapabilityProposalService, CapabilityRequestService

router = APIRouter(tags=["capability_requests"])


@router.get("/projects/{project_id}/capability-requests", response_model=list[CapabilityRequestResponse])
def list_project_capability_requests(project_id: str, db: Session = Depends(get_db)) -> list[CapabilityRequestResponse]:
    return CapabilityRequestService(db).list_project_requests(project_id)


@router.get("/projects/{project_id}/capability-requests/summary", response_model=CapabilityRequestProjectSummaryResponse)
def get_project_capability_request_summary(project_id: str, db: Session = Depends(get_db)) -> CapabilityRequestProjectSummaryResponse:
    return CapabilityRequestService(db).get_project_summary(project_id)


@router.get("/capability-requests/{request_id}", response_model=CapabilityRequestResponse)
def get_capability_request(request_id: str, db: Session = Depends(get_db)) -> CapabilityRequestResponse:
    return CapabilityRequestService(db).get_request(request_id)


@router.patch("/capability-requests/{request_id}/review", response_model=CapabilityRequestResponse)
def update_capability_request_review(
    request_id: str,
    payload: CapabilityRequestReviewUpdateRequest,
    db: Session = Depends(get_db),
) -> CapabilityRequestResponse:
    return CapabilityRequestService(db).update_review_state(
        request_id,
        review_status=payload.review_status,
        review_notes=payload.review_notes,
    )


@router.post("/capability-requests/{request_id}/convert", response_model=CapabilityRequestConversionResponse)
def convert_capability_request(request_id: str, db: Session = Depends(get_db)) -> CapabilityRequestConversionResponse:
    return CapabilityLifecycleService(db).convert_request(request_id)


@router.post("/capability-requests/{request_id}/proposal", response_model=CapabilityProposalResponse)
def generate_capability_proposal(request_id: str, db: Session = Depends(get_db)) -> CapabilityProposalResponse:
    return CapabilityProposalService(db).generate_for_request(request_id)

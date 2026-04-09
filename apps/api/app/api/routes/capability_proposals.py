from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.capability_registry import (
    CapabilityActivationEligibilityResponse,
    CapabilityActivationResultResponse,
    CapabilityMaterializationEligibilityResponse,
    CapabilityMaterializationProjectSummaryResponse,
    CapabilityMaterializationResponse,
    CapabilityMaterializationResultResponse,
    CapabilityProposalActivationRequest,
    CapabilityProposalProjectSummaryResponse,
    CapabilityProposalResponse,
    CapabilityProposalReviewUpdateRequest,
    CapabilityReusableCandidateProjectSummaryResponse,
    CapabilityReusableCandidateResponse,
)
from app.services.capabilities import CapabilityMaterializationService, CapabilityProposalService

router = APIRouter(tags=["capability_proposals"])


@router.get("/projects/{project_id}/capability-proposals", response_model=list[CapabilityProposalResponse])
def list_project_capability_proposals(project_id: str, db: Session = Depends(get_db)) -> list[CapabilityProposalResponse]:
    return CapabilityProposalService(db).list_project_proposals(project_id)


@router.get("/projects/{project_id}/capability-proposals/summary", response_model=CapabilityProposalProjectSummaryResponse)
def get_project_capability_proposal_summary(
    project_id: str,
    db: Session = Depends(get_db),
) -> CapabilityProposalProjectSummaryResponse:
    return CapabilityProposalService(db).get_project_summary(project_id)


@router.get("/projects/{project_id}/capability-materializations", response_model=list[CapabilityMaterializationResponse])
def list_project_capability_materializations(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[CapabilityMaterializationResponse]:
    return CapabilityMaterializationService(db).list_project_materializations(project_id)


@router.get(
    "/projects/{project_id}/capability-materializations/summary",
    response_model=CapabilityMaterializationProjectSummaryResponse,
)
def get_project_capability_materialization_summary(
    project_id: str,
    db: Session = Depends(get_db),
) -> CapabilityMaterializationProjectSummaryResponse:
    return CapabilityMaterializationService(db).get_project_summary(project_id)


@router.get(
    "/projects/{project_id}/reusable-capabilities",
    response_model=list[CapabilityReusableCandidateResponse],
)
def list_project_reusable_capabilities(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[CapabilityReusableCandidateResponse]:
    return CapabilityMaterializationService(db).list_reusable_candidates(project_id)


@router.get(
    "/projects/{project_id}/reusable-capabilities/summary",
    response_model=CapabilityReusableCandidateProjectSummaryResponse,
)
def get_project_reusable_capability_summary(
    project_id: str,
    db: Session = Depends(get_db),
) -> CapabilityReusableCandidateProjectSummaryResponse:
    return CapabilityMaterializationService(db).get_reusable_summary(project_id)


@router.get("/capability-proposals/{proposal_id}", response_model=CapabilityProposalResponse)
def get_capability_proposal(proposal_id: str, db: Session = Depends(get_db)) -> CapabilityProposalResponse:
    return CapabilityProposalService(db).get_proposal(proposal_id)


@router.patch("/capability-proposals/{proposal_id}/review", response_model=CapabilityProposalResponse)
def update_capability_proposal_review(
    proposal_id: str,
    payload: CapabilityProposalReviewUpdateRequest,
    db: Session = Depends(get_db),
) -> CapabilityProposalResponse:
    return CapabilityProposalService(db).update_review_state(proposal_id, payload)


@router.get("/capability-proposals/{proposal_id}/activation-eligibility", response_model=CapabilityActivationEligibilityResponse)
def get_capability_proposal_activation_eligibility(
    proposal_id: str,
    db: Session = Depends(get_db),
) -> CapabilityActivationEligibilityResponse:
    return CapabilityProposalService(db).get_activation_eligibility(proposal_id)


@router.post("/capability-proposals/{proposal_id}/activate", response_model=CapabilityActivationResultResponse)
def activate_capability_proposal(
    proposal_id: str,
    payload: CapabilityProposalActivationRequest,
    db: Session = Depends(get_db),
) -> CapabilityActivationResultResponse:
    return CapabilityProposalService(db).activate_proposal(proposal_id, payload)


@router.get(
    "/capability-proposals/{proposal_id}/materialization-eligibility",
    response_model=CapabilityMaterializationEligibilityResponse,
)
def get_capability_proposal_materialization_eligibility(
    proposal_id: str,
    db: Session = Depends(get_db),
) -> CapabilityMaterializationEligibilityResponse:
    return CapabilityMaterializationService(db).get_eligibility(proposal_id)


@router.post("/capability-proposals/{proposal_id}/materialize", response_model=CapabilityMaterializationResultResponse)
def materialize_capability_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
) -> CapabilityMaterializationResultResponse:
    return CapabilityMaterializationService(db).materialize_proposal(proposal_id)

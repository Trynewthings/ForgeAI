from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.debug import ProviderPingRequest, ProviderPingResponse
from app.services.provider_debug_service import ProviderDebugService

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/provider/ping", response_model=ProviderPingResponse)
def ping_provider(payload: ProviderPingRequest, db: Session = Depends(get_db)) -> ProviderPingResponse:
    return ProviderDebugService(db).ping(payload)

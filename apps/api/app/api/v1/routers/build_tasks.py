from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.enums import TERMINAL_BUILD_STATUSES
from app.core.logging import log_event
from app.schemas.build_task import ArtifactResponse, BuildStepResponse, BuildTaskResponse
from app.services.build_task_service import BuildTaskService

router = APIRouter(prefix="/build-tasks", tags=["build-tasks"])
logger = logging.getLogger(__name__)


@router.get("/{build_task_id}", response_model=BuildTaskResponse)
def get_build_task(build_task_id: str, db: Session = Depends(get_db)) -> BuildTaskResponse:
    return BuildTaskService(db).get_build_task(build_task_id)


@router.get("/{build_task_id}/steps", response_model=list[BuildStepResponse])
def get_build_task_steps(build_task_id: str, db: Session = Depends(get_db)) -> list[BuildStepResponse]:
    return BuildTaskService(db).get_build_steps(build_task_id)


@router.get("/{build_task_id}/artifacts", response_model=list[ArtifactResponse])
def get_build_task_artifacts(build_task_id: str, db: Session = Depends(get_db)) -> list[ArtifactResponse]:
    return BuildTaskService(db).get_artifacts(build_task_id)


@router.get("/{build_task_id}/events")
async def stream_build_task_events(build_task_id: str) -> StreamingResponse:
    async def event_stream():
        log_event(logger, logging.INFO, "sse.build.opened", build_task_id=build_task_id)
        previous_payload = None
        while True:
            with SessionLocal() as db:
                payload = BuildTaskService(db).build_event_payload(build_task_id).model_dump(mode="json")
            encoded_payload = json.dumps(payload, ensure_ascii=False)
            if encoded_payload != previous_payload:
                previous_payload = encoded_payload
                log_event(
                    logger,
                    logging.INFO,
                    "sse.build.emitted",
                    build_task_id=build_task_id,
                    status=payload["status"],
                    current_step=payload.get("current_step"),
                    total_tokens=payload["total_tokens"],
                )
                yield f"event: build_update\ndata: {encoded_payload}\n\n"
            if payload["status"] in TERMINAL_BUILD_STATUSES:
                log_event(logger, logging.INFO, "sse.build.closed", build_task_id=build_task_id, status=payload["status"])
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

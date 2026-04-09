from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.agent_system import (
    AgentNodeMCPBindingRequest,
    AgentNodeModelBindingRequest,
    AgentNodeResponse,
    AgentNodeSkillBindingRequest,
    AgentSystemResponse,
)
from app.services.agent_system_service import AgentSystemService

router = APIRouter(tags=["agent-system"])


@router.get("/projects/{project_id}/agent-system", response_model=AgentSystemResponse)
def get_project_agent_system(project_id: str, db: Session = Depends(get_db)) -> AgentSystemResponse:
    return AgentSystemService(db).get_or_initialize_project_agent_system(project_id)


@router.get("/agent-nodes/{agent_node_id}", response_model=AgentNodeResponse)
def get_agent_node(agent_node_id: str, db: Session = Depends(get_db)) -> AgentNodeResponse:
    return AgentSystemService(db).get_agent_node(agent_node_id)


@router.patch("/agent-nodes/{agent_node_id}/model", response_model=AgentNodeResponse)
def update_agent_node_model(
    agent_node_id: str,
    payload: AgentNodeModelBindingRequest,
    db: Session = Depends(get_db),
) -> AgentNodeResponse:
    return AgentSystemService(db).update_agent_node_model(agent_node_id, payload)


@router.patch("/agent-nodes/{agent_node_id}/skills", response_model=AgentNodeResponse)
def update_agent_node_skills(
    agent_node_id: str,
    payload: AgentNodeSkillBindingRequest,
    db: Session = Depends(get_db),
) -> AgentNodeResponse:
    return AgentSystemService(db).update_agent_node_skills(agent_node_id, payload)


@router.patch("/agent-nodes/{agent_node_id}/mcps", response_model=AgentNodeResponse)
def update_agent_node_mcps(
    agent_node_id: str,
    payload: AgentNodeMCPBindingRequest,
    db: Session = Depends(get_db),
) -> AgentNodeResponse:
    return AgentSystemService(db).update_agent_node_mcps(agent_node_id, payload)


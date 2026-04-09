from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.entities import AgentNode, AgentSystem


class AgentSystemRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, agent_system: AgentSystem) -> AgentSystem:
        self.db.add(agent_system)
        self.db.flush()
        self.db.refresh(agent_system)
        return agent_system

    def get_by_project_id(self, project_id: str, with_nodes: bool = False) -> Optional[AgentSystem]:
        statement = select(AgentSystem).where(AgentSystem.project_id == project_id)
        if with_nodes:
            statement = statement.options(
                selectinload(AgentSystem.agent_nodes).selectinload(AgentNode.skill_bindings),
                selectinload(AgentSystem.agent_nodes).selectinload(AgentNode.mcp_bindings),
                selectinload(AgentSystem.agent_nodes).selectinload(AgentNode.model_config),
            )
        return self.db.scalar(statement)


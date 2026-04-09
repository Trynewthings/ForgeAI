from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.entities import AgentNode


class AgentNodeRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, agent_node_id: str, with_relations: bool = False) -> Optional[AgentNode]:
        statement = select(AgentNode).where(AgentNode.id == agent_node_id)
        if with_relations:
            statement = statement.options(
                selectinload(AgentNode.skill_bindings),
                selectinload(AgentNode.mcp_bindings),
                selectinload(AgentNode.model_config),
            )
        return self.db.scalar(statement)


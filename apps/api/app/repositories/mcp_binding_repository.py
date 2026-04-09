from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.entities import MCPBinding


class MCPBindingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_node(self, agent_node_id: str, bindings: list[MCPBinding]) -> list[MCPBinding]:
        self.db.execute(delete(MCPBinding).where(MCPBinding.agent_node_id == agent_node_id))
        self.db.add_all(bindings)
        self.db.flush()
        return bindings


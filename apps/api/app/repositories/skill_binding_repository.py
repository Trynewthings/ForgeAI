from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.entities import SkillBinding


class SkillBindingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_node(self, agent_node_id: str, bindings: list[SkillBinding]) -> list[SkillBinding]:
        self.db.execute(delete(SkillBinding).where(SkillBinding.agent_node_id == agent_node_id))
        self.db.add_all(bindings)
        self.db.flush()
        return bindings


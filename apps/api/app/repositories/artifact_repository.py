from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.entities import Artifact


class ArtifactRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, artifact: Artifact) -> Artifact:
        self.db.add(artifact)
        self.db.flush()
        self.db.refresh(artifact)
        return artifact

    def list_by_build_task(self, build_task_id: str) -> list[Artifact]:
        statement = select(Artifact).where(Artifact.build_task_id == build_task_id).order_by(Artifact.created_at.asc())
        return list(self.db.scalars(statement).all())

    def list_by_run(self, run_id: str) -> list[Artifact]:
        statement = select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at.asc())
        return list(self.db.scalars(statement).all())

    def list_by_build(self, build_id: str) -> list[Artifact]:
        statement = select(Artifact).where(Artifact.build_id == build_id).order_by(Artifact.created_at.asc())
        return list(self.db.scalars(statement).all())

    def list_by_executable(self, executable_id: str) -> list[Artifact]:
        statement = select(Artifact).where(Artifact.executable_id == executable_id).order_by(Artifact.created_at.asc())
        return list(self.db.scalars(statement).all())

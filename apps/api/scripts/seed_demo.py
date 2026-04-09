from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.db.base import Base
from app.schemas.agent_system import AgentNodeMCPBindingRequest, AgentNodeModelBindingRequest, AgentNodeSkillBindingRequest, NamedConfigPayload
from app.schemas.build_task import BuildTaskCreateRequest
from app.schemas.model_config import ModelConfigCreateRequest
from app.schemas.project import ProjectCreateRequest
from app.schemas.requirement_session import RequirementMessageCreateRequest, RequirementSessionCreateRequest
from app.services.agent_system_service import AgentSystemService
from app.services.build_task_service import BuildTaskService
from app.services.model_config_service import ModelConfigService
from app.services.project_service import ProjectService
from app.services.requirement_session_service import RequirementSessionService


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        settings.build_execution_mode = "thread"
        settings.build_step_delay_seconds = 0.01

        project_service = ProjectService(db)
        requirement_service = RequirementSessionService(db)
        build_task_service = BuildTaskService(db)
        model_config_service = ModelConfigService(db)
        agent_system_service = AgentSystemService(db)

        project = project_service.create_project(
            ProjectCreateRequest(name="AgentForge Demo Project", description="Seeded demo project for local validation")
        )
        session = requirement_service.create_session(RequirementSessionCreateRequest(project_id=project.id))
        requirement_service.send_message(
            session.id,
            RequirementMessageCreateRequest(
                content=(
                    "Goal: Build an AI agent system builder for enterprise teams.\n"
                    "Target Users:\n- product managers\n- architects\n"
                    "Delivery Form: desktop application\n"
                    "P0 Features:\n- project creation\n- requirement clarification\n- build task tracking\n"
                    "Constraints:\n- no auth in MVP\n"
                    "Deployment: dockerized deployment\n"
                    "Quality Requirements:\n- auditability\n- reliability"
                )
            ),
        )
        finalized = requirement_service.finalize(session.id)

        model_config = model_config_service.create_model_config(
            ModelConfigCreateRequest(
                provider="local-endpoint",
                model_name="demo-local-model",
                endpoint="http://localhost:11434/api/generate",
                api_key_ref=None,
                temperature=0.2,
                max_tokens=1024,
            )
        )

        build_task = build_task_service.create_build_task(
            BuildTaskCreateRequest(project_id=project.id, requirement_session_id=finalized.session.id)
        )

        for _ in range(200):
            current = build_task_service.get_build_task(build_task.id)
            if current.status in {"completed", "failed"}:
                break
            time.sleep(0.05)

        agent_system = agent_system_service.get_or_initialize_project_agent_system(project.id)
        code_generator = next((node for node in agent_system.nodes if node.role == "code_generator"), None)
        if code_generator is not None:
            agent_system_service.update_agent_node_model(
                code_generator.id,
                AgentNodeModelBindingRequest(model_config_id=model_config.id),
            )
            agent_system_service.update_agent_node_skills(
                code_generator.id,
                AgentNodeSkillBindingRequest(
                    bindings=[
                        NamedConfigPayload(name="repo_scaffolder", config_json={"framework": "fastapi-react"}),
                        NamedConfigPayload(name="test_generator", config_json={"coverage_target": "core"}),
                    ]
                ),
            )
            agent_system_service.update_agent_node_mcps(
                code_generator.id,
                AgentNodeMCPBindingRequest(
                    bindings=[
                        NamedConfigPayload(name="filesystem", config_json={"root_path": "."}),
                        NamedConfigPayload(name="git", config_json={"repo_path": "."}),
                    ]
                ),
            )

        print("Seed data created successfully.")
        print(f"Project ID: {project.id}")
        print(f"Requirement Session ID: {finalized.session.id}")
        print(f"Build Task ID: {build_task.id}")
        print(f"Model Config ID: {model_config.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

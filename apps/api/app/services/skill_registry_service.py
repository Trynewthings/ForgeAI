from __future__ import annotations

from app.schemas.agent_system import SkillRegistryItemResponse

DEFAULT_SKILLS = [
    SkillRegistryItemResponse(
        name="repo_scaffolder",
        description="Generate or refine repository skeletons and developer boilerplate.",
        config_schema={"type": "object", "properties": {"framework": {"type": "string"}}},
    ),
    SkillRegistryItemResponse(
        name="requirements_summarizer",
        description="Summarize multi-turn requirement conversations into engineering language.",
        config_schema={"type": "object", "properties": {"tone": {"type": "string"}}},
    ),
    SkillRegistryItemResponse(
        name="test_generator",
        description="Draft focused test cases for the current module or build step.",
        config_schema={"type": "object", "properties": {"coverage_target": {"type": "string"}}},
    ),
]


class SkillRegistryService:
    def list_skills(self) -> list[SkillRegistryItemResponse]:
        return DEFAULT_SKILLS


from fastapi import APIRouter

from app.schemas.agent_system import MCPRegistryItemResponse, SkillRegistryItemResponse
from app.services.mcp_registry_service import MCPRegistryService
from app.services.skill_registry_service import SkillRegistryService

router = APIRouter(tags=["registries"])


@router.get("/skills", response_model=list[SkillRegistryItemResponse])
def list_skills() -> list[SkillRegistryItemResponse]:
    return SkillRegistryService().list_skills()


@router.get("/mcps", response_model=list[MCPRegistryItemResponse])
def list_mcps() -> list[MCPRegistryItemResponse]:
    return MCPRegistryService().list_mcps()


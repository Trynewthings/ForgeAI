from __future__ import annotations

from app.schemas.agent_system import MCPRegistryItemResponse

DEFAULT_MCPS = [
    MCPRegistryItemResponse(
        name="filesystem",
        description="Access and inspect local workspace files through a controlled MCP surface.",
        endpoint="local://filesystem",
        config_schema={"type": "object", "properties": {"root_path": {"type": "string"}}},
    ),
    MCPRegistryItemResponse(
        name="git",
        description="Inspect repository status, branches, and commit metadata.",
        endpoint="local://git",
        config_schema={"type": "object", "properties": {"repo_path": {"type": "string"}}},
    ),
    MCPRegistryItemResponse(
        name="docs-index",
        description="Query internal architecture notes or indexed project docs.",
        endpoint="local://docs-index",
        config_schema={"type": "object", "properties": {"collection": {"type": "string"}}},
    ),
]


class MCPRegistryService:
    def list_mcps(self) -> list[MCPRegistryItemResponse]:
        return DEFAULT_MCPS


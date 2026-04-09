from fastapi import APIRouter

from app.api.routes.blueprints import router as blueprints_router
from app.api.routes.builds import router as builds_router
from app.api.routes.capability_lifecycle import router as capability_lifecycle_router
from app.api.routes.capability_proposals import router as capability_proposals_router
from app.api.routes.capability_requests import router as capability_requests_router
from app.api.routes.executables import router as executables_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.projects import router as projects_router
from app.api.routes.provider_debug import router as provider_debug_router
from app.api.routes.requirements import router as requirements_v2_router
from app.api.routes.runtime_profiles import router as runtime_profiles_router
from app.api.routes.runs import router as runs_router
from app.api.v1.routers.agent_systems import router as agent_systems_router
from app.api.v1.routers.build_tasks import router as build_tasks_router
from app.api.v1.routers.health import router as health_router
from app.api.v1.routers.model_configs import router as model_configs_router
from app.api.v1.routers.registries import router as registries_router
from app.api.v1.routers.requirements import router as requirements_legacy_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router)
api_v1_router.include_router(projects_router)
api_v1_router.include_router(requirements_v2_router)
api_v1_router.include_router(blueprints_router)
api_v1_router.include_router(builds_router)
api_v1_router.include_router(capability_requests_router)
api_v1_router.include_router(capability_lifecycle_router)
api_v1_router.include_router(capability_proposals_router)
api_v1_router.include_router(executables_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(runtime_profiles_router)
api_v1_router.include_router(runs_router)
api_v1_router.include_router(provider_debug_router)

# Legacy routes kept for compatibility while v2 becomes the mainline.
api_v1_router.include_router(requirements_legacy_router)
api_v1_router.include_router(build_tasks_router)
api_v1_router.include_router(model_configs_router)
api_v1_router.include_router(agent_systems_router)
api_v1_router.include_router(registries_router)

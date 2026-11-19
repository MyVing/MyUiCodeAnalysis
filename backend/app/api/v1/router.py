"""v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import (
    analyses,
    api_usage,
    explore,
    graph,
    health,
    plugins,
    projects,
    requirements,
    runtime,
    source,
    specdocs,
    tasks,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(projects.router)
api_router.include_router(graph.router)
api_router.include_router(api_usage.router)
api_router.include_router(analyses.router)
api_router.include_router(tasks.router)
api_router.include_router(plugins.router)
api_router.include_router(requirements.router)
api_router.include_router(runtime.router)
api_router.include_router(explore.router)
api_router.include_router(source.router)
api_router.include_router(specdocs.router)

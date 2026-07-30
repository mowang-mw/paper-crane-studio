"""M2 HTTP 路由集合。"""

from fastapi import APIRouter

from .health import router as health_router
from .jobs import router as jobs_router
from .media import router as media_router
from .projects import router as projects_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(projects_router)
api_router.include_router(jobs_router)
api_router.include_router(media_router)

__all__ = ["api_router"]

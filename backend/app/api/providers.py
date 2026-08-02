"""ScriptProvider 列表与即时健康状态。"""

from fastapi import APIRouter, Request

from ..providers.registry import provider_registry
from ..schemas import ProvidersRead


router = APIRouter(tags=["providers"])


@router.get("/providers", response_model=ProvidersRead)
def get_providers(request: Request) -> ProvidersRead:
    return ProvidersRead.model_validate(provider_registry(request.app.state.settings))

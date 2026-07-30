"""服务、数据库与媒体工具就绪状态。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Request

from ..schemas import HealthRead


router = APIRouter(tags=["health"])


def _tool_available(command: str, environment_name: str) -> bool:
    configured = os.environ.get(environment_name)
    if configured:
        return Path(configured).expanduser().is_file()
    resolved = shutil.which(command)
    return bool(resolved and Path(resolved).is_file())


@router.get("/health", response_model=HealthRead)
def health(request: Request) -> HealthRead:
    database = request.app.state.database
    settings = request.app.state.settings
    return HealthRead(
        service="ok",
        database="ok" if database.is_healthy() else "error",
        ffmpeg_available=_tool_available("ffmpeg", "FFMPEG_BIN"),
        ffprobe_available=_tool_available("ffprobe", "FFPROBE_BIN"),
        data_dir=str(settings.data_dir),
        stage=settings.stage,
    )


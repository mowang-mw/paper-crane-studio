"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import api_router
from .config import Settings
from .database import Database


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    database = database or Database(str(settings.database_url))
    database.create_schema()

    app = FastAPI(
        title="AnimeFlow M5-B API",
        version="0.5.0",
        description=(
            "本地 Script/Image/Audio Provider 分阶段复用 + FFmpeg 纵向链路"
        ),
    )
    app.state.settings = settings
    app.state.database = database
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()

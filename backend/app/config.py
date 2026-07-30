"""M2 本地单用户运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sqlite_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path.resolve().as_posix()}"


@dataclass(frozen=True, slots=True)
class Settings:
    """可由环境变量加载，也可在测试中直接注入临时目录。"""

    root_dir: Path = field(default_factory=_default_root)
    data_dir: Path | None = None
    database_url: str | None = None
    api_prefix: str = "/api"
    stage: str = "M2"
    worker_poll_seconds: float = 1.0
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    def __post_init__(self) -> None:
        root_dir = Path(self.root_dir).resolve()
        data_dir = Path(self.data_dir or root_dir / "data").resolve()
        database_url = self.database_url or _sqlite_url(data_dir / "app.db")
        object.__setattr__(self, "root_dir", root_dir)
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "database_url", database_url)

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.environ.get("ANIME_PLATFORM_ROOT", _default_root()))
        configured_data = os.environ.get("ANIME_PLATFORM_DATA_DIR")
        data = Path(configured_data) if configured_data else root / "data"
        origins = tuple(
            item.strip()
            for item in os.environ.get(
                "ANIME_PLATFORM_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
            if item.strip()
        )
        return cls(
            root_dir=root,
            data_dir=data,
            database_url=os.environ.get("ANIME_PLATFORM_DATABASE_URL"),
            worker_poll_seconds=float(
                os.environ.get("ANIME_PLATFORM_WORKER_POLL_SECONDS", "1.0")
            ),
            cors_origins=origins,
        )

    @classmethod
    def for_data_dir(cls, data_dir: Path, *, root_dir: Path | None = None) -> "Settings":
        return cls(root_dir=root_dir or _default_root(), data_dir=data_dir)

    def ensure_directories(self) -> None:
        assert self.data_dir is not None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "projects").mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "projects" / project_id


"""M2 本地单用户运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


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
    stage: str = "M3"
    worker_poll_seconds: float = 1.0
    script_provider: str = "mock"
    llama_server_base_url: str = "http://127.0.0.1:8081"
    llama_model_id: str = "Qwen3-4B-Q4_K_M.gguf"
    llama_model_path: Path | None = None
    llama_timeout_seconds: float = 180.0
    llama_health_timeout_seconds: float = 2.0
    llama_temperature: float = 0.1
    llama_top_p: float = 0.9
    llama_max_tokens: int = 4096
    llama_prompt_version: str = "script-v1-qwen3-nonthinking-v3"
    llama_context_size: int = 8192
    llama_model_sha256: str | None = None
    llama_server_version: str = "unknown"
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    def __post_init__(self) -> None:
        root_dir = Path(self.root_dir).resolve()
        data_dir = Path(self.data_dir or root_dir / "data").resolve()
        database_url = self.database_url or _sqlite_url(data_dir / "app.db")
        model_path = Path(
            self.llama_model_path or root_dir / "models" / "text" / "Qwen3-4B-Q4_K_M.gguf"
        ).resolve()
        provider = self.script_provider.strip().lower()
        if provider not in {"mock", "llamacpp"}:
            raise ValueError("SCRIPT_PROVIDER 只允许 mock 或 llamacpp")
        base_url = self.llama_server_base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LLAMA_SERVER_BASE_URL 必须是无凭据的本机回环 HTTP 地址")
        if self.llama_timeout_seconds <= 0 or self.llama_health_timeout_seconds <= 0:
            raise ValueError("llama.cpp 超时必须大于 0")
        if not 0 <= self.llama_temperature <= 2:
            raise ValueError("LLAMA_TEMPERATURE 必须在 0—2 之间")
        if not 0 < self.llama_top_p <= 1:
            raise ValueError("LLAMA_TOP_P 必须在 0—1 之间")
        if self.llama_max_tokens < 256:
            raise ValueError("LLAMA_MAX_TOKENS 至少为 256")
        if self.llama_context_size < 512:
            raise ValueError("LLAMA_CONTEXT_SIZE 至少为 512")
        object.__setattr__(self, "root_dir", root_dir)
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "database_url", database_url)
        object.__setattr__(self, "script_provider", provider)
        object.__setattr__(self, "llama_server_base_url", base_url)
        object.__setattr__(self, "llama_model_path", model_path)

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
            script_provider=os.environ.get("SCRIPT_PROVIDER", "mock"),
            llama_server_base_url=os.environ.get(
                "LLAMA_SERVER_BASE_URL", "http://127.0.0.1:8081"
            ),
            llama_model_id=os.environ.get(
                "LLAMA_MODEL_ID", "Qwen3-4B-Q4_K_M.gguf"
            ),
            llama_model_path=Path(
                os.environ.get(
                    "LLAMA_MODEL_PATH",
                    root / "models" / "text" / "Qwen3-4B-Q4_K_M.gguf",
                )
            ),
            llama_timeout_seconds=float(
                os.environ.get("LLAMA_TIMEOUT_SECONDS", "180")
            ),
            llama_health_timeout_seconds=float(
                os.environ.get("LLAMA_HEALTH_TIMEOUT_SECONDS", "2")
            ),
            llama_temperature=float(os.environ.get("LLAMA_TEMPERATURE", "0.1")),
            llama_top_p=float(os.environ.get("LLAMA_TOP_P", "0.9")),
            llama_max_tokens=int(os.environ.get("LLAMA_MAX_TOKENS", "4096")),
            llama_prompt_version=os.environ.get(
                "LLAMA_PROMPT_VERSION", "script-v1-qwen3-nonthinking-v3"
            ),
            llama_context_size=int(os.environ.get("LLAMA_CONTEXT_SIZE", "8192")),
            llama_model_sha256=os.environ.get("LLAMA_MODEL_SHA256") or None,
            llama_server_version=os.environ.get("LLAMA_SERVER_VERSION", "unknown"),
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

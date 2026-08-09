"""M2 本地单用户运行配置。"""

from __future__ import annotations

import os
import re
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
    stage: str = "M5-B"
    worker_poll_seconds: float = 1.0
    script_provider: str = "mock"
    llama_server_base_url: str = "http://127.0.0.1:8081"
    llama_server_executable: Path | None = None
    llama_model_id: str = "Qwen3-4B-Q4_K_M.gguf"
    llama_model_path: Path | None = None
    llama_timeout_seconds: float = 180.0
    llama_health_timeout_seconds: float = 2.0
    llama_startup_timeout_seconds: float = 120.0
    llama_temperature: float = 0.1
    llama_top_p: float = 0.9
    llama_max_tokens: int = 4096
    llama_prompt_version: str = "script-v1-qwen3-nonthinking-v3"
    llama_context_size: int = 8192
    llama_gpu_layers: int = 99
    llama_model_sha256: str | None = None
    llama_server_version: str = "unknown"
    image_provider: str = "mock"
    comfyui_python: Path | None = None
    comfyui_root: Path | None = None
    comfyui_host: str = "127.0.0.1"
    comfyui_port: int = 8188
    comfyui_model_path: Path | None = None
    comfyui_model_id: str = "cagliostrolab/animagine-xl-4.0"
    comfyui_model_sha256: str = (
        "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
    )
    comfyui_model_license: str = "CreativeML Open RAIL++-M"
    comfyui_expected_commit: str = "f06a187f50f896e4a0ba5be1ce1f2d2dcd13b77b"
    comfyui_startup_timeout_seconds: float = 240.0
    comfyui_image_timeout_seconds: float = 600.0
    comfyui_job_timeout_seconds: float = 3600.0
    comfyui_http_timeout_seconds: float = 30.0
    image_width: int = 1024
    image_height: int = 576
    image_steps: int = 24
    image_cfg: float = 5.0
    image_sampler: str = "euler_ancestral"
    image_scheduler: str = "normal"
    image_base_seed: int = 20_260_802
    audio_provider: str = "mock"
    qwen_tts_python: Path | None = None
    qwen_tts_runner: Path | None = None
    qwen_tts_model_path: Path | None = None
    qwen_tts_model_id: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    qwen_tts_model_revision: str = (
        "85e237c12c027371202489a0ec509ded67b5e4b5"
    )
    qwen_tts_model_sha256: str = (
        "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb"
    )
    qwen_tts_tokenizer_sha256: str = (
        "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"
    )
    qwen_tts_model_license: str = "Apache-2.0"
    qwen_tts_package_version: str = "0.1.1"
    qwen_tts_default_speaker: str = "Serena"
    qwen_tts_language: str = "Chinese"
    qwen_tts_seed: int = 20_260_803
    qwen_tts_model_load_timeout_seconds: float = 300.0
    qwen_tts_shot_timeout_seconds: float = 300.0
    qwen_tts_job_timeout_seconds: float = 1_200.0
    qwen_tts_gpu_release_timeout_seconds: float = 60.0
    audio_gpu_handoff_max_used_mib: int = 2_048
    audio_lead_in_seconds: float = 0.20
    audio_lead_out_seconds: float = 0.35
    audio_rendered_max_seconds: float = 60.0
    dashscope_api_key: str | None = field(default=None, repr=False)
    dashscope_workspace_id: str | None = None
    dashscope_region: str = "beijing"
    cloud_wan_model_id: str = "wan2.7-i2v-2026-04-25"
    cloud_wan_poll_interval_seconds: float = 15.0
    cloud_wan_timeout_seconds: float = 1_800.0
    cloud_wan_http_timeout_seconds: float = 30.0
    cloud_wan_max_source_image_bytes: int = 10 * 1024 * 1024
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    def __post_init__(self) -> None:
        root_dir = Path(self.root_dir).resolve()
        data_dir = Path(self.data_dir or root_dir / "data").resolve()
        database_url = self.database_url or _sqlite_url(data_dir / "app.db")
        server_executable = Path(
            self.llama_server_executable
            or root_dir / "tools" / "llama.cpp" / "llama-server.exe"
        ).resolve()
        model_path = Path(
            self.llama_model_path or root_dir / "models" / "text" / "Qwen3-4B-Q4_K_M.gguf"
        ).resolve()
        comfyui_root = Path(self.comfyui_root or root_dir / "tools" / "ComfyUI").resolve()
        comfyui_python = Path(
            self.comfyui_python
            or root_dir / ".venv-comfyui" / "Scripts" / "python.exe"
        ).resolve()
        comfyui_model_path = Path(
            self.comfyui_model_path
            or root_dir / "models" / "image" / "animagine-xl-4.0-opt.safetensors"
        ).resolve()
        qwen_tts_python = Path(
            self.qwen_tts_python
            or root_dir / ".venv-qwen3-tts" / "python.exe"
        ).resolve()
        qwen_tts_runner = Path(
            self.qwen_tts_runner
            or root_dir / "scripts" / "qwen3_tts_job_runner.py"
        ).resolve()
        qwen_tts_model_path = Path(
            self.qwen_tts_model_path
            or root_dir
            / "models"
            / "audio"
            / "Qwen3-TTS-12Hz-0.6B-CustomVoice"
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
        if any(
            value <= 0
            for value in (
                self.llama_timeout_seconds,
                self.llama_health_timeout_seconds,
                self.llama_startup_timeout_seconds,
            )
        ):
            raise ValueError("llama.cpp 超时必须大于 0")
        if not 0 <= self.llama_temperature <= 2:
            raise ValueError("LLAMA_TEMPERATURE 必须在 0—2 之间")
        if not 0 < self.llama_top_p <= 1:
            raise ValueError("LLAMA_TOP_P 必须在 0—1 之间")
        if self.llama_max_tokens < 256:
            raise ValueError("LLAMA_MAX_TOKENS 至少为 256")
        if self.llama_context_size < 512:
            raise ValueError("LLAMA_CONTEXT_SIZE 至少为 512")
        if self.llama_gpu_layers < 0:
            raise ValueError("LLAMA_GPU_LAYERS 不得为负数")
        image_provider = self.image_provider.strip().lower()
        if image_provider not in {"mock", "comfyui-animagine-xl-4"}:
            raise ValueError(
                "IMAGE_PROVIDER 只允许 mock 或 comfyui-animagine-xl-4"
            )
        if self.comfyui_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("COMFYUI_HOST 必须是本机回环地址")
        if not 1 <= self.comfyui_port <= 65535:
            raise ValueError("COMFYUI_PORT 必须在 1—65535 之间")
        if any(
            value <= 0
            for value in (
                self.comfyui_startup_timeout_seconds,
                self.comfyui_image_timeout_seconds,
                self.comfyui_job_timeout_seconds,
                self.comfyui_http_timeout_seconds,
            )
        ):
            raise ValueError("ComfyUI 所有超时必须大于 0")
        if self.comfyui_job_timeout_seconds < self.comfyui_image_timeout_seconds:
            raise ValueError("ComfyUI Job 总超时不得小于单图超时")
        if self.image_width <= 0 or self.image_height <= 0 or self.image_steps <= 0:
            raise ValueError("真实图像宽、高和 steps 必须大于 0")
        if self.image_cfg <= 0:
            raise ValueError("真实图像 CFG 必须大于 0")
        if self.image_base_seed < 0:
            raise ValueError("IMAGE_BASE_SEED 不得为负数")
        audio_provider = self.audio_provider.strip().lower()
        if audio_provider not in {"mock", "qwen3-tts-0.6b-customvoice"}:
            raise ValueError(
                "AUDIO_PROVIDER 只允许 mock 或 qwen3-tts-0.6b-customvoice"
            )
        if self.qwen_tts_default_speaker not in {"Serena", "Vivian"}:
            raise ValueError("QWEN_TTS_DEFAULT_SPEAKER 只允许 Serena 或 Vivian")
        if self.qwen_tts_language != "Chinese":
            raise ValueError("M5-B 的 QWEN_TTS_LANGUAGE 固定为 Chinese")
        if self.qwen_tts_seed < 0:
            raise ValueError("QWEN_TTS_SEED 不得为负数")
        if any(
            value <= 0
            for value in (
                self.qwen_tts_model_load_timeout_seconds,
                self.qwen_tts_shot_timeout_seconds,
                self.qwen_tts_job_timeout_seconds,
                self.qwen_tts_gpu_release_timeout_seconds,
                self.audio_rendered_max_seconds,
            )
        ):
            raise ValueError("Qwen3-TTS 超时与最终媒体上限必须大于 0")
        if self.audio_gpu_handoff_max_used_mib <= 0:
            raise ValueError("AUDIO_GPU_HANDOFF_MAX_USED_MIB 必须大于 0")
        if self.qwen_tts_job_timeout_seconds < self.qwen_tts_shot_timeout_seconds:
            raise ValueError("Qwen3-TTS Job 总超时不得小于单镜头超时")
        if self.audio_lead_in_seconds < 0 or self.audio_lead_out_seconds < 0:
            raise ValueError("真实旁白 lead-in/lead-out 不得为负数")
        dashscope_api_key = (self.dashscope_api_key or "").strip() or None
        dashscope_workspace_id = (self.dashscope_workspace_id or "").strip() or None
        dashscope_region = self.dashscope_region.strip().lower()
        if dashscope_region != "beijing":
            raise ValueError("DASHSCOPE_REGION 当前只支持 beijing")
        if dashscope_workspace_id is not None and not re.fullmatch(
            r"[A-Za-z0-9-]+", dashscope_workspace_id
        ):
            raise ValueError("DASHSCOPE_WORKSPACE_ID 必须是安全的 DNS 标签")
        if any(
            value <= 0
            for value in (
                self.cloud_wan_poll_interval_seconds,
                self.cloud_wan_timeout_seconds,
                self.cloud_wan_http_timeout_seconds,
                self.cloud_wan_max_source_image_bytes,
            )
        ):
            raise ValueError("Wan 云视频超时、轮询间隔和图片上限必须大于 0")
        if self.cloud_wan_timeout_seconds < self.cloud_wan_http_timeout_seconds:
            raise ValueError("Wan 总超时不得小于单次 HTTP 超时")
        object.__setattr__(self, "root_dir", root_dir)
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "database_url", database_url)
        object.__setattr__(self, "script_provider", provider)
        object.__setattr__(self, "llama_server_base_url", base_url)
        object.__setattr__(self, "llama_server_executable", server_executable)
        object.__setattr__(self, "llama_model_path", model_path)
        object.__setattr__(self, "image_provider", image_provider)
        object.__setattr__(self, "comfyui_root", comfyui_root)
        object.__setattr__(self, "comfyui_python", comfyui_python)
        object.__setattr__(self, "comfyui_model_path", comfyui_model_path)
        object.__setattr__(self, "audio_provider", audio_provider)
        object.__setattr__(self, "qwen_tts_python", qwen_tts_python)
        object.__setattr__(self, "qwen_tts_runner", qwen_tts_runner)
        object.__setattr__(self, "qwen_tts_model_path", qwen_tts_model_path)
        object.__setattr__(self, "dashscope_api_key", dashscope_api_key)
        object.__setattr__(self, "dashscope_workspace_id", dashscope_workspace_id)
        object.__setattr__(self, "dashscope_region", dashscope_region)

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
            llama_server_executable=Path(
                os.environ.get(
                    "LLAMA_SERVER_BIN",
                    root / "tools" / "llama.cpp" / "llama-server.exe",
                )
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
            llama_startup_timeout_seconds=float(
                os.environ.get("LLAMA_STARTUP_TIMEOUT_SECONDS", "120")
            ),
            llama_temperature=float(os.environ.get("LLAMA_TEMPERATURE", "0.1")),
            llama_top_p=float(os.environ.get("LLAMA_TOP_P", "0.9")),
            llama_max_tokens=int(os.environ.get("LLAMA_MAX_TOKENS", "4096")),
            llama_prompt_version=os.environ.get(
                "LLAMA_PROMPT_VERSION", "script-v1-qwen3-nonthinking-v3"
            ),
            llama_context_size=int(os.environ.get("LLAMA_CONTEXT_SIZE", "8192")),
            llama_gpu_layers=int(os.environ.get("LLAMA_GPU_LAYERS", "99")),
            llama_model_sha256=os.environ.get("LLAMA_MODEL_SHA256") or None,
            llama_server_version=os.environ.get("LLAMA_SERVER_VERSION", "unknown"),
            image_provider=os.environ.get("IMAGE_PROVIDER", "mock"),
            comfyui_python=Path(
                os.environ.get(
                    "COMFYUI_PYTHON",
                    root / ".venv-comfyui" / "Scripts" / "python.exe",
                )
            ),
            comfyui_root=Path(
                os.environ.get("COMFYUI_ROOT", root / "tools" / "ComfyUI")
            ),
            comfyui_host=os.environ.get("COMFYUI_HOST", "127.0.0.1"),
            comfyui_port=int(os.environ.get("COMFYUI_PORT", "8188")),
            comfyui_model_path=Path(
                os.environ.get(
                    "COMFYUI_MODEL_PATH",
                    root / "models" / "image" / "animagine-xl-4.0-opt.safetensors",
                )
            ),
            comfyui_model_id=os.environ.get(
                "COMFYUI_MODEL_ID", "cagliostrolab/animagine-xl-4.0"
            ),
            comfyui_model_sha256=os.environ.get(
                "COMFYUI_MODEL_SHA256",
                "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac",
            ),
            comfyui_model_license=os.environ.get(
                "COMFYUI_MODEL_LICENSE", "CreativeML Open RAIL++-M"
            ),
            comfyui_expected_commit=os.environ.get(
                "COMFYUI_EXPECTED_COMMIT",
                "f06a187f50f896e4a0ba5be1ce1f2d2dcd13b77b",
            ),
            comfyui_startup_timeout_seconds=float(
                os.environ.get("COMFYUI_STARTUP_TIMEOUT_SECONDS", "240")
            ),
            comfyui_image_timeout_seconds=float(
                os.environ.get("COMFYUI_IMAGE_TIMEOUT_SECONDS", "600")
            ),
            comfyui_job_timeout_seconds=float(
                os.environ.get("COMFYUI_JOB_TIMEOUT_SECONDS", "3600")
            ),
            comfyui_http_timeout_seconds=float(
                os.environ.get("COMFYUI_HTTP_TIMEOUT_SECONDS", "30")
            ),
            image_width=int(os.environ.get("IMAGE_WIDTH", "1024")),
            image_height=int(os.environ.get("IMAGE_HEIGHT", "576")),
            image_steps=int(os.environ.get("IMAGE_STEPS", "24")),
            image_cfg=float(os.environ.get("IMAGE_CFG", "5")),
            image_sampler=os.environ.get("IMAGE_SAMPLER", "euler_ancestral"),
            image_scheduler=os.environ.get("IMAGE_SCHEDULER", "normal"),
            image_base_seed=int(os.environ.get("IMAGE_BASE_SEED", "20260802")),
            audio_provider=os.environ.get("AUDIO_PROVIDER", "mock"),
            qwen_tts_python=Path(
                os.environ.get(
                    "QWEN_TTS_PYTHON",
                    root / ".venv-qwen3-tts" / "python.exe",
                )
            ),
            qwen_tts_runner=Path(
                os.environ.get(
                    "QWEN_TTS_RUNNER",
                    root / "scripts" / "qwen3_tts_job_runner.py",
                )
            ),
            qwen_tts_model_path=Path(
                os.environ.get(
                    "QWEN_TTS_MODEL_PATH",
                    root
                    / "models"
                    / "audio"
                    / "Qwen3-TTS-12Hz-0.6B-CustomVoice",
                )
            ),
            qwen_tts_model_id=os.environ.get(
                "QWEN_TTS_MODEL_ID",
                "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
            ),
            qwen_tts_model_revision=os.environ.get(
                "QWEN_TTS_MODEL_REVISION",
                "85e237c12c027371202489a0ec509ded67b5e4b5",
            ),
            qwen_tts_model_sha256=os.environ.get(
                "QWEN_TTS_MODEL_SHA256",
                "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb",
            ),
            qwen_tts_tokenizer_sha256=os.environ.get(
                "QWEN_TTS_TOKENIZER_SHA256",
                "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
            ),
            qwen_tts_model_license=os.environ.get(
                "QWEN_TTS_MODEL_LICENSE", "Apache-2.0"
            ),
            qwen_tts_package_version=os.environ.get(
                "QWEN_TTS_PACKAGE_VERSION", "0.1.1"
            ),
            qwen_tts_default_speaker=os.environ.get(
                "QWEN_TTS_DEFAULT_SPEAKER", "Serena"
            ),
            qwen_tts_language=os.environ.get("QWEN_TTS_LANGUAGE", "Chinese"),
            qwen_tts_seed=int(os.environ.get("QWEN_TTS_SEED", "20260803")),
            qwen_tts_model_load_timeout_seconds=float(
                os.environ.get("QWEN_TTS_MODEL_LOAD_TIMEOUT_SECONDS", "300")
            ),
            qwen_tts_shot_timeout_seconds=float(
                os.environ.get("QWEN_TTS_SHOT_TIMEOUT_SECONDS", "300")
            ),
            qwen_tts_job_timeout_seconds=float(
                os.environ.get("QWEN_TTS_JOB_TIMEOUT_SECONDS", "1200")
            ),
            qwen_tts_gpu_release_timeout_seconds=float(
                os.environ.get("QWEN_TTS_GPU_RELEASE_TIMEOUT_SECONDS", "60")
            ),
            audio_gpu_handoff_max_used_mib=int(
                os.environ.get("AUDIO_GPU_HANDOFF_MAX_USED_MIB", "2048")
            ),
            audio_lead_in_seconds=float(
                os.environ.get("AUDIO_LEAD_IN_SECONDS", "0.20")
            ),
            audio_lead_out_seconds=float(
                os.environ.get("AUDIO_LEAD_OUT_SECONDS", "0.35")
            ),
            audio_rendered_max_seconds=float(
                os.environ.get("AUDIO_RENDERED_MAX_SECONDS", "60")
            ),
            dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY") or None,
            dashscope_workspace_id=os.environ.get("DASHSCOPE_WORKSPACE_ID") or None,
            dashscope_region=os.environ.get("DASHSCOPE_REGION", "beijing"),
            cloud_wan_poll_interval_seconds=float(
                os.environ.get("CLOUD_WAN_POLL_INTERVAL_SECONDS", "15")
            ),
            cloud_wan_timeout_seconds=float(
                os.environ.get("CLOUD_WAN_TIMEOUT_SECONDS", "1800")
            ),
            cloud_wan_http_timeout_seconds=float(
                os.environ.get("CLOUD_WAN_HTTP_TIMEOUT_SECONDS", "30")
            ),
            cloud_wan_max_source_image_bytes=int(
                os.environ.get("CLOUD_WAN_MAX_SOURCE_IMAGE_BYTES", str(10 * 1024 * 1024))
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

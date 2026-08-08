"""M2 API 的 Pydantic 请求与响应契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import JobStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    story: str

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不得只包含空白字符")
        return value

    @field_validator("story")
    @classmethod
    def validate_story_length(cls, value: str) -> str:
        value = value.strip()
        length = len(value)
        if length < 10:
            raise ValueError(
                f"故事过短：去除首尾空白后共 {length} 个字符，至少需要 10 个字符"
            )
        if length > 3000:
            raise ValueError(
                f"故事过长：去除首尾空白后共 {length} 个字符，最多允许 3000 个字符"
            )
        return value


class ProjectRead(ApiModel):
    id: str
    title: str
    story: str
    status: str
    script_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ShotRead(ApiModel):
    id: str
    project_id: str
    shot_index: int
    title: str
    visual_description: str
    narration: str
    duration_seconds: float
    provider_id: str
    parameters_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class JobRead(ApiModel):
    id: str
    project_id: str
    job_type: str
    status: JobStatus
    progress: int
    provider_id: str
    request_json: dict[str, Any]
    result_json: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ExportRead(ApiModel):
    id: str
    project_id: str
    job_id: str
    file_path: str
    manifest_path: str
    duration_seconds: float
    sha256: str
    created_at: datetime
    video_url: str
    download_url: str
    manifest_url: str
    poster_url: str


class ProjectDetail(BaseModel):
    project: ProjectRead
    shots: list[ShotRead]
    recent_jobs: list[JobRead]
    latest_export: ExportRead | None


class JobQueued(BaseModel):
    job_id: str
    status: Literal["QUEUED"] = "QUEUED"


class MediaPolishOptions(BaseModel):
    motion_preset: Literal["static", "gentle_zoom", "cinematic_pan"] = (
        "gentle_zoom"
    )
    background_audio_enabled: bool = False
    background_volume: float = Field(default=0.12, ge=0.02, le=0.35)


class GenerationRequest(MediaPolishOptions):
    script_provider: Literal["mock", "llamacpp"] | None = None
    desired_shot_count: Literal[3, 4, 5] | None = 4


class RealImageRenderRequest(MediaPolishOptions):
    source_script_job_id: str = Field(min_length=1, max_length=36)
    image_provider: Literal["comfyui-animagine-xl-4"] = (
        "comfyui-animagine-xl-4"
    )
    base_seed: int | None = Field(default=None, ge=0, le=2**63 - 6)


class RealAudioRenderRequest(MediaPolishOptions):
    source_image_job_id: str = Field(min_length=1, max_length=36)
    audio_provider: Literal["qwen3-tts-0.6b-customvoice"] = (
        "qwen3-tts-0.6b-customvoice"
    )
    speaker: Literal["Serena", "Vivian"] = "Serena"
    language: Literal["Chinese"] = "Chinese"


class MediaRerenderRequest(MediaPolishOptions):
    source_audio_job_id: str = Field(min_length=1, max_length=36)


class BackgroundAudioRead(BaseModel):
    asset_id: str
    original_filename: str
    mime_type: str
    format: Literal["wav", "mp3", "m4a", "ogg"]
    duration_seconds: float
    size_bytes: int
    sha256: str
    storage_path: str
    source_type: Literal["USER_UPLOAD"] = "USER_UPLOAD"
    codec_name: str
    sample_rate: int | None = None
    channels: int | None = None
    rights_notice: str


class ProviderStatusRead(BaseModel):
    provider_id: Literal["mock", "llamacpp"]
    display_name: str
    available: bool
    configured: bool
    model_id: str
    source_type: Literal["MOCK", "LOCAL_MODEL"]
    server_version: str | None = None
    runtime_state: Literal[
        "READY_TO_START",
        "ONLINE",
        "CONFIG_ERROR",
        "PORT_CONFLICT",
        "NOT_APPLICABLE",
    ]
    detail: str | None = None


class ImageProviderStatusRead(BaseModel):
    provider_id: Literal["mock", "comfyui-animagine-xl-4"]
    display_name: str
    available: bool
    configured: bool
    model_id: str
    source_type: Literal["MOCK", "LOCAL_MODEL"]
    detail: str | None = None
    requires_gpu_handoff: bool = False


class AudioProviderStatusRead(BaseModel):
    provider_id: Literal["mock", "qwen3-tts-0.6b-customvoice"]
    display_name: str
    available: bool
    configured: bool
    model_id: str
    source_type: Literal["MOCK", "LOCAL_MODEL"]
    detail: str | None = None
    requires_gpu_handoff: bool = False
    speakers: list[Literal["Serena", "Vivian"]]
    default_speaker: Literal["Serena", "Vivian"]
    language: Literal["Chinese"]


class ProvidersRead(BaseModel):
    default_script_provider: Literal["mock", "llamacpp"]
    checked_at: datetime
    providers: list[ProviderStatusRead]
    default_image_provider: Literal["mock", "comfyui-animagine-xl-4"]
    image_providers: list[ImageProviderStatusRead]
    default_audio_provider: Literal["mock", "qwen3-tts-0.6b-customvoice"]
    audio_providers: list[AudioProviderStatusRead]


class HealthRead(BaseModel):
    service: Literal["ok"]
    database: Literal["ok", "error"]
    ffmpeg_available: bool
    ffprobe_available: bool
    data_dir: str
    stage: str

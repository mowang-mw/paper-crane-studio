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


class ProjectDetail(BaseModel):
    project: ProjectRead
    shots: list[ShotRead]
    recent_jobs: list[JobRead]
    latest_export: ExportRead | None


class JobQueued(BaseModel):
    job_id: str
    status: Literal["QUEUED"] = "QUEUED"


class GenerationRequest(BaseModel):
    script_provider: Literal["mock", "llamacpp"] | None = None
    desired_shot_count: Literal[3, 4, 5] | None = 4


class RealImageRenderRequest(BaseModel):
    source_script_job_id: str = Field(min_length=1, max_length=36)
    image_provider: Literal["comfyui-animagine-xl-4"] = (
        "comfyui-animagine-xl-4"
    )
    base_seed: int | None = Field(default=None, ge=0, le=2**63 - 6)


class ProviderStatusRead(BaseModel):
    provider_id: Literal["mock", "llamacpp"]
    display_name: str
    available: bool
    configured: bool
    model_id: str
    source_type: Literal["MOCK", "LOCAL_MODEL"]
    server_version: str | None = None
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


class ProvidersRead(BaseModel):
    default_script_provider: Literal["mock", "llamacpp"]
    checked_at: datetime
    providers: list[ProviderStatusRead]
    default_image_provider: Literal["mock", "comfyui-animagine-xl-4"]
    image_providers: list[ImageProviderStatusRead]


class HealthRead(BaseModel):
    service: Literal["ok"]
    database: Literal["ok", "error"]
    ffmpeg_available: bool
    ffprobe_available: bool
    data_dir: str
    stage: str

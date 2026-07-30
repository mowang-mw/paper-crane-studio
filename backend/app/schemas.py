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
    story: str = Field(min_length=1, max_length=20_000)

    @field_validator("title", "story")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不得只包含空白字符")
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


class HealthRead(BaseModel):
    service: Literal["ok"]
    database: Literal["ok", "error"]
    ffmpeg_available: bool
    ffprobe_available: bool
    data_dir: str
    stage: str

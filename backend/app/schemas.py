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


class VisualSelectionRead(BaseModel):
    source_image_asset_ids: dict[str, str] = Field(default_factory=dict)
    source_video_job_id: str | None = None


class VisualSelectionUpdate(VisualSelectionRead):
    @field_validator("source_image_asset_ids")
    @classmethod
    def validate_source_image_asset_ids(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 5:
            raise ValueError("最多只能为 5 个镜头选择当前关键帧")
        normalized: dict[str, str] = {}
        for shot_id, asset_id in value.items():
            shot_id = shot_id.strip()
            asset_id = asset_id.strip()
            if not shot_id or not asset_id or len(shot_id) > 120 or len(asset_id) > 36:
                raise ValueError("source_image_asset_ids 包含无效镜头或资产 ID")
            normalized[shot_id] = asset_id
        return normalized


class ProjectDetail(BaseModel):
    project: ProjectRead
    shots: list[ShotRead]
    recent_jobs: list[JobRead]
    video_jobs: list[JobRead] = Field(default_factory=list)
    latest_export: ExportRead | None
    image_assets: list["ImageAssetRead"] = Field(default_factory=list)
    visual_selection: VisualSelectionRead = Field(default_factory=VisualSelectionRead)


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
    source_video_job_id: str | None = Field(default=None, min_length=1, max_length=36)
    source_image_asset_ids: dict[str, str] = Field(default_factory=dict)
    audio_provider: Literal["qwen3-tts-0.6b-customvoice"] = (
        "qwen3-tts-0.6b-customvoice"
    )
    speaker: Literal["Serena", "Vivian"] = "Serena"
    language: Literal["Chinese"] = "Chinese"

    @field_validator("source_image_asset_ids")
    @classmethod
    def validate_source_image_asset_ids(cls, value: dict[str, str]) -> dict[str, str]:
        return VisualSelectionUpdate.validate_source_image_asset_ids(value)


class VideoRenderRequest(BaseModel):
    source_image_job_id: str | None = Field(default=None, min_length=1, max_length=36)
    source_image_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    source_image_asset_ids: dict[str, str] = Field(default_factory=dict)
    target_shot_ids: list[str] | None = None
    video_provider: Literal["mock-video", "cloud-wan-2.7"] = "mock-video"
    duration_seconds: float = Field(default=2.0, gt=0, le=60)
    motion_preset: Literal["static", "gentle_zoom", "cinematic_pan"] = (
        "gentle_zoom"
    )

    @field_validator("source_image_asset_ids")
    @classmethod
    def validate_source_image_asset_ids(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 5:
            raise ValueError("最多只能为 5 个镜头选择首帧资产")
        normalized: dict[str, str] = {}
        for shot_id, asset_id in value.items():
            shot_id = shot_id.strip()
            asset_id = asset_id.strip()
            if not shot_id or not asset_id or len(shot_id) > 120 or len(asset_id) > 36:
                raise ValueError("source_image_asset_ids 包含无效镜头或资产 ID")
            normalized[shot_id] = asset_id
        return normalized

    @field_validator("target_shot_ids")
    @classmethod
    def validate_target_shot_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [shot_id.strip() for shot_id in value]
        if not normalized or len(normalized) > 5:
            raise ValueError("target_shot_ids 必须包含 1—5 个镜头")
        if any(not shot_id or len(shot_id) > 120 for shot_id in normalized):
            raise ValueError("target_shot_ids 包含无效镜头 ID")
        if len(set(normalized)) != len(normalized):
            raise ValueError("target_shot_ids 不能重复")
        return normalized


class MediaRerenderRequest(MediaPolishOptions):
    source_audio_job_id: str = Field(min_length=1, max_length=36)
    source_video_job_id: str | None = Field(default=None, min_length=1, max_length=36)
    source_image_asset_ids: dict[str, str] = Field(default_factory=dict)

    @field_validator("source_image_asset_ids")
    @classmethod
    def validate_source_image_asset_ids(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 5:
            raise ValueError("最多只能为 5 个镜头选择 Final Media 图片资产")
        normalized: dict[str, str] = {}
        for shot_id, asset_id in value.items():
            shot_id = shot_id.strip()
            asset_id = asset_id.strip()
            if not shot_id or not asset_id or len(shot_id) > 120 or len(asset_id) > 36:
                raise ValueError("source_image_asset_ids 包含无效镜头或资产 ID")
            normalized[shot_id] = asset_id
        return normalized


class BestMediaVisualSelection(BaseModel):
    shot_id: str
    selected_type: Literal["VIDEO_SHOT", "IMAGE", "LEGACY_IMAGE"]
    asset_id: str | None = None
    source_job_id: str | None = None
    provider: str
    provider_hint: str | None = None
    source_type: str
    is_mock: bool
    priority_class: str
    selection_reason: str
    source_image_asset_id: str | None = None


class BestMediaAudioSelection(BaseModel):
    job_id: str
    provider: str
    source_type: str
    is_mock: bool
    source_script_job_id: str | None = None
    source_image_job_id: str | None = None
    speaker: str | None = None
    reason: str


class BestMediaPlan(BaseModel):
    mode: Literal["BEST_AVAILABLE", "IMAGE_ONLY", "VIDEO_PREFERRED"]
    status: Literal["READY", "AMBIGUOUS", "BLOCKED"]
    priority: list[str]
    shots: list[BestMediaVisualSelection]
    audio: BestMediaAudioSelection | None = None
    problems: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    available_image_shot_count: int = 0
    available_video_shot_count: int = 0
    freshness: Literal["NO_EXPORT", "CURRENT", "OUTDATED"] = "NO_EXPORT"
    freshness_reason: str = "当前还没有成片。"


class SmartMediaRenderRequest(MediaPolishOptions):
    preferred_audio_job_id: str | None = Field(default=None, min_length=1, max_length=36)
    composition_mode: Literal["BEST_AVAILABLE", "IMAGE_ONLY", "VIDEO_PREFERRED"] = (
        "BEST_AVAILABLE"
    )


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


class ExternalImagePromptBundle(BaseModel):
    shot_id: str
    shot_title: str
    adapter: Literal["external-natural-language-v1"]
    prompt: str
    source_fields: dict[str, Any]
    lineage: dict[str, Any]


class ImageAssetRead(BaseModel):
    asset_id: str
    project_id: str
    shot_id: str | None
    database_shot_id: str | None
    asset_type: Literal["KEYFRAME_IMAGE"]
    provider_id: str
    source_type: str
    generation_mode: str | None = None
    external_source_type: str | None = None
    provider_hint: str | None = None
    original_filename: str | None = None
    sha256: str
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    imported_at: str | None = None
    exported_prompt: dict[str, Any] | None = None
    image_url: str


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


class VideoProviderStatusRead(BaseModel):
    provider_id: Literal["mock-video", "cloud-wan-2.7"]
    display_name: str
    available: bool
    configured: bool
    model_id: str
    source_type: Literal["MOCK", "REAL_CLOUD_MODEL"]
    detail: str | None = None
    requires_gpu_handoff: bool = False
    runtime_state: Literal["READY_TO_USE", "CONFIG_ERROR"]
    requires_api_key: bool = False
    may_incur_cost: bool = False


class ProvidersRead(BaseModel):
    default_script_provider: Literal["mock", "llamacpp"]
    checked_at: datetime
    providers: list[ProviderStatusRead]
    default_image_provider: Literal["mock", "comfyui-animagine-xl-4"]
    image_providers: list[ImageProviderStatusRead]
    default_audio_provider: Literal["mock", "qwen3-tts-0.6b-customvoice"]
    audio_providers: list[AudioProviderStatusRead]
    default_video_provider: Literal["none", "mock-video", "cloud-wan-2.7"]
    video_providers: list[VideoProviderStatusRead]


class HealthRead(BaseModel):
    service: Literal["ok"]
    database: Literal["ok", "error"]
    ffmpeg_available: bool
    ffprobe_available: bool
    data_dir: str
    stage: str

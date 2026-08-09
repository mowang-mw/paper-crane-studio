"""项目创建、查询与异步生成入队。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import crud
from ..config import Settings
from ..database import get_session
from ..models import Export, JobStatus
from ..media.background_audio import background_job_snapshot
from ..media.ffmpeg import MediaToolError
from ..providers.registry import check_llamacpp
from ..schemas import (
    ExportRead,
    JobQueued,
    JobRead,
    GenerationRequest,
    MediaRerenderRequest,
    RealAudioRenderRequest,
    RealImageRenderRequest,
    VideoRenderRequest,
    ProjectCreate,
    ProjectDetail,
    ProjectRead,
    ShotRead,
)
from ..services.image_jobs import (
    REAL_IMAGE_JOB_TYPE,
    gpu_handoff_error_payload,
    gpu_handoff_status,
    script_from_source_job,
    write_script_snapshot,
)
from ..services.audio_jobs import (
    REAL_AUDIO_JOB_TYPE,
    REAL_AUDIO_PROVIDER_ID,
    audio_gpu_handoff_error_payload,
    audio_gpu_handoff_status,
    create_audio_source_snapshot,
)
from ..services.media_rerender import (
    MEDIA_RERENDER_JOB_TYPE,
    MEDIA_RERENDER_PROVIDER_ID,
    media_rerender_error_payload,
)
from ..services.video_jobs import VIDEO_JOB_TYPE, VIDEO_PROVIDER_ID
from .job_serialization import job_read_with_media_urls
from ..services.image_jobs import REAL_IMAGE_JOB_TYPE, REAL_IMAGE_PROVIDER_ID


router = APIRouter(prefix="/projects", tags=["projects"])
logger = logging.getLogger(__name__)

DEMO_TITLE = "纸鹤的夜航"
DEMO_STORY = (
    "停电的雨夜，少女阿澄在窗边折出一只纸鹤。纸鹤被微光唤醒，"
    "飞过屋顶、灯火和云层，在黎明前把远方的银杏叶带回窗边。"
)


def _export_read(item: Export) -> ExportRead:
    return ExportRead(
        id=item.id,
        project_id=item.project_id,
        job_id=item.job_id,
        file_path=item.file_path,
        manifest_path=item.manifest_path,
        duration_seconds=item.duration_seconds,
        sha256=item.sha256,
        created_at=item.created_at,
        video_url=f"/api/projects/{item.project_id}/exports/{item.id}/video",
        download_url=(
            f"/api/projects/{item.project_id}/exports/{item.id}/video?download=true"
        ),
        manifest_url=f"/api/projects/{item.project_id}/exports/{item.id}/manifest",
        poster_url=f"/api/projects/{item.project_id}/exports/{item.id}/poster",
    )


def _media_polish_snapshot(
    settings: Settings,
    project_id: str,
    payload: (
        GenerationRequest
        | RealImageRenderRequest
        | RealAudioRenderRequest
        | MediaRerenderRequest
    ),
) -> dict[str, object]:
    try:
        background_audio = background_job_snapshot(
            data_dir=Path(settings.data_dir),
            project_id=project_id,
            enabled=payload.background_audio_enabled,
            volume=payload.background_volume,
        )
    except MediaToolError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "motion_preset": payload.motion_preset,
        "background_audio": background_audio,
    }


def _rerender_source_error(code: str, summary: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=media_rerender_error_payload(code, summary),
    )


def _project_directory_for_delete(settings: Settings, project_id: str) -> Path:
    """解析并限制项目目录为 ``DATA_ROOT/projects`` 的直接子目录。"""

    data_root = Path(settings.data_dir).resolve()
    projects_root = (data_root / "projects").resolve()
    if projects_root.parent != data_root:
        raise ValueError("projects 根目录本身已越过 DATA_ROOT")
    raw_project_id = Path(project_id)
    if (
        not project_id
        or raw_project_id.is_absolute()
        or raw_project_id.name != project_id
        or project_id in {".", ".."}
    ):
        raise ValueError("项目 ID 不能包含绝对路径或目录跳转")
    candidate = (projects_root / raw_project_id).resolve()
    if (
        candidate in {data_root, projects_root}
        or candidate.parent != projects_root
        or candidate.name != project_id
    ):
        raise ValueError("项目目录不在受控 projects 根目录内")
    return candidate


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
) -> ProjectRead:
    project = crud.create_project(session, title=payload.title, story=payload.story)
    session.commit()
    return ProjectRead.model_validate(project)


@router.get("", response_model=list[ProjectRead])
def get_projects(session: Session = Depends(get_session)) -> list[ProjectRead]:
    return [ProjectRead.model_validate(item) for item in crud.list_projects(session)]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if crud.project_has_active_jobs(session, project.id):
        raise HTTPException(
            status_code=409,
            detail="当前项目仍有任务正在等待或生成，请等待任务结束后再删除。",
        )

    settings: Settings = request.app.state.settings
    try:
        project_directory = _project_directory_for_delete(settings, project.id)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="项目存储路径安全校验失败，已拒绝删除。",
        ) from exc

    try:
        crud.delete_project(session, project)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("删除项目数据库记录失败 project_id=%s", project_id)
        raise HTTPException(
            status_code=500,
            detail="项目数据库记录删除失败，请检查服务日志。",
        ) from exc

    try:
        if project_directory.exists():
            if not project_directory.is_dir():
                raise OSError(f"项目存储路径不是目录：{project_directory}")
            shutil.rmtree(project_directory)
    except OSError as exc:
        session.rollback()
        logger.exception("删除项目文件失败 project_id=%s", project_id)
        raise HTTPException(
            status_code=500,
            detail=f"项目文件删除失败，数据库记录已保留：{exc}",
        ) from exc

    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("提交项目删除事务失败 project_id=%s", project_id)
        raise HTTPException(
            status_code=500,
            detail="项目文件已清理，但数据库删除提交失败，请检查服务日志。",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/demo", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_demo_project(session: Session = Depends(get_session)) -> ProjectRead:
    project = crud.create_project(session, title=DEMO_TITLE, story=DEMO_STORY)
    session.commit()
    return ProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, session: Session = Depends(get_session)) -> ProjectDetail:
    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    latest = crud.latest_export(session, project.id)
    return ProjectDetail(
        project=ProjectRead.model_validate(project),
        shots=[ShotRead.model_validate(item) for item in crud.list_shots(session, project.id)],
        recent_jobs=[
            job_read_with_media_urls(session, item)
            for item in crud.recent_jobs(session, project.id)
        ],
        latest_export=_export_read(latest) if latest is not None else None,
    )


@router.post(
    "/{project_id}/generate",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_project(
    project_id: str,
    request: Request,
    payload: GenerationRequest | None = Body(default=None),
    session: Session = Depends(get_session),
) -> JobQueued:
    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    settings: Settings = request.app.state.settings
    payload = payload or GenerationRequest()
    provider_id = payload.script_provider or settings.script_provider
    desired_shot_count = payload.desired_shot_count
    if provider_id == "llamacpp":
        availability = check_llamacpp(settings)
        if not availability["available"]:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "PROVIDER_UNAVAILABLE",
                    "stage": "PROVIDER_UNAVAILABLE",
                    "summary": str(availability["detail"]),
                    "story_char_count": len(project.story.strip()),
                    "desired_shot_count": desired_shot_count,
                    "first_attempt_errors": [],
                    "repair_attempt_errors": [],
                    "suggestions": [
                        "检查 llama-server 可执行文件、GGUF 模型与 8081 端口后重新检查。",
                        "若只需离线保底，请显式选择 Mock Script Provider。",
                    ],
                    "provider_id": "llamacpp",
                    "model_id": settings.llama_model_id,
                    "raw_response_path": None,
                    "repair_response_path": None,
                },
            )
    job = crud.create_job(
        session,
        project=project,
        request_json={
            "project_id": project.id,
            "output": {"width": 1280, "height": 720, "fps": 24},
            "script_provider": provider_id,
            "desired_shot_count": desired_shot_count,
            "story_char_count": len(project.story.strip()),
            **_media_polish_snapshot(settings, project.id, payload),
        },
        provider_id=provider_id,
    )
    session.commit()
    return JobQueued(job_id=job.id)


@router.post(
    "/{project_id}/render-real-images",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def render_project_with_real_images(
    project_id: str,
    payload: RealImageRenderRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> JobQueued:
    """复用成功 Job 的严格 ScriptV1，创建不调用 Qwen 的真实图像任务。"""

    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if crud.project_has_active_jobs(session, project.id):
        raise HTTPException(
            status_code=409,
            detail="当前项目仍有任务正在等待或运行，请完成后再生成真实动漫画面。",
        )

    source_job = crud.get_job(session, payload.source_script_job_id)
    if (
        source_job is None
        or source_job.project_id != project.id
        or source_job.job_type != "GENERATE_SHORT_VIDEO"
        or source_job.status != JobStatus.SUCCEEDED
    ):
        raise HTTPException(
            status_code=409,
            detail="只能复用当前项目中已经成功的结构化剧本 Job。",
        )

    settings: Settings = request.app.state.settings
    parsed_llama = urlparse(settings.llama_server_base_url)
    handoff = gpu_handoff_status(
        llama_host=parsed_llama.hostname or "127.0.0.1",
        llama_port=parsed_llama.port or 8081,
    )
    if handoff["conflict"]:
        raise HTTPException(status_code=409, detail=gpu_handoff_error_payload())

    try:
        script, source_trace = script_from_source_job(
            settings,
            project=project,
            source_job=source_job,
        )
    except RuntimeError as exc:
        detail = getattr(exc, "generation_error", str(exc))
        raise HTTPException(status_code=409, detail=detail) from exc
    if project.script_json != script.model_dump(mode="json"):
        raise HTTPException(
            status_code=409,
            detail=(
                "所选来源 Job 已不是项目当前剧本；请使用当前剧本对应的成功 Job。"
            ),
        )

    source_result = dict(source_job.result_json or {})
    source_script_provider = str(
        source_result.get("script_provider") or source_job.provider_id
    )
    source_script_source_type = str(
        source_result.get("script_source_type") or "UNKNOWN"
    )
    base_seed = (
        payload.base_seed
        if payload.base_seed is not None
        else settings.image_base_seed
    )
    job = crud.create_job(
        session,
        project=project,
        provider_id=payload.image_provider,
        job_type=REAL_IMAGE_JOB_TYPE,
        request_json={},
    )
    try:
        snapshot_path, snapshot_sha256 = write_script_snapshot(
            settings,
            project_id=project.id,
            image_job_id=job.id,
            source_job_id=source_job.id,
            source_script_provider=source_script_provider,
            script=script,
            source_trace=source_trace,
        )
    except OSError as exc:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail="无法写入受控 ScriptV1 快照，真实图像任务未入队。",
        ) from exc

    job.request_json = {
        "project_id": project.id,
        "script_provider": "reused",
        "source_script_provider": source_script_provider,
        "source_script_source_type": source_script_source_type,
        "source_script_job_id": source_job.id,
        "reuse_script_from_job_id": source_job.id,
        "script_provider_calls_expected": 0,
        "script_snapshot_path": snapshot_path.resolve()
        .relative_to(Path(settings.data_dir).resolve())
        .as_posix(),
        "script_snapshot_sha256": snapshot_sha256,
        "script_snapshot_owner_job_id": job.id,
        "image_provider": payload.image_provider,
        "audio_provider": "mock",
        "base_seed": base_seed,
        "image_options": {
            "width": settings.image_width,
            "height": settings.image_height,
            "batch_size": 1,
            "steps": settings.image_steps,
            "cfg": settings.image_cfg,
            "sampler": settings.image_sampler,
            "scheduler": settings.image_scheduler,
            "denoise": 1.0,
            "lowvram": True,
            "startup_timeout_seconds": settings.comfyui_startup_timeout_seconds,
            "generation_timeout_seconds": settings.comfyui_image_timeout_seconds,
            "job_timeout_seconds": settings.comfyui_job_timeout_seconds,
            "http_timeout_seconds": settings.comfyui_http_timeout_seconds,
        },
        "output": {"width": 1280, "height": 720, "fps": 24},
        "story_char_count": len(project.story.strip()),
        "actual_shot_count": len(script.shots),
        **_media_polish_snapshot(settings, project.id, payload),
    }
    session.commit()
    return JobQueued(job_id=job.id)


@router.post(
    "/{project_id}/render-video",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def render_project_video(
    project_id: str,
    payload: VideoRenderRequest,
    session: Session = Depends(get_session),
) -> JobQueued:
    """Queue the optional mock video stage without changing final-media inputs."""

    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在。")
    if crud.project_has_active_jobs(session, project.id):
        raise HTTPException(status_code=409, detail="当前项目仍有任务正在运行。")
    source_job = crud.get_job(session, payload.source_image_job_id)
    if source_job is None or source_job.project_id != project.id:
        raise HTTPException(status_code=409, detail="来源图片 Job 不属于当前项目。")
    if source_job.status != JobStatus.SUCCEEDED:
        raise HTTPException(status_code=409, detail="来源图片 Job 尚未成功。")
    source_images = (source_job.result_json or {}).get("image_shots")
    if not isinstance(source_images, list) or not source_images:
        raise HTTPException(status_code=409, detail="来源 Job 没有可复用的关键帧资产。")

    job = crud.create_job(
        session,
        project=project,
        provider_id=payload.video_provider,
        job_type=VIDEO_JOB_TYPE,
        request_json={
            "project_id": project.id,
            "parent_job_id": source_job.id,
            "source_image_job_id": source_job.id,
            "video_provider": payload.video_provider,
            "video_options": {
                "width": 1280,
                "height": 720,
                "fps": 24,
                "duration_seconds": payload.duration_seconds,
                "motion_preset": payload.motion_preset,
            },
            "final_media_consumes_video": False,
            "fallback_media_path": "KEYFRAME_FFMPEG_MOTION",
        },
    )
    if job.provider_id != VIDEO_PROVIDER_ID:
        raise HTTPException(status_code=422, detail="当前仅提供明确标记的 MockVideoProvider。")
    session.commit()
    return JobQueued(job_id=job.id)


@router.post(
    "/{project_id}/render-real-audio",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def render_project_with_real_audio(
    project_id: str,
    payload: RealAudioRenderRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> JobQueued:
    """复用成功 M4-B ScriptV1 与真实 PNG，创建不调用上游模型的旁白 Job。"""

    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if crud.project_has_active_jobs(session, project.id):
        raise HTTPException(
            status_code=409,
            detail="当前项目仍有任务正在等待或运行，请完成后再生成真实 AI 旁白。",
        )

    source_image_job = crud.get_job(session, payload.source_image_job_id)
    if (
        source_image_job is None
        or source_image_job.project_id != project.id
        or source_image_job.job_type != REAL_IMAGE_JOB_TYPE
        or source_image_job.provider_id != REAL_IMAGE_PROVIDER_ID
        or source_image_job.status != JobStatus.SUCCEEDED
    ):
        raise HTTPException(
            status_code=409,
            detail="只能复用当前项目中已经成功的 M4-B 真实图像 Job。",
        )
    source_result = dict(source_image_job.result_json or {})
    if (
        source_result.get("image_provider") != REAL_IMAGE_PROVIDER_ID
        or source_result.get("mock_image_fallback") is not False
    ):
        raise HTTPException(
            status_code=409,
            detail="来源 Job 没有可证明为真实模型生成的完整关键帧。",
        )
    source_images = source_result.get("image_shots")
    if not isinstance(source_images, list) or not 3 <= len(source_images) <= 5:
        raise HTTPException(
            status_code=409,
            detail="来源真实图像 Job 的逐镜头关键帧追溯不完整。",
        )

    settings: Settings = request.app.state.settings
    handoff = audio_gpu_handoff_status(settings)
    if handoff["conflict"]:
        raise HTTPException(
            status_code=409,
            detail=audio_gpu_handoff_error_payload(handoff),
        )
    if not Path(settings.qwen_tts_python).is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TTS_ENV_NOT_FOUND",
                "stage": "TTS_PREFLIGHT",
                "summary": "独立 Qwen3-TTS Python 环境不存在。",
                "retryable": False,
                "provider_id": REAL_AUDIO_PROVIDER_ID,
            },
        )
    if not Path(settings.qwen_tts_model_path).joinpath("model.safetensors").is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TTS_MODEL_NOT_FOUND",
                "stage": "TTS_PREFLIGHT",
                "summary": "固定 Qwen3-TTS 模型文件不存在。",
                "retryable": False,
                "provider_id": REAL_AUDIO_PROVIDER_ID,
            },
        )

    try:
        script, source_trace = script_from_source_job(
            settings,
            project=project,
            source_job=source_image_job,
        )
    except RuntimeError as exc:
        detail = getattr(exc, "generation_error", str(exc))
        raise HTTPException(status_code=409, detail=detail) from exc
    script_json = script.model_dump(mode="json")
    if project.script_json != script_json:
        raise HTTPException(
            status_code=409,
            detail="来源真实图像 Job 已不是项目当前 ScriptV1，拒绝生成旁白。",
        )

    source_script_job_id = str(
        source_result.get("source_script_job_id")
        or (source_image_job.request_json or {}).get("source_script_job_id")
        or ""
    )
    source_script_provider = str(
        source_result.get("source_script_provider") or "unknown"
    )
    if not source_script_job_id:
        raise HTTPException(
            status_code=409,
            detail="来源真实图像 Job 缺少原始 Script Job 追溯。",
        )

    job = crud.create_job(
        session,
        project=project,
        provider_id=payload.audio_provider,
        job_type=REAL_AUDIO_JOB_TYPE,
        request_json={},
    )
    try:
        snapshot_path, snapshot_sha256 = create_audio_source_snapshot(
            settings,
            project_id=project.id,
            audio_job_id=job.id,
            source_script_job_id=source_script_job_id,
            source_image_job_id=source_image_job.id,
            source_script_provider=source_script_provider,
            source_image_provider=REAL_IMAGE_PROVIDER_ID,
            script=script,
            source_images=source_images,
            source_trace=source_trace,
        )
    except (OSError, TypeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"无法写入受控 M5-B 来源快照：{exc}",
        ) from exc

    relative_snapshot = snapshot_path.resolve().relative_to(
        Path(settings.data_dir).resolve()
    ).as_posix()
    source_duration = sum(float(shot.duration_seconds) for shot in script.shots)
    job.request_json = {
        "project_id": project.id,
        "parent_job_id": source_image_job.id,
        "source_script_job_id": source_script_job_id,
        "source_image_job_id": source_image_job.id,
        "script_provider": "reused",
        "source_script_provider": source_script_provider,
        "script_provider_calls_expected": 0,
        "image_provider": "reused",
        "source_image_provider": REAL_IMAGE_PROVIDER_ID,
        "image_provider_calls_expected": 0,
        "source_image_model_id": source_result.get("image_model_id"),
        "audio_provider": payload.audio_provider,
        "speaker": payload.speaker,
        "language": payload.language,
        "audio_source_snapshot_path": relative_snapshot,
        "audio_source_snapshot_sha256": snapshot_sha256,
        "audio_source_snapshot_owner_job_id": job.id,
        "audio_options": {
            "speaker": payload.speaker,
            "language": payload.language,
            "base_seed": settings.qwen_tts_seed,
            "model_load_timeout_seconds": (
                settings.qwen_tts_model_load_timeout_seconds
            ),
            "generation_timeout_seconds": settings.qwen_tts_shot_timeout_seconds,
            "job_timeout_seconds": settings.qwen_tts_job_timeout_seconds,
        },
        "timing_options": {
            "lead_in_seconds": settings.audio_lead_in_seconds,
            "lead_out_seconds": settings.audio_lead_out_seconds,
            "max_total_duration_seconds": settings.audio_rendered_max_seconds,
            "fps": 24,
        },
        "output": {"width": 1280, "height": 720, "fps": 24},
        "story_char_count": len(project.story.strip()),
        "actual_shot_count": len(script.shots),
        "source_planned_duration_seconds": source_duration,
        **_media_polish_snapshot(settings, project.id, payload),
    }
    session.commit()
    return JobQueued(job_id=job.id)


@router.post(
    "/{project_id}/media-rerender",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def rerender_project_media_only(
    project_id: str,
    payload: MediaRerenderRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> JobQueued:
    """Queue an FFmpeg-only export using validated real PNG and WAV assets."""

    project = crud.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if crud.project_has_active_jobs(session, project.id):
        raise HTTPException(status_code=409, detail="当前项目仍有任务正在运行。")

    source_audio_job = crud.get_job(session, payload.source_audio_job_id)
    if source_audio_job is None:
        raise _rerender_source_error(
            "SOURCE_AUDIO_JOB_NOT_FOUND", "来源真实旁白 Job 不存在。"
        )
    if source_audio_job.project_id != project.id:
        raise _rerender_source_error(
            "SOURCE_JOB_PROJECT_MISMATCH", "来源旁白 Job 不属于当前项目。"
        )
    if (
        source_audio_job.job_type != REAL_AUDIO_JOB_TYPE
        or source_audio_job.provider_id != REAL_AUDIO_PROVIDER_ID
        or source_audio_job.status != JobStatus.SUCCEEDED
    ):
        raise _rerender_source_error(
            "SOURCE_AUDIO_JOB_NOT_FOUND", "来源 Job 不是成功的真实 Qwen3-TTS 旁白 Job。"
        )
    audio_result = dict(source_audio_job.result_json or {})
    if audio_result.get("mock_audio_fallback") is not False:
        raise _rerender_source_error(
            "SOURCE_AUDIO_MISSING", "来源旁白 Job 无法证明使用了完整真实 WAV。"
        )

    source_script_job_id = str(
        audio_result.get("source_script_job_id")
        or (source_audio_job.request_json or {}).get("source_script_job_id")
        or ""
    )
    source_image_job_id = str(
        audio_result.get("source_image_job_id")
        or (source_audio_job.request_json or {}).get("source_image_job_id")
        or ""
    )
    source_script_job = crud.get_job(session, source_script_job_id)
    source_image_job = crud.get_job(session, source_image_job_id)
    if source_script_job is None:
        raise _rerender_source_error(
            "SOURCE_SCRIPT_NOT_FOUND", "来源 ScriptV1 Job 不存在。"
        )
    if source_image_job is None:
        raise _rerender_source_error(
            "SOURCE_IMAGE_JOB_NOT_FOUND", "来源真实图片 Job 不存在。"
        )
    if (
        source_script_job.project_id != project.id
        or source_image_job.project_id != project.id
    ):
        raise _rerender_source_error(
            "SOURCE_JOB_PROJECT_MISMATCH", "剧本、图片和旁白来源必须属于同一个项目。"
        )
    if source_script_job.status != JobStatus.SUCCEEDED:
        raise _rerender_source_error(
            "SOURCE_SCRIPT_NOT_FOUND", "来源 ScriptV1 Job 尚未成功。"
        )
    if (
        source_image_job.job_type != REAL_IMAGE_JOB_TYPE
        or source_image_job.provider_id != REAL_IMAGE_PROVIDER_ID
        or source_image_job.status != JobStatus.SUCCEEDED
        or (source_image_job.result_json or {}).get("mock_image_fallback") is not False
    ):
        raise _rerender_source_error(
            "SOURCE_IMAGE_JOB_NOT_FOUND", "来源 Job 不是成功的真实 Animagine 图片 Job。"
        )

    settings: Settings = request.app.state.settings
    media_snapshot = _media_polish_snapshot(settings, project.id, payload)
    background = media_snapshot.get("background_audio")
    job = crud.create_job(
        session,
        project=project,
        provider_id=MEDIA_RERENDER_PROVIDER_ID,
        job_type=MEDIA_RERENDER_JOB_TYPE,
        request_json={
            "project_id": project.id,
            "parent_job_id": source_audio_job.id,
            "source_script_job_id": source_script_job.id,
            "source_image_job_id": source_image_job.id,
            "source_audio_job_id": source_audio_job.id,
            "script_provider": "reused",
            "image_provider": "reused",
            "audio_provider": "reused",
            "source_script_provider": (
                audio_result.get("source_script_provider") or source_script_job.provider_id
            ),
            "source_image_provider": REAL_IMAGE_PROVIDER_ID,
            "source_audio_provider": REAL_AUDIO_PROVIDER_ID,
            "script_provider_calls_expected": 0,
            "image_provider_calls_expected": 0,
            "audio_provider_calls_expected": 0,
            "media_only": True,
            "background_audio_id": (
                background.get("asset_id")
                if isinstance(background, dict) and background.get("enabled") is True
                else None
            ),
            "output": {"width": 1280, "height": 720, "fps": 24},
            "actual_shot_count": len(project.script_json.get("shots", []))
            if isinstance(project.script_json, dict)
            else 0,
            **media_snapshot,
        },
    )
    session.commit()
    return JobQueued(job_id=job.id)

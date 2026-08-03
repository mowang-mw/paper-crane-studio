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
from ..providers.registry import check_llamacpp
from ..schemas import (
    ExportRead,
    JobQueued,
    JobRead,
    GenerationRequest,
    RealImageRenderRequest,
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
            JobRead.model_validate(item) for item in crud.recent_jobs(session, project.id)
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
    provider_id = payload.script_provider if payload and payload.script_provider else settings.script_provider
    desired_shot_count = payload.desired_shot_count if payload is not None else 4
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
                        "在项目根目录运行 .\\scripts\\run_llm_server.ps1 后重新检查。",
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
    }
    session.commit()
    return JobQueued(job_id=job.id)

"""M2 同步 CRUD；事务边界由 API 或 Worker 显式控制。"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Asset, Export, GenerationJob, JobStatus, Project, Shot, utc_now


def create_project(session: Session, *, title: str, story: str) -> Project:
    project = Project(title=title.strip(), story=story.strip(), status="DRAFT")
    session.add(project)
    session.flush()
    return project


def get_project(session: Session, project_id: str) -> Project | None:
    return session.get(Project, project_id)


def project_has_active_jobs(session: Session, project_id: str) -> bool:
    return (
        session.scalars(
            select(GenerationJob.id)
            .where(
                GenerationJob.project_id == project_id,
                GenerationJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .limit(1)
        ).first()
        is not None
    )


def delete_project(session: Session, project: Project) -> None:
    session.delete(project)
    session.flush()


def list_projects(session: Session) -> list[Project]:
    return list(
        session.scalars(select(Project).order_by(Project.created_at.desc())).all()
    )


def list_shots(session: Session, project_id: str) -> list[Shot]:
    return list(
        session.scalars(
            select(Shot)
            .where(Shot.project_id == project_id)
            .order_by(Shot.shot_index.asc())
        ).all()
    )


def replace_shots(
    session: Session,
    *,
    project: Project,
    script_json: dict[str, Any],
    shots: Iterable[dict[str, Any]],
) -> list[Shot]:
    session.execute(delete(Shot).where(Shot.project_id == project.id))
    created: list[Shot] = []
    for payload in shots:
        shot = Shot(project_id=project.id, **payload)
        session.add(shot)
        created.append(shot)
    project.script_json = script_json
    project.updated_at = utc_now()
    session.flush()
    return created


def create_job(
    session: Session,
    *,
    project: Project,
    request_json: dict[str, Any] | None = None,
    provider_id: str = "mock",
) -> GenerationJob:
    job = GenerationJob(
        project_id=project.id,
        job_type="GENERATE_SHORT_VIDEO",
        status=JobStatus.QUEUED,
        progress=0,
        provider_id=provider_id,
        request_json=request_json or {},
    )
    project.status = "QUEUED"
    project.updated_at = utc_now()
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> GenerationJob | None:
    return session.get(GenerationJob, job_id)


def recent_jobs(session: Session, project_id: str, *, limit: int = 10) -> list[GenerationJob]:
    return list(
        session.scalars(
            select(GenerationJob)
            .where(GenerationJob.project_id == project_id)
            .order_by(GenerationJob.created_at.desc())
            .limit(limit)
        ).all()
    )


def claim_next_queued_job(session: Session) -> GenerationJob | None:
    """领取最早 QUEUED 任务；M2 明确只支持一个 Worker。"""

    job = session.scalars(
        select(GenerationJob)
        .where(GenerationJob.status == JobStatus.QUEUED)
        .order_by(GenerationJob.created_at.asc())
        .limit(1)
    ).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.progress = 1
    job.started_at = utc_now()
    job.error_message = None
    project = session.get(Project, job.project_id)
    if project is not None:
        project.status = "GENERATING"
        project.updated_at = utc_now()
    session.flush()
    return job


def set_job_progress(session: Session, job: GenerationJob, progress: int) -> None:
    if job.status != JobStatus.RUNNING:
        raise ValueError("只能更新 RUNNING 任务的进度")
    job.progress = max(1, min(99, int(progress)))
    session.flush()


def mark_job_succeeded(
    session: Session,
    *,
    job: GenerationJob,
    result_json: dict[str, Any],
) -> None:
    if job.status != JobStatus.RUNNING:
        raise ValueError("只有 RUNNING 任务可以标记为 SUCCEEDED")
    job.status = JobStatus.SUCCEEDED
    job.progress = 100
    job.result_json = result_json
    job.error_message = None
    job.finished_at = utc_now()
    project = session.get(Project, job.project_id)
    if project is not None:
        project.status = "EXPORTED"
        project.updated_at = utc_now()
    session.flush()


def mark_job_failed(
    session: Session,
    *,
    job: GenerationJob,
    error_message: str,
) -> None:
    if job.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
        raise ValueError("只有 QUEUED 或 RUNNING 任务可以标记为 FAILED")
    job.status = JobStatus.FAILED
    job.error_message = error_message[:8_000]
    job.finished_at = utc_now()
    project = session.get(Project, job.project_id)
    if project is not None:
        project.status = "FAILED"
        project.updated_at = utc_now()
    session.flush()


def retry_failed_job(session: Session, failed_job: GenerationJob) -> GenerationJob:
    if failed_job.status != JobStatus.FAILED:
        raise ValueError("仅 FAILED 任务允许手动重试")
    project = session.get(Project, failed_job.project_id)
    if project is None:
        raise ValueError("原任务所属项目不存在")
    request_json = dict(failed_job.request_json or {})
    request_json["retry_of_job_id"] = failed_job.id
    return create_job(
        session,
        project=project,
        request_json=request_json,
        provider_id=failed_job.provider_id,
    )


def create_asset(
    session: Session,
    *,
    project_id: str,
    asset_type: str,
    file_path: str,
    sha256: str,
    provider_id: str = "mock",
    source_type: str = "DETERMINISTIC_FALLBACK",
    shot_id: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> Asset:
    asset = Asset(
        project_id=project_id,
        shot_id=shot_id,
        asset_type=asset_type,
        provider_id=provider_id,
        source_type=source_type,
        file_path=file_path,
        metadata_json=metadata_json or {},
        sha256=sha256,
    )
    session.add(asset)
    session.flush()
    return asset


def create_export(
    session: Session,
    *,
    project_id: str,
    job_id: str,
    file_path: str,
    manifest_path: str,
    duration_seconds: float,
    sha256: str,
) -> Export:
    export = Export(
        project_id=project_id,
        job_id=job_id,
        file_path=file_path,
        manifest_path=manifest_path,
        duration_seconds=duration_seconds,
        sha256=sha256,
    )
    session.add(export)
    session.flush()
    return export


def get_export(session: Session, export_id: str) -> Export | None:
    return session.get(Export, export_id)


def latest_export(session: Session, project_id: str) -> Export | None:
    return session.scalars(
        select(Export)
        .where(Export.project_id == project_id)
        .order_by(Export.created_at.desc())
        .limit(1)
    ).first()

"""任务轮询与显式手动重试。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import crud
from ..database import get_session
from ..schemas import JobQueued, JobRead


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, session: Session = Depends(get_session)) -> JobRead:
    job = crud.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JobRead.model_validate(job)


@router.post(
    "/{job_id}/retry",
    response_model=JobQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(job_id: str, session: Session = Depends(get_session)) -> JobQueued:
    job = crud.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        retried = crud.retry_failed_job(session, job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return JobQueued(job_id=retried.id)


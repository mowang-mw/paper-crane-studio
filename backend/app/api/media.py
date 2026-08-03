"""仅按数据库中的 Export 记录安全提供项目媒体。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import crud
from ..database import get_session


router = APIRouter(prefix="/projects", tags=["media"])


def _safe_export_path(request: Request, project_id: str, stored_path: str) -> Path:
    settings = request.app.state.settings
    data_root = Path(settings.data_dir).resolve()
    project_root = settings.project_dir(project_id).resolve()
    relative = Path(stored_path)
    if relative.is_absolute():
        raise HTTPException(status_code=404, detail="媒体路径无效")
    candidate = (data_root / relative).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="媒体路径越界") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    return candidate


def _find_export(session: Session, project_id: str, export_id: str):
    export = crud.get_export(session, export_id)
    if export is None or export.project_id != project_id:
        raise HTTPException(status_code=404, detail="导出不存在")
    return export


def _find_image_asset(session: Session, project_id: str, asset_id: str):
    asset = crud.get_asset(session, asset_id)
    if (
        asset is None
        or asset.project_id != project_id
        or asset.asset_type != "KEYFRAME_IMAGE"
    ):
        raise HTTPException(status_code=404, detail="关键帧不存在")
    return asset


@router.get("/{project_id}/exports/{export_id}/video")
def get_export_video(
    project_id: str,
    export_id: str,
    request: Request,
    download: bool = Query(False),
    session: Session = Depends(get_session),
) -> FileResponse:
    export = _find_export(session, project_id, export_id)
    path = _safe_export_path(request, project_id, export.file_path)
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name if download else None,
    )


@router.get("/{project_id}/exports/{export_id}/manifest")
def get_export_manifest(
    project_id: str,
    export_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    export = _find_export(session, project_id, export_id)
    path = _safe_export_path(request, project_id, export.manifest_path)
    return FileResponse(path, media_type="application/json; charset=utf-8")


@router.get("/{project_id}/assets/{asset_id}/content")
def get_image_asset(
    project_id: str,
    asset_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    asset = _find_image_asset(session, project_id, asset_id)
    path = _safe_export_path(request, project_id, asset.file_path)
    if path.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="关键帧格式无效")
    return FileResponse(path, media_type="image/png")

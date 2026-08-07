"""仅按数据库中的 Export 记录安全提供项目媒体。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from .. import crud
from ..database import get_session
from ..media.background_audio import (
    MAX_BACKGROUND_AUDIO_BYTES,
    RIGHTS_NOTICE,
    background_audio_directory,
    background_audio_metadata_path,
    inspect_background_audio,
    load_background_metadata,
    validate_background_filename,
    write_background_metadata,
)
from ..media.ffmpeg import MediaToolError, sha256_file
from ..schemas import BackgroundAudioRead


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


def _safe_manifest_media_path(request: Request, project_id: str, stored_path: str) -> Path:
    settings = request.app.state.settings
    project_root = settings.project_dir(project_id).resolve()
    relative = Path(stored_path)
    if relative.is_absolute():
        raise HTTPException(status_code=404, detail="媒体路径无效")
    candidates = (
        (Path(settings.root_dir).resolve() / relative).resolve(),
        (Path(settings.data_dir).resolve() / relative).resolve(),
    )
    for candidate in candidates:
        try:
            candidate.relative_to(project_root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=404, detail="媒体文件不存在或路径越界")


def _find_export(session: Session, project_id: str, export_id: str):
    export = crud.get_export(session, export_id)
    if export is None or export.project_id != project_id:
        raise HTTPException(status_code=404, detail="导出不存在")
    return export


def _find_public_media_asset(session: Session, project_id: str, asset_id: str):
    asset = crud.get_asset(session, asset_id)
    if (
        asset is None
        or asset.project_id != project_id
        or asset.asset_type not in {"KEYFRAME_IMAGE", "NARRATION_AUDIO"}
    ):
        raise HTTPException(status_code=404, detail="媒体资产不存在")
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


@router.get("/{project_id}/exports/{export_id}/poster")
def get_export_poster(
    project_id: str,
    export_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    export = _find_export(session, project_id, export_id)
    manifest_path = _safe_export_path(request, project_id, export.manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stored_path = manifest["output"]["poster_path"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="成片封面不存在") from exc
    if not isinstance(stored_path, str):
        raise HTTPException(status_code=404, detail="成片封面路径无效")
    path = _safe_manifest_media_path(request, project_id, stored_path)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=404, detail="成片封面格式无效")
    return FileResponse(path, media_type="image/jpeg" if path.suffix.lower() != ".png" else "image/png")


@router.get("/{project_id}/background-audio", response_model=BackgroundAudioRead | None)
def get_background_audio(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> BackgroundAudioRead | None:
    if crud.get_project(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        payload = load_background_metadata(request.app.state.settings.data_dir, project_id)
    except MediaToolError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return BackgroundAudioRead.model_validate(payload) if payload is not None else None


@router.post("/{project_id}/background-audio", response_model=BackgroundAudioRead)
async def upload_background_audio(
    project_id: str,
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    session: Session = Depends(get_session),
) -> BackgroundAudioRead:
    if crud.get_project(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    mime_type = request.headers.get("content-type", "application/octet-stream")
    try:
        extension = validate_background_filename(filename, mime_type)
    except MediaToolError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    settings = request.app.state.settings
    directory = background_audio_directory(settings.data_dir, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".upload-{uuid4().hex}.tmp"
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BACKGROUND_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="背景音文件不能超过 20MB")
                handle.write(chunk)
        if size <= 0:
            raise HTTPException(status_code=422, detail="背景音文件为空")
        try:
            probe = inspect_background_audio(temporary)
        except MediaToolError as exc:
            raise HTTPException(status_code=422, detail=f"背景音无法解码：{exc}") from exc

        digest = sha256_file(temporary)
        destination = directory / f"user-audio-{digest[:16]}{extension}"
        previous = None
        metadata_path = background_audio_metadata_path(settings.data_dir, project_id)
        if metadata_path.is_file():
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                previous = loaded if isinstance(loaded, dict) else None
            except (OSError, json.JSONDecodeError):
                previous = None
        os.replace(temporary, destination)
        data_root = Path(settings.data_dir).resolve()
        payload = {
            "asset_id": digest[:24],
            "original_filename": filename,
            "mime_type": mime_type.split(";", 1)[0].strip().lower(),
            "format": extension.removeprefix("."),
            "duration_seconds": probe["duration_seconds"],
            "size_bytes": size,
            "sha256": digest,
            "storage_path": destination.resolve().relative_to(data_root).as_posix(),
            "source_type": "USER_UPLOAD",
            "codec_name": probe["codec_name"],
            "sample_rate": probe["sample_rate"],
            "channels": probe["channels"],
            "rights_notice": RIGHTS_NOTICE,
        }
        write_background_metadata(metadata_path, payload)
        if previous and previous.get("storage_path") != payload["storage_path"]:
            old_path = (data_root / str(previous.get("storage_path", ""))).resolve()
            try:
                old_path.relative_to(directory.resolve())
            except ValueError:
                pass
            else:
                old_path.unlink(missing_ok=True)
        return BackgroundAudioRead.model_validate(payload)
    finally:
        temporary.unlink(missing_ok=True)


@router.delete(
    "/{project_id}/background-audio",
    status_code=204,
    response_class=Response,
)
def delete_background_audio(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    if crud.get_project(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    settings = request.app.state.settings
    try:
        payload = load_background_metadata(settings.data_dir, project_id)
    except MediaToolError:
        payload = None
    metadata_path = background_audio_metadata_path(settings.data_dir, project_id)
    if payload is not None:
        data_root = Path(settings.data_dir).resolve()
        candidate = (data_root / payload["storage_path"]).resolve()
        directory = background_audio_directory(settings.data_dir, project_id).resolve()
        try:
            candidate.relative_to(directory)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="背景音存储路径越界") from exc
        candidate.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    return Response(status_code=204)


@router.get("/{project_id}/background-audio/content")
def get_background_audio_content(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    if crud.get_project(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        payload = load_background_metadata(request.app.state.settings.data_dir, project_id)
    except MediaToolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="尚未上传背景音")
    path = (
        Path(request.app.state.settings.data_dir).resolve() / payload["storage_path"]
    ).resolve()
    return FileResponse(path, media_type=payload["mime_type"], filename=payload["original_filename"])


@router.get("/{project_id}/assets/{asset_id}/content")
def get_image_asset(
    project_id: str,
    asset_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    asset = _find_public_media_asset(session, project_id, asset_id)
    path = _safe_export_path(request, project_id, asset.file_path)
    expected = {
        "KEYFRAME_IMAGE": (".png", "image/png"),
        "NARRATION_AUDIO": (".wav", "audio/wav"),
    }[asset.asset_type]
    if path.suffix.lower() != expected[0]:
        raise HTTPException(status_code=404, detail="媒体资产格式无效")
    return FileResponse(path, media_type=expected[1])

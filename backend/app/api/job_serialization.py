"""API-only enrichment for safe public URLs stored outside the Job snapshot."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import crud
from ..models import GenerationJob
from ..schemas import JobRead


def job_read_with_media_urls(session: Session, job: GenerationJob) -> JobRead:
    result = dict(job.result_json or {})
    raw_audio_shots = result.get("audio_shots")
    if isinstance(raw_audio_shots, list):
        enriched: list[Any] = []
        for raw_item in raw_audio_shots:
            if not isinstance(raw_item, dict):
                enriched.append(raw_item)
                continue
            item = dict(raw_item)
            asset_id = item.get("audio_asset_id")
            asset = crud.get_asset(session, asset_id) if isinstance(asset_id, str) else None
            if (
                asset is not None
                and asset.project_id == job.project_id
                and asset.asset_type == "NARRATION_AUDIO"
            ):
                item["audio_url"] = (
                    f"/api/projects/{job.project_id}/assets/{asset.id}/content"
                )
                item.pop("audio_url_error", None)
            else:
                item.pop("audio_url", None)
                item["audio_url_error"] = {
                    "code": "AUDIO_ASSET_URL_MISSING",
                    "summary": "旁白文件缺少可公开访问的媒体资产记录。",
                }
            enriched.append(item)
        result["audio_shots"] = enriched
    raw_video_shots = result.get("video_shots")
    if isinstance(raw_video_shots, list):
        enriched_videos: list[Any] = []
        for raw_item in raw_video_shots:
            if not isinstance(raw_item, dict):
                enriched_videos.append(raw_item)
                continue
            item = dict(raw_item)
            asset_id = item.get("video_asset_id")
            asset = crud.get_asset(session, asset_id) if isinstance(asset_id, str) else None
            if (
                asset is not None
                and asset.project_id == job.project_id
                and asset.asset_type == "VIDEO_SHOT"
            ):
                item["video_url"] = (
                    f"/api/projects/{job.project_id}/assets/{asset.id}/content"
                )
                item.pop("video_url_error", None)
            else:
                item.pop("video_url", None)
                item["video_url_error"] = {
                    "code": "VIDEO_ASSET_URL_MISSING",
                    "summary": "动态视频缺少可公开访问的媒体资产记录。",
                }
            enriched_videos.append(item)
        result["video_shots"] = enriched_videos
    return JobRead.model_validate(job).model_copy(update={"result_json": result or None})

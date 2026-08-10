"""Persist the user's current visual selections in existing Shot metadata."""

from __future__ import annotations

from typing import Any, Iterable

from ..models import Shot as DatabaseShot
from ..script_schema import ScriptV1
from .external_images import database_shot_for_script_shot


SELECTED_IMAGE_ASSET_KEY = "selected_image_asset_id"
SELECTED_VIDEO_JOB_KEY = "selected_video_job_id"


def read_visual_selection(
    *, script: ScriptV1, database_shots: Iterable[DatabaseShot]
) -> dict[str, Any]:
    shots = list(database_shots)
    image_asset_ids: dict[str, str] = {}
    video_job_ids: set[str] = set()
    for script_shot in script.shots:
        database_shot = database_shot_for_script_shot(shots, script_shot)
        parameters = (
            database_shot.parameters_json
            if isinstance(database_shot.parameters_json, dict)
            else {}
        )
        image_asset_id = parameters.get(SELECTED_IMAGE_ASSET_KEY)
        if isinstance(image_asset_id, str) and image_asset_id:
            image_asset_ids[script_shot.id] = image_asset_id
        video_job_id = parameters.get(SELECTED_VIDEO_JOB_KEY)
        if isinstance(video_job_id, str) and video_job_id:
            video_job_ids.add(video_job_id)
    return {
        "source_image_asset_ids": image_asset_ids,
        "source_video_job_id": next(iter(video_job_ids)) if len(video_job_ids) == 1 else None,
    }


def write_visual_selection(
    *,
    script: ScriptV1,
    database_shots: Iterable[DatabaseShot],
    source_image_asset_ids: dict[str, str],
    source_video_job_id: str | None,
) -> dict[str, Any]:
    shots = list(database_shots)
    for script_shot in script.shots:
        database_shot = database_shot_for_script_shot(shots, script_shot)
        parameters = dict(
            database_shot.parameters_json
            if isinstance(database_shot.parameters_json, dict)
            else {}
        )
        image_asset_id = source_image_asset_ids.get(script_shot.id)
        if image_asset_id:
            parameters[SELECTED_IMAGE_ASSET_KEY] = image_asset_id
        else:
            parameters.pop(SELECTED_IMAGE_ASSET_KEY, None)
        if source_video_job_id:
            parameters[SELECTED_VIDEO_JOB_KEY] = source_video_job_id
        else:
            parameters.pop(SELECTED_VIDEO_JOB_KEY, None)
        database_shot.parameters_json = parameters
    return {
        "source_image_asset_ids": dict(source_image_asset_ids),
        "source_video_job_id": source_video_job_id,
    }


__all__ = ["read_visual_selection", "write_visual_selection"]

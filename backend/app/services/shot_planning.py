"""Production-layer Shot corrections without mutating the original ScriptV1."""

from __future__ import annotations

from typing import Any

from ..models import Shot as DatabaseShot
from ..script_schema import Shot


PRODUCTION_OVERRIDE_KEY = "production_override"
OVERRIDE_FIELDS = ("keyframe_description", "motion_description")


def read_production_override(
    parameters_json: dict[str, Any] | None,
) -> dict[str, str]:
    parameters = parameters_json if isinstance(parameters_json, dict) else {}
    raw = parameters.get(PRODUCTION_OVERRIDE_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        field: value.strip()
        for field in OVERRIDE_FIELDS
        if isinstance((value := raw.get(field)), str) and value.strip()
    }


def effective_shot_plan(script_shot: Shot, database_shot: DatabaseShot) -> dict[str, Any]:
    override = read_production_override(database_shot.parameters_json)
    return {
        "shot_id": script_shot.id,
        "title": script_shot.title,
        "keyframe_description": override.get(
            "keyframe_description", script_shot.visual_description
        ),
        "motion_description": override.get(
            "motion_description", script_shot.camera
        ),
        "narration": script_shot.narration,
        "planning_source": "LLM_WITH_HUMAN_OVERRIDE" if override else "LLM",
        "override": override,
        "original": {
            "visual_description": script_shot.visual_description,
            "camera": script_shot.camera,
            "narration": script_shot.narration,
        },
    }


def update_production_override(
    database_shot: DatabaseShot,
    *,
    keyframe_description: str | None,
    motion_description: str | None,
) -> None:
    parameters = (
        dict(database_shot.parameters_json)
        if isinstance(database_shot.parameters_json, dict)
        else {}
    )
    override = {
        field: value.strip()
        for field, value in {
            "keyframe_description": keyframe_description,
            "motion_description": motion_description,
        }.items()
        if isinstance(value, str) and value.strip()
    }
    if override:
        parameters[PRODUCTION_OVERRIDE_KEY] = override
    else:
        parameters.pop(PRODUCTION_OVERRIDE_KEY, None)
    database_shot.parameters_json = parameters


__all__ = [
    "PRODUCTION_OVERRIDE_KEY",
    "effective_shot_plan",
    "read_production_override",
    "update_production_override",
]

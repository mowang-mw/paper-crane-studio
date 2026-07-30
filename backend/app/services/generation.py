"""Provider 编排与数据库镜头落库，不包含 HTTP 或 FFmpeg 命令。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .. import crud
from ..models import Project
from ..providers.base import AudioProvider, ImageProvider, ScriptProvider, ScriptShot


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    media_shots: tuple[dict[str, Any], ...]
    planned_duration_seconds: float


class GenerationService:
    def __init__(
        self,
        *,
        script_provider: ScriptProvider,
        image_provider: ImageProvider,
        audio_provider: AudioProvider,
    ) -> None:
        self.script_provider = script_provider
        self.image_provider = image_provider
        self.audio_provider = audio_provider

    def prepare(self, session: Session, project: Project) -> PreparedGeneration:
        script = self.script_provider.generate(title=project.title, story=project.story)
        if len(script.shots) != 4:
            raise ValueError("M2 Mock 脚本必须恰好生成 4 个镜头")

        records: list[dict[str, Any]] = []
        trace_shots: list[dict[str, Any]] = []
        plans: list[tuple[ScriptShot, dict[str, Any]]] = []
        for shot in script.shots:
            visual = self.image_provider.plan(shot=shot)
            audio = self.audio_provider.plan(shot=shot)
            parameters = {
                **visual.parameters,
                **audio.parameters,
                "provider_shot_id": shot.provider_shot_id,
                "visual_provider_id": visual.provider_id,
                "audio_provider_id": audio.provider_id,
                "source_type": visual.source_type,
            }
            records.append(
                {
                    "shot_index": shot.shot_index,
                    "title": shot.title,
                    "visual_description": shot.visual_description,
                    "narration": shot.narration,
                    "duration_seconds": shot.duration_seconds,
                    "provider_id": script.provider_id,
                    "parameters_json": parameters,
                }
            )
            trace_shots.append(
                {
                    "provider_shot_id": shot.provider_shot_id,
                    "shot_index": shot.shot_index,
                    "title": shot.title,
                    "visual_description": shot.visual_description,
                    "narration": shot.narration,
                    "duration_seconds": shot.duration_seconds,
                    "provider_id": script.provider_id,
                    "parameters": parameters,
                }
            )
            plans.append((shot, parameters))

        script_json = {
            "schema_version": "mock-script.v1",
            "fixture_version": script.fixture_version,
            "provider_id": script.provider_id,
            "source_type": script.source_type,
            "shots": trace_shots,
        }
        persisted = crud.replace_shots(
            session,
            project=project,
            script_json=script_json,
            shots=records,
        )
        media_shots: list[dict[str, Any]] = []
        for db_shot, (provider_shot, parameters) in zip(persisted, plans, strict=True):
            media_shots.append(
                {
                    "shot_id": db_shot.id,
                    "sequence_no": provider_shot.shot_index,
                    "title": provider_shot.title,
                    "visual_description": provider_shot.visual_description,
                    "subtitle_text": provider_shot.narration,
                    "duration_seconds": provider_shot.duration_seconds,
                    "provider_id": script.provider_id,
                    "source_type": script.source_type,
                    "generation_parameters": parameters,
                }
            )
        duration = sum(float(item["duration_seconds"]) for item in media_shots)
        return PreparedGeneration(tuple(media_shots), duration)

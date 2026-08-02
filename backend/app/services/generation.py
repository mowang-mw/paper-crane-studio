"""M3 Provider 编排：慢模型调用与数据库事务明确分离。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .. import crud
from ..models import Project
from ..providers.base import (
    AudioProvider,
    ImageProvider,
    ScriptProvider,
    ScriptResult,
    ScriptShot,
    script_result_from_v1,
)
from ..script_schema import ScriptV1, analyze_script_usage


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    """事务外生成完毕、等待短事务落库的不可变结果。"""

    script_json: dict[str, Any]
    script_trace: dict[str, Any]
    script_validation_warnings: dict[str, list[str]]
    shot_records: tuple[dict[str, Any], ...]
    media_shots: tuple[dict[str, Any], ...]
    planned_duration_seconds: float
    script_provider: str
    script_source_type: str
    desired_shot_count: int | None
    actual_shot_count: int
    story_char_count: int
    repair_used: bool
    duration_normalization: dict[str, Any]


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

    def prepare(
        self,
        *,
        title: str,
        story: str,
        desired_shot_count: int | None = None,
    ) -> PreparedGeneration:
        """在数据库事务外调用 Provider，并准备媒体与持久化快照。"""

        script = self.script_provider.generate(
            title=title,
            story=story,
            desired_shot_count=desired_shot_count,
        )
        return self._prepare_result(
            script=script,
            desired_shot_count=desired_shot_count,
            story_char_count=len(story.strip()),
        )

    def prepare_validated_script(
        self,
        *,
        script: ScriptV1,
        provider_id: str,
        source_type: str,
        trace: dict[str, Any],
        desired_shot_count: int | None,
        story_char_count: int,
    ) -> PreparedGeneration:
        """从追溯中的严格 ScriptV1 恢复媒体阶段，不调用 ScriptProvider。"""

        result = script_result_from_v1(
            script,
            provider_id=provider_id,
            source_type=source_type,
            trace=trace,
        )
        return self._prepare_result(
            script=result,
            desired_shot_count=desired_shot_count,
            story_char_count=story_char_count,
        )

    def _prepare_result(
        self,
        *,
        script: ScriptResult,
        desired_shot_count: int | None,
        story_char_count: int,
    ) -> PreparedGeneration:
        if not 3 <= len(script.shots) <= 5:
            raise ValueError(
                f"script.v1 必须生成 3—5 个镜头，实际 {len(script.shots)}"
            )
        if (
            desired_shot_count is not None
            and len(script.shots) != desired_shot_count
        ):
            raise ValueError(
                f"要求 {desired_shot_count} 个镜头，实际生成 {len(script.shots)} 个"
            )
        if script.script is None:
            raise ValueError("ScriptProvider 必须返回经过 ScriptV1 校验的完整 script")

        warnings = analyze_script_usage(script.script).model_dump(mode="json")
        script_trace = dict(script.trace or {})
        script_trace["validation_warnings"] = warnings
        script_trace.setdefault("desired_shot_count", desired_shot_count)
        script_trace.setdefault("story_char_count", story_char_count)
        duration_normalization = script_trace.get("duration_normalization")
        if not isinstance(duration_normalization, dict):
            duration_normalization = {
                "normalized": False,
                "original_durations": [],
                "normalized_durations": [],
                "original_total": None,
                "normalized_total": None,
                "reason": None,
            }

        records: list[dict[str, Any]] = []
        media_shots: list[dict[str, Any]] = []
        for shot in script.shots:
            visual = self.image_provider.plan(shot=shot)
            audio = self.audio_provider.plan(shot=shot)
            parameters = {
                **visual.parameters,
                **audio.parameters,
                "provider_shot_id": shot.provider_shot_id,
                "visual_provider_id": visual.provider_id,
                "audio_provider_id": audio.provider_id,
                "image_source_type": visual.source_type,
                "audio_source_type": audio.source_type,
                "scene_id": shot.scene_id,
                "character_ids": list(shot.character_ids),
                "camera": shot.camera,
                "image_prompt": shot.image_prompt,
                "negative_prompt": shot.negative_prompt,
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
            media_shots.append(
                {
                    "shot_id": shot.provider_shot_id,
                    "sequence_no": shot.shot_index,
                    "title": shot.title,
                    "visual_description": shot.visual_description,
                    "subtitle_text": shot.narration,
                    "duration_seconds": shot.duration_seconds,
                    # 这里描述的是即将被 FFmpeg 消费的媒体素材，而不是剧本来源。
                    # 剧本 Provider 单独保留，避免把 Mock 画面误标成 Qwen 生成。
                    "provider_id": visual.provider_id,
                    "source_type": visual.source_type,
                    "script_provider_id": script.provider_id,
                    "generation_parameters": parameters,
                }
            )

        duration = sum(float(item["duration_seconds"]) for item in media_shots)
        if not 20.0 <= duration <= 40.0:
            raise ValueError(f"script.v1 总时长必须为 20—40 秒，实际 {duration:g} 秒")
        return PreparedGeneration(
            script_json=script.script.model_dump(mode="json"),
            script_trace=script_trace,
            script_validation_warnings=warnings,
            shot_records=tuple(records),
            media_shots=tuple(media_shots),
            planned_duration_seconds=duration,
            script_provider=script.provider_id,
            script_source_type=script.source_type,
            desired_shot_count=desired_shot_count,
            actual_shot_count=len(script.shots),
            story_char_count=story_char_count,
            repair_used=bool(script_trace.get("repair_used")),
            duration_normalization=dict(duration_normalization),
        )

    @staticmethod
    def persist(
        session: Session,
        *,
        project: Project,
        prepared: PreparedGeneration,
    ) -> None:
        """仅执行快速 SQLite 写入；不得在这里调用模型或 FFmpeg。"""

        crud.replace_shots(
            session,
            project=project,
            script_json=prepared.script_json,
            shots=prepared.shot_records,
        )

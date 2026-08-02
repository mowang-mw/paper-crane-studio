"""最小 Provider 抽象；FFmpeg 合成刻意不伪装成模型 Provider。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..script_schema import ScriptV1


@dataclass(frozen=True, slots=True)
class ScriptShot:
    provider_shot_id: str
    shot_index: int
    title: str
    visual_description: str
    narration: str
    duration_seconds: float
    scene_id: str = ""
    character_ids: tuple[str, ...] = ()
    camera: str = ""
    image_prompt: str = ""
    negative_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ScriptResult:
    provider_id: str
    source_type: str
    fixture_version: str
    shots: tuple[ScriptShot, ...]
    script: ScriptV1 | None = None
    trace: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class VisualPlan:
    provider_id: str
    source_type: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AudioPlan:
    provider_id: str
    source_type: str
    parameters: dict[str, Any]


class ScriptProvider(ABC):
    provider_id: str

    @abstractmethod
    def generate(
        self,
        *,
        title: str,
        story: str,
        desired_shot_count: int | None = None,
    ) -> ScriptResult:
        raise NotImplementedError


class ImageProvider(ABC):
    provider_id: str

    @abstractmethod
    def plan(self, *, shot: ScriptShot) -> VisualPlan:
        raise NotImplementedError


class AudioProvider(ABC):
    provider_id: str

    @abstractmethod
    def plan(self, *, shot: ScriptShot) -> AudioPlan:
        raise NotImplementedError


def script_result_from_v1(
    script: ScriptV1,
    *,
    provider_id: str,
    source_type: str,
    trace: dict[str, Any] | None = None,
) -> ScriptResult:
    """把权威 ScriptV1 适配到已被 M2 编排服务使用的只读结果。"""

    return ScriptResult(
        provider_id=provider_id,
        source_type=source_type,
        fixture_version=script.schema_version,
        shots=tuple(
            ScriptShot(
                provider_shot_id=shot.id,
                shot_index=shot.index,
                title=shot.title,
                visual_description=shot.visual_description,
                narration=shot.narration,
                duration_seconds=shot.duration_seconds,
                scene_id=shot.scene_id,
                character_ids=tuple(shot.character_ids),
                camera=shot.camera,
                image_prompt=shot.image_prompt,
                negative_prompt=shot.negative_prompt,
            )
            for shot in script.shots
        ),
        script=script,
        trace=trace,
    )

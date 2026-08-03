"""最小 Provider 抽象；FFmpeg 合成刻意不伪装成模型 Provider。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..script_schema import Character, Scene, ScriptV1, Shot


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
class ImageGenerationOptions:
    """一次真实关键帧 Job 的不可变生成参数快照。"""

    width: int = 1024
    height: int = 576
    steps: int = 24
    cfg: float = 5.0
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"
    denoise: float = 1.0
    batch_size: int = 1
    base_seed: int = 20_260_802
    lowvram: bool = True
    startup_timeout_seconds: float = 240.0
    generation_timeout_seconds: float = 1_200.0
    job_timeout_seconds: float = 3_600.0
    http_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.width < 320 or self.height < 180:
            raise ValueError("图像分辨率至少为 320x180")
        if self.width % 8 or self.height % 8:
            raise ValueError("图像宽高必须是 8 的倍数")
        if not 1 <= self.steps <= 100:
            raise ValueError("steps 必须在 1—100 之间")
        if not 0 < self.cfg <= 30:
            raise ValueError("cfg 必须在 0—30 之间")
        if not 0 < self.denoise <= 1:
            raise ValueError("denoise 必须在 0—1 之间")
        if self.batch_size != 1:
            raise ValueError("M4-B 固定 batch_size=1")
        if type(self.base_seed) is not int or not 0 <= self.base_seed < 2**63:
            raise ValueError("base_seed 必须是 0 到 2^63-1 之间的整数")
        if not self.sampler.strip() or not self.scheduler.strip():
            raise ValueError("sampler 和 scheduler 不得为空")
        for name in (
            "startup_timeout_seconds",
            "generation_timeout_seconds",
            "job_timeout_seconds",
            "http_timeout_seconds",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} 必须大于 0")

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg": self.cfg,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "denoise": self.denoise,
            "batch_size": self.batch_size,
            "base_seed": self.base_seed,
            "lowvram": self.lowvram,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "generation_timeout_seconds": self.generation_timeout_seconds,
            "job_timeout_seconds": self.job_timeout_seconds,
            "http_timeout_seconds": self.http_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    """生成单个镜头关键帧所需的完整、可追溯输入。"""

    project_id: str
    job_id: str
    script: ScriptV1
    shot: Shot
    characters: tuple[Character, ...]
    scene: Scene
    output_dir: Path
    options: ImageGenerationOptions

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.job_id.strip():
            raise ValueError("project_id 和 job_id 不得为空")
        if self.shot.scene_id != self.scene.id:
            raise ValueError("ImageGenerationRequest 的 shot 与 scene 不匹配")
        expected_character_ids = tuple(self.shot.character_ids)
        actual_character_ids = tuple(character.id for character in self.characters)
        if actual_character_ids != expected_character_ids:
            raise ValueError("ImageGenerationRequest 的角色顺序必须与 shot.character_ids 一致")
        if self.shot not in self.script.shots:
            raise ValueError("ImageGenerationRequest 的 shot 不属于给定 ScriptV1")
        object.__setattr__(self, "output_dir", Path(self.output_dir).resolve())


@dataclass(frozen=True, slots=True)
class GeneratedImageAsset:
    """ImageProvider 返回给 Worker 的单张关键帧及其追溯信息。"""

    provider_id: str
    model_id: str
    shot_id: str
    image_path: Path
    width: int
    height: int
    seed: int
    positive_prompt: str
    negative_prompt: str
    generation_seconds: float
    image_sha256: str
    model_sha256: str
    workflow_path: Path
    trace_path: Path
    warnings: tuple[str, ...] = ()
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "shot_id": self.shot_id,
            "image_path": str(self.image_path),
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "generation_seconds": self.generation_seconds,
            "image_sha256": self.image_sha256,
            "model_sha256": self.model_sha256,
            "workflow_path": str(self.workflow_path),
            "trace_path": str(self.trace_path),
            "warnings": list(self.warnings),
            "reused": self.reused,
        }


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

    def generate(self, *, request: ImageGenerationRequest) -> GeneratedImageAsset:
        """生成真实图片；仅规划型 Provider 可保留默认的不支持行为。"""

        raise NotImplementedError(f"{self.provider_id} ImageProvider 不支持独立图片生成")

    def generate_batch(
        self,
        *,
        requests: tuple[ImageGenerationRequest, ...],
        reusable_assets: tuple[GeneratedImageAsset, ...] = (),
        progress_callback: Callable[[int, int, GeneratedImageAsset], None] | None = None,
    ) -> tuple[GeneratedImageAsset, ...]:
        """默认顺序实现；有状态 Provider 应覆写以复用一次模型生命周期。"""

        del reusable_assets
        generated: list[GeneratedImageAsset] = []
        for request in requests:
            asset = self.generate(request=request)
            generated.append(asset)
            if progress_callback:
                progress_callback(len(generated), len(requests), asset)
        return tuple(generated)


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

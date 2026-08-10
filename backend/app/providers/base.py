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


@dataclass(frozen=True, slots=True)
class AudioGenerationOptions:
    """一次真实旁白 Job 的不可变参数快照。"""

    speaker: str = "Serena"
    language: str = "Chinese"
    base_seed: int = 20_260_803
    model_load_timeout_seconds: float = 300.0
    generation_timeout_seconds: float = 300.0
    job_timeout_seconds: float = 1_800.0

    def __post_init__(self) -> None:
        if not self.speaker.strip():
            raise ValueError("speaker 不得为空")
        if not self.language.strip():
            raise ValueError("language 不得为空")
        if type(self.base_seed) is not int or not 0 <= self.base_seed < 2**63 - 6:
            raise ValueError("base_seed 必须是 0 到 2^63-6 之间的整数")
        for name in (
            "model_load_timeout_seconds",
            "generation_timeout_seconds",
            "job_timeout_seconds",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.job_timeout_seconds < (
            self.model_load_timeout_seconds + self.generation_timeout_seconds
        ):
            raise ValueError("job_timeout_seconds 必须覆盖模型加载和至少一次生成")

    def as_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "language": self.language,
            "base_seed": self.base_seed,
            "model_load_timeout_seconds": self.model_load_timeout_seconds,
            "generation_timeout_seconds": self.generation_timeout_seconds,
            "job_timeout_seconds": self.job_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class AudioGenerationRequest:
    """生成一个镜头旁白所需的严格输入；正文直接来自 ScriptV1。"""

    project_id: str
    job_id: str
    source_script_job_id: str
    source_image_job_id: str | None
    script: ScriptV1
    shot: Shot
    output_dir: Path
    options: AudioGenerationOptions

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.job_id.strip():
            raise ValueError("project_id 和 job_id 不得为空")
        if not self.source_script_job_id.strip():
            raise ValueError("source_script_job_id 不得为空")
        if self.source_image_job_id is not None and not self.source_image_job_id.strip():
            raise ValueError("source_image_job_id 提供时不得为空")
        if self.shot not in self.script.shots:
            raise ValueError("AudioGenerationRequest 的 shot 不属于给定 ScriptV1")
        if not self.shot.narration.strip():
            raise ValueError("真实 TTS 旁白不得为空")
        object.__setattr__(self, "output_dir", Path(self.output_dir).resolve())


@dataclass(frozen=True, slots=True)
class VideoGenerationOptions:
    """Deterministic options for an optional per-shot video provider."""

    width: int = 1280
    height: int = 720
    fps: int = 24
    duration_seconds: float = 2.0
    motion_preset: str = "gentle_zoom"

    def __post_init__(self) -> None:
        if self.width < 320 or self.height < 180 or self.width % 8 or self.height % 8:
            raise ValueError("video dimensions must be at least 320x180 and divisible by 8")
        if self.fps <= 0 or self.fps > 120:
            raise ValueError("video fps must be between 1 and 120")
        if self.duration_seconds <= 0 or self.duration_seconds > 60:
            raise ValueError("video duration must be between 0 and 60 seconds")
        if self.motion_preset not in {"static", "gentle_zoom", "cinematic_pan"}:
            raise ValueError("unsupported video motion preset")

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_seconds": self.duration_seconds,
            "motion_preset": self.motion_preset,
        }


@dataclass(frozen=True, slots=True)
class VideoGenerationRequest:
    project_id: str
    job_id: str
    shot: ScriptShot
    source_image_path: Path
    prompt: str
    motion_description: str
    output_dir: Path
    options: VideoGenerationOptions
    motion_prompt: str | None = None

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.job_id.strip():
            raise ValueError("project_id and job_id must not be empty")
        source_image_path = Path(self.source_image_path).resolve()
        if not source_image_path.is_file():
            raise ValueError("source keyframe image does not exist")
        if not self.prompt.strip():
            raise ValueError("video prompt must not be empty")
        object.__setattr__(self, "source_image_path", source_image_path)
        object.__setattr__(self, "output_dir", Path(self.output_dir).resolve())


@dataclass(frozen=True, slots=True)
class VideoPlan:
    provider_id: str
    source_type: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GeneratedVideoAsset:
    provider_id: str
    shot_id: str
    video_path: Path
    duration_seconds: float
    width: int
    height: int
    fps: int
    source_type: str
    video_sha256: str
    trace_path: Path
    metadata: dict[str, Any]
    status: str = "SUCCEEDED"
    success: bool = True
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "shot_id": self.shot_id,
            "video_path": str(self.video_path),
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "source_type": self.source_type,
            "video_sha256": self.video_sha256,
            "trace_path": str(self.trace_path),
            "metadata": self.metadata,
            "status": self.status,
            "success": self.success,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class GeneratedAudioAsset:
    """AudioProvider 返回的真实 PCM WAV 与完整追溯。"""

    provider_id: str
    model_id: str
    model_revision: str
    model_sha256: str
    shot_id: str
    audio_path: Path
    trace_path: Path
    text: str
    speaker: str
    language: str
    seed: int
    sample_rate: int
    channels: int
    sample_width_bytes: int
    duration_seconds: float
    generation_seconds: float
    real_time_factor: float
    peak_amplitude: float
    rms: float
    audio_sha256: str
    warnings: tuple[str, ...] = ()
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "shot_id": self.shot_id,
            "audio_path": str(self.audio_path),
            "trace_path": str(self.trace_path),
            "text": self.text,
            "speaker": self.speaker,
            "language": self.language,
            "seed": self.seed,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width_bytes,
            "duration_seconds": self.duration_seconds,
            "generation_seconds": self.generation_seconds,
            "real_time_factor": self.real_time_factor,
            "peak_amplitude": self.peak_amplitude,
            "rms": self.rms,
            "audio_sha256": self.audio_sha256,
            "warnings": list(self.warnings),
            "reused": self.reused,
        }


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

    def generate(self, *, request: AudioGenerationRequest) -> GeneratedAudioAsset:
        """生成真实旁白；仅规划型 Provider 可保留默认的不支持行为。"""

        raise NotImplementedError(f"{self.provider_id} AudioProvider 不支持旁白生成")

    def generate_batch(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...] = (),
        progress_callback: Callable[[int, int, GeneratedAudioAsset], None] | None = None,
    ) -> tuple[GeneratedAudioAsset, ...]:
        """默认顺序实现；本地模型 Provider 应覆写以只加载一次模型。"""

        del reusable_assets
        generated: list[GeneratedAudioAsset] = []
        for request in requests:
            asset = self.generate(request=request)
            generated.append(asset)
            if progress_callback:
                progress_callback(len(generated), len(requests), asset)
        return tuple(generated)


class VideoProvider(ABC):
    """Optional image-to-video stage; final media may ignore these assets."""

    provider_id: str

    @abstractmethod
    def plan(self, *, shot: ScriptShot) -> VideoPlan:
        raise NotImplementedError

    @abstractmethod
    def generate(self, *, request: VideoGenerationRequest) -> GeneratedVideoAsset:
        raise NotImplementedError

    def generate_batch(
        self,
        *,
        requests: tuple[VideoGenerationRequest, ...],
        progress_callback: Callable[[int, int, GeneratedVideoAsset], None] | None = None,
    ) -> tuple[GeneratedVideoAsset, ...]:
        generated: list[GeneratedVideoAsset] = []
        for request in requests:
            asset = self.generate(request=request)
            generated.append(asset)
            if progress_callback:
                progress_callback(len(generated), len(requests), asset)
        return tuple(generated)


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

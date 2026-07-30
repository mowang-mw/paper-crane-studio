"""最小 Provider 抽象；FFmpeg 合成刻意不伪装成模型 Provider。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ScriptShot:
    provider_shot_id: str
    shot_index: int
    title: str
    visual_description: str
    narration: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ScriptResult:
    provider_id: str
    source_type: str
    fixture_version: str
    shots: tuple[ScriptShot, ...]


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
    def generate(self, *, title: str, story: str) -> ScriptResult:
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


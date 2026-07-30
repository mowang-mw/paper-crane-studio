"""无网络、无模型权重的确定性 Mock Provider。"""

from __future__ import annotations

import json
from pathlib import Path

from .base import (
    AudioPlan,
    AudioProvider,
    ImageProvider,
    ScriptProvider,
    ScriptResult,
    ScriptShot,
    VisualPlan,
)


SOURCE_TYPE = "DETERMINISTIC_FALLBACK"


class MockScriptProvider(ScriptProvider):
    provider_id = "mock"

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()

    def generate(self, *, title: str, story: str) -> ScriptResult:
        if "纸鹤" in title or "纸鹤" in story:
            return self._paper_crane_fixture()
        story_excerpt = " ".join(story.split())[:32] or "一个尚未命名的故事"
        titles = ("故事启程", "意外发生", "穿越旅途", "抵达远方")
        descriptions = (
            f"以夜色中的远景建立故事：{story_excerpt}",
            "主角发现微光，画面由静止转为缓慢推进。",
            "旅途经过城市轮廓与云层，镜头向右平移。",
            "晨光出现，主角在开阔远景中完成这段旅程。",
        )
        narrations = (
            f"故事从这里开始：{story_excerpt}",
            "一束微光，让安静的夜晚有了方向。",
            "它越过屋顶和云层，继续向前。",
            "黎明到来，远方终于清晰可见。",
        )
        shots = tuple(
            ScriptShot(
                provider_shot_id=f"mock_shot_{index:02d}",
                shot_index=index,
                title=titles[index - 1],
                visual_description=descriptions[index - 1],
                narration=narrations[index - 1],
                duration_seconds=7.0,
            )
            for index in range(1, 5)
        )
        return ScriptResult(self.provider_id, SOURCE_TYPE, "generic.mock.v1", shots)

    def _paper_crane_fixture(self) -> ScriptResult:
        fixture_path = self.root_dir / "fixtures" / "paper-crane" / "script.v1.json"
        try:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取固定演示 fixture：{fixture_path}") from exc
        shots = tuple(
            ScriptShot(
                provider_shot_id=str(item["shot_id"]),
                shot_index=int(item["sequence_no"]),
                title=str(item["title"]),
                visual_description=str(item["visual_description"]),
                narration=str(item["subtitle_text"]),
                duration_seconds=float(item["duration_seconds"]),
            )
            for item in fixture["shots"]
        )
        return ScriptResult(
            self.provider_id,
            SOURCE_TYPE,
            str(fixture["fixture_version"]),
            shots,
        )


class MockImageProvider(ImageProvider):
    provider_id = "mock"
    _TEMPLATES = (
        ("0x0d1730", "rainy_window", "RAINY WINDOW", "PUSH_IN"),
        ("0x0b2840", "glowing_flight", "GLOWING FLIGHT", "PULL_OUT"),
        ("0x17173d", "rooftop_clouds", "ROOFTOPS AND CLOUDS", "PAN_RIGHT"),
        ("0x75435f", "dawn_horizon", "DAWN HORIZON", "PUSH_IN_FADE"),
    )

    def plan(self, *, shot: ScriptShot) -> VisualPlan:
        try:
            background, composition, label, motion = self._TEMPLATES[shot.shot_index - 1]
        except IndexError as exc:
            raise ValueError("M2 Mock 视觉仅支持 1—4 号镜头") from exc
        return VisualPlan(
            self.provider_id,
            SOURCE_TYPE,
            {
                "seed": 4100 + shot.shot_index,
                "background_color": background,
                "composition_template": composition,
                "scene_label": label,
                "motion": motion,
                "width": 1280,
                "height": 720,
                "fps": 24,
            },
        )


class MockAudioProvider(AudioProvider):
    provider_id = "mock"
    _FREQUENCIES = (261.63, 329.63, 392.0, 523.25)

    def plan(self, *, shot: ScriptShot) -> AudioPlan:
        try:
            frequency = self._FREQUENCIES[shot.shot_index - 1]
        except IndexError as exc:
            raise ValueError("M2 Mock 音频仅支持 1—4 号镜头") from exc
        return AudioPlan(
            self.provider_id,
            SOURCE_TYPE,
            {
                "audio_frequency_hz": frequency,
                "sample_rate": 48_000,
                "method": "deterministic_pcm_wave",
            },
        )


"""无网络、无模型权重的确定性 Mock Provider。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Sequence

from ..media.ffmpeg import resolve_media_tools, run_command, sha256_file
from ..script_schema import Character, Scene, ScriptV1, Shot
from .base import (
    AudioPlan,
    AudioProvider,
    ImageProvider,
    GeneratedVideoAsset,
    ScriptProvider,
    ScriptResult,
    ScriptShot,
    VisualPlan,
    VideoGenerationRequest,
    VideoPlan,
    VideoProvider,
    script_result_from_v1,
)


SOURCE_TYPE = "DETERMINISTIC_FALLBACK"


class MockScriptProvider(ScriptProvider):
    provider_id = "mock"

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.last_script: ScriptV1 | None = None

    def generate(
        self,
        *,
        title: str,
        story: str,
        desired_shot_count: int | None = None,
    ) -> ScriptResult:
        if desired_shot_count not in (None, 3, 4, 5):
            raise ValueError("desired_shot_count 只允许 3、4、5 或 null")
        actual_shot_count = desired_shot_count or 4
        if "纸鹤" in title or "纸鹤" in story:
            script = self._paper_crane_fixture(actual_shot_count)
        else:
            script = self._generic_script(
                title=title,
                story=story,
                shot_count=actual_shot_count,
            )
        self.last_script = script
        return script_result_from_v1(
            script,
            provider_id=self.provider_id,
            source_type=SOURCE_TYPE,
            trace={
                "provider_id": self.provider_id,
                "source_type": SOURCE_TYPE,
                "schema_version": script.schema_version,
                "fixture": "paper-crane" if "纸鹤" in title or "纸鹤" in story else "generic",
                "desired_shot_count": desired_shot_count,
                "actual_shot_count": len(script.shots),
                "story_char_count": len(story.strip()),
                "repair_used": False,
                "duration_normalization": {
                    "normalized": False,
                    "original_durations": [],
                    "normalized_durations": [],
                    "original_total": None,
                    "normalized_total": None,
                    "reason": None,
                },
            },
        )

    def _generic_script(
        self,
        *,
        title: str,
        story: str,
        shot_count: int,
    ) -> ScriptV1:
        normalized_story = " ".join(story.split())
        story_excerpt = normalized_story[:20] or "一个尚未命名的故事"
        titles = ("故事启程", "意外发生", "穿越旅途", "发现转机", "抵达远方")
        descriptions = (
            f"以夜色中的远景建立故事：{story_excerpt}",
            "主角发现微光，画面由静止转为缓慢推进。",
            "旅途经过城市轮廓与云层，镜头向右平移。",
            "主角在高处发现新的方向，微光在画面中央聚拢。",
            "晨光出现，主角在开阔远景中完成这段旅程。",
        )
        narrations = (
            f"故事从这里开始：{story_excerpt}",
            "一束微光，让安静的夜晚有了方向。",
            "它越过屋顶和云层，继续向前。",
            "穿过夜色以后，新的方向出现在眼前。",
            "黎明到来，远方终于清晰可见。",
        )
        cameras = ("缓慢推近", "缓慢拉远", "向右平移", "轻微上摇", "缓慢推近后淡出")
        scene_titles = ("夜色起点", "微光近景", "屋顶旅途", "高处转机", "黎明远景")
        source_indexes = {
            3: (0, 2, 4),
            4: (0, 1, 2, 4),
            5: (0, 1, 2, 3, 4),
        }[shot_count]
        duration_seconds = 8.0 if shot_count == 3 else 7.0
        scenes = [
            Scene(
                id=f"scene_{index:02d}",
                name=scene_titles[source_index],
                description=descriptions[source_index],
                time="黎明" if source_index == 4 else "夜晚",
                lighting="暖色晨光" if source_index == 4 else "柔和月光与微光",
                consistency_prompt=f"原创二维动漫场景，{scene_titles[source_index]}，16:9",
            )
            for index, source_index in enumerate(source_indexes, start=1)
        ]
        shots = [
            Shot(
                id=f"shot_{index:02d}",
                index=index,
                title=titles[source_index],
                scene_id=f"scene_{index:02d}",
                character_ids=["character_01"],
                visual_description=descriptions[source_index],
                camera=cameras[source_index],
                image_prompt=(
                    f"原创二维动漫概念画，统一主角造型，16:9，"
                    f"{descriptions[source_index]}"
                ),
                negative_prompt="文字，水印，品牌标志，角色畸变",
                narration=narrations[source_index],
                duration_seconds=duration_seconds,
            )
            for index, source_index in enumerate(source_indexes, start=1)
        ]
        return ScriptV1(
            schema_version="script.v1",
            title=title.strip() or "未命名故事",
            synopsis=normalized_story[:500] or story_excerpt,
            characters=[
                Character(
                    id="character_01",
                    name="主角",
                    role="推动故事完成旅程的主角",
                    appearance="原创少年角色，深色短发，清晰自然的面部特征。",
                    personality="安静、好奇、坚定",
                    costume="浅色外套与深色长裤",
                    consistency_prompt="同一原创少年，深色短发，浅色外套，二维动漫造型一致",
                )
            ],
            scenes=scenes,
            shots=shots,
        )

    def _paper_crane_fixture(self, shot_count: int) -> ScriptV1:
        fixture_path = self.root_dir / "fixtures" / "paper-crane" / "script.v1.json"
        try:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取固定演示 fixture：{fixture_path}") from exc
        fixture_shots = list(fixture["shots"])
        fixture_shots.append(
            {
                "shot_id": "shot_05",
                "sequence_no": 5,
                "title": "晨光回响",
                "visual_description": "晨光铺满窗台，纸鹤停在银杏叶旁，阿澄微笑注视远方。",
                "subtitle_text": "夜航结束了，微光却留在清晨的窗边。",
                "duration_seconds": 7.0,
            }
        )
        source_indexes = {
            3: (0, 2, 3),
            4: (0, 1, 2, 3),
            5: (0, 1, 2, 3, 4),
        }[shot_count]
        selected = [fixture_shots[index] for index in source_indexes]
        duration_seconds = 8.0 if shot_count == 3 else 7.0
        cameras = ("缓慢推近", "缓慢拉远", "向右平移", "缓慢推近后淡出", "缓慢拉远")
        shots = [
            Shot(
                id=f"shot_{index:02d}",
                index=index,
                title=str(item["title"]),
                scene_id=f"scene_{index:02d}",
                character_ids=["character_01"],
                visual_description=str(item["visual_description"]),
                camera=cameras[source_index],
                image_prompt=(
                    "原创二维动漫概念画，少女阿澄保持深色短发与浅色居家服，"
                    f"16:9，{item['visual_description']}"
                ),
                negative_prompt="文字，水印，品牌标志，角色畸变",
                narration=str(item["subtitle_text"]),
                duration_seconds=duration_seconds,
            )
            for index, (source_index, item) in enumerate(
                zip(source_indexes, selected),
                start=1,
            )
        ]
        scenes = [
            Scene(
                id=f"scene_{index:02d}",
                name=str(item["title"]),
                description=str(item["visual_description"]),
                time="黎明" if source_index >= 3 else "雨夜",
                lighting="暖色晨光" if source_index >= 3 else "烛光与冷色窗光",
                consistency_prompt=f"原创二维动漫场景，{item['title']}，16:9",
            )
            for index, (source_index, item) in enumerate(
                zip(source_indexes, selected),
                start=1,
            )
        ]
        return ScriptV1(
            schema_version="script.v1",
            title=str(fixture["project"]["title"]),
            synopsis="雨夜中，阿澄折出的纸鹤被微光唤醒，飞越城市并迎来黎明。",
            characters=[
                Character(
                    id="character_01",
                    name="阿澄",
                    role="折出纸鹤并见证夜航的主角",
                    appearance="原创少女，深色短发，眼神温和，身形纤细。",
                    personality="安静、专注、富有想象力",
                    costume="浅色居家服与深色披肩",
                    consistency_prompt="同一原创少女阿澄，深色短发，浅色居家服，二维动漫造型一致",
                )
            ],
            scenes=scenes,
            shots=shots,
        )


class MockImageProvider(ImageProvider):
    provider_id = "mock"
    _TEMPLATES = (
        ("0x0d1730", "rainy_window", "RAINY WINDOW", "PUSH_IN"),
        ("0x0b2840", "glowing_flight", "GLOWING FLIGHT", "PULL_OUT"),
        ("0x17173d", "rooftop_clouds", "ROOFTOPS AND CLOUDS", "PAN_RIGHT"),
        ("0x75435f", "dawn_horizon", "DAWN HORIZON", "PUSH_IN_FADE"),
        ("0x31506b", "dawn_horizon", "STORY EPILOGUE", "PULL_OUT"),
    )

    def plan(self, *, shot: ScriptShot) -> VisualPlan:
        try:
            background, composition, label, motion = self._TEMPLATES[shot.shot_index - 1]
        except IndexError as exc:
            raise ValueError("Mock 视觉仅支持 1—5 号镜头") from exc
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
    _FREQUENCIES = (261.63, 329.63, 392.0, 523.25, 659.25)

    def plan(self, *, shot: ScriptShot) -> AudioPlan:
        try:
            frequency = self._FREQUENCIES[shot.shot_index - 1]
        except IndexError as exc:
            raise ValueError("Mock 音频仅支持 1—5 号镜头") from exc
        return AudioPlan(
            self.provider_id,
            SOURCE_TYPE,
            {
                "audio_frequency_hz": frequency,
                "sample_rate": 48_000,
                "method": "deterministic_pcm_wave",
            },
        )


VideoCommandRunner = Callable[[Sequence[str]], None]


class MockVideoProvider(VideoProvider):
    """Create a traceable mock MP4 from a keyframe without claiming AI inference."""

    provider_id = "mock-video"
    source_type = "MOCK"

    def __init__(
        self,
        *,
        ffmpeg_path: Path | None = None,
        command_runner: VideoCommandRunner | None = None,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.command_runner = command_runner

    def plan(self, *, shot: ScriptShot) -> VideoPlan:
        return VideoPlan(
            provider_id=self.provider_id,
            source_type=self.source_type,
            parameters={
                "method": "deterministic_ffmpeg_keyframe_video",
                "shot_index": shot.shot_index,
                "seed": 81_000 + shot.shot_index,
            },
        )

    def generate(self, *, request: VideoGenerationRequest) -> GeneratedVideoAsset:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = request.output_dir / f"{request.shot.provider_shot_id}.mp4"
        trace_path = request.output_dir / f"{request.shot.provider_shot_id}.video-trace.json"
        ffmpeg = self.ffmpeg_path or resolve_media_tools().ffmpeg
        options = request.options
        duration = f"{options.duration_seconds:.3f}"
        filter_graph = (
            f"scale={options.width}:{options.height}:force_original_aspect_ratio=decrease,"
            f"pad={options.width}:{options.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={options.fps},format=yuv420p"
        )
        command = [
            str(ffmpeg),
            "-y",
            "-loop",
            "1",
            "-i",
            str(request.source_image_path),
            "-t",
            duration,
            "-vf",
            filter_graph,
            "-an",
            "-c:v",
            "libx264",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        if self.command_runner is None:
            run_command(command, timeout_seconds=max(60, int(options.duration_seconds * 10)))
        else:
            self.command_runner(command)
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("MockVideoProvider did not create a non-empty MP4")

        metadata = {
            "mock": True,
            "ai_video_generated": False,
            "method": "deterministic_ffmpeg_keyframe_video",
            "source_keyframe": str(request.source_image_path),
            "prompt": request.prompt,
            "motion_description": request.motion_description,
            "options": options.as_dict(),
            "plan": self.plan(shot=request.shot).parameters,
            "command": command,
        }
        trace_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return GeneratedVideoAsset(
            provider_id=self.provider_id,
            shot_id=request.shot.provider_shot_id,
            video_path=output_path,
            duration_seconds=options.duration_seconds,
            width=options.width,
            height=options.height,
            fps=options.fps,
            source_type=self.source_type,
            video_sha256=sha256_file(output_path),
            trace_path=trace_path,
            metadata=metadata,
        )

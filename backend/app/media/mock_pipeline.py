"""M0—M5 确定性 Mock、真实关键帧与真实旁白媒体流水线。"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ffmpeg import (
    MediaToolError,
    MediaTools,
    decode_media_fully,
    ffmpeg_filter_path,
    ffprobe_json,
    find_chinese_font,
    resolve_media_tools,
    run_command,
    runtime_summary,
    sha256_file,
    verify_media,
)
from .subtitles import BurnedSubtitle, prepare_burned_subtitle


WIDTH = 1280
HEIGHT = 720
CANVAS_WIDTH = 1344
CANVAS_HEIGHT = 756
FPS = 24
SAMPLE_RATE = 48_000
MOTION_PRESETS = {"static", "gentle_zoom", "cinematic_pan"}
DEFAULT_MOTION_PRESET = "gentle_zoom"
MOTION_SUPERSAMPLE = 2
M6_MEDIA_MANIFEST_VERSION = "m6.media-export.v1"


def _repo_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_media_target(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.part{output.suffix}")
    if temporary.exists():
        temporary.unlink()
    return temporary


def generate_mock_wav(path: Path, duration_seconds: float, frequency_hz: float) -> None:
    """生成低音量、确定性的双声道 Mock 提示音。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part.wav")
    if temporary.exists():
        temporary.unlink()

    frame_count = int(round(duration_seconds * SAMPLE_RATE))
    amplitude = int(32767 * 0.035)
    with wave.open(str(temporary), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        buffer = bytearray()
        for sample_index in range(frame_count):
            position = sample_index / SAMPLE_RATE
            envelope = min(1.0, position / 0.25, (duration_seconds - position) / 0.35)
            envelope = max(0.0, envelope)
            value = int(
                amplitude
                * envelope
                * (
                    0.72 * math.sin(2.0 * math.pi * frequency_hz * position)
                    + 0.28 * math.sin(2.0 * math.pi * frequency_hz * 1.5 * position)
                )
            )
            buffer.extend(struct.pack("<hh", value, value))
            if len(buffer) >= 256 * 1024:
                output.writeframesraw(buffer)
                buffer.clear()
        if buffer:
            output.writeframesraw(buffer)
    os.replace(temporary, path)


def load_script_fixture(fixture_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MediaToolError(f"找不到 M1 fixture：{fixture_path}") from exc
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"M1 fixture 不是有效 JSON：{fixture_path}") from exc

    if payload.get("fixture_version") != "script.v1":
        raise MediaToolError("fixture_version 必须为 script.v1")
    shots = payload.get("shots")
    if not isinstance(shots, list) or len(shots) != 4:
        raise MediaToolError("《纸鹤的夜航》fixture 必须恰好包含 4 个镜头")

    required = {
        "shot_id",
        "sequence_no",
        "title",
        "visual_description",
        "subtitle_text",
        "subtitle_file",
        "duration_seconds",
        "provider_id",
        "source_type",
        "generation_parameters",
    }
    expected_sequence = list(range(1, 5))
    actual_sequence: list[int] = []
    duration = 0.0
    for shot in shots:
        if not isinstance(shot, dict) or not required.issubset(shot):
            missing = sorted(required - set(shot if isinstance(shot, dict) else {}))
            raise MediaToolError(f"镜头 fixture 缺少字段：{missing}")
        actual_sequence.append(int(shot["sequence_no"]))
        duration += float(shot["duration_seconds"])
        if shot["provider_id"] != "mock":
            raise MediaToolError("M1 fixture 的 provider_id 必须明确为 mock")
        if shot["source_type"] != "DETERMINISTIC_FALLBACK":
            raise MediaToolError("M1 fixture 的 source_type 必须为 DETERMINISTIC_FALLBACK")
        subtitle_path = fixture_path.parent / str(shot["subtitle_file"])
        if not subtitle_path.is_file():
            raise MediaToolError(f"找不到镜头字幕文件：{subtitle_path}")
        if subtitle_path.read_text(encoding="utf-8").strip() != shot["subtitle_text"]:
            raise MediaToolError(f"字幕文件与 fixture 文本不一致：{subtitle_path}")

    if actual_sequence != expected_sequence:
        raise MediaToolError(f"镜头顺序必须为 {expected_sequence}，实际 {actual_sequence}")
    if abs(duration - 28.0) > 0.001:
        raise MediaToolError(f"固定 fixture 计划总时长必须为 28 秒，实际 {duration}")
    return payload


def _label_filter(
    font: Path,
    text: str,
    y: int,
    fontsize: int = 30,
    *,
    box_opacity: float = 0.42,
    box_border: int = 10,
) -> str:
    safe_text = text.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:")
    return (
        "drawtext="
        f"fontfile={ffmpeg_filter_path(font)}:"
        f"text='{safe_text}':fontcolor=white:fontsize={fontsize}:"
        f"x=36:y={y}:box=1:boxcolor=black@{box_opacity:.2f}:boxborderw={box_border}"
    )


def _composition_filters(template: str) -> list[str]:
    compositions = {
        "rainy_window": [
            "drawbox=x=0:y=545:w=1344:h=211:color=0x241b2b:t=fill",
            "drawbox=x=820:y=70:w=390:h=420:color=0x6aa9d8@0.25:t=fill",
            "drawbox=x=820:y=70:w=390:h=420:color=0xb8dcff@0.7:t=8",
            "drawbox=x=1011:y=70:w=8:h=420:color=0xb8dcff@0.55:t=fill",
            "drawbox=x=820:y=276:w=390:h=8:color=0xb8dcff@0.55:t=fill",
            "drawbox=x=285:y=300:w=120:h=245:color=0x17121d@0.92:t=fill",
            "drawbox=x=310:y=245:w=70:h=70:color=0x17121d@0.92:t=fill",
            "drawbox=x=520:y=500:w=165:h=12:color=0xf4efe2@0.92:t=fill",
            "drawbox=x=590:y=466:w=24:h=52:color=0xffd98a@0.8:t=fill",
        ],
        "glowing_flight": [
            "drawbox=x=0:y=530:w=1344:h=226:color=0x10263a:t=fill",
            "drawbox=x=65:y=105:w=360:h=360:color=0x79cfff@0.18:t=fill",
            "drawbox=x=65:y=105:w=360:h=360:color=0xa9dcff@0.65:t=8",
            "drawbox=x=690:y=310:w=190:h=18:color=0xf7f3da@0.95:t=fill",
            "drawbox=x=760:y=260:w=45:h=115:color=0xffef9a@0.38:t=fill",
            "drawbox=x=625:y=270:w=75:h=75:color=0xe9f8ff@0.55:t=fill",
            "drawbox=x=930:y=140:w=130:h=130:color=0xb7e9ff@0.2:t=fill",
            "drawbox=x=985:y=195:w=20:h=20:color=0xffffff@0.95:t=fill",
        ],
        "rooftop_clouds": [
            "drawbox=x=0:y=520:w=1344:h=236:color=0x161326:t=fill",
            "drawbox=x=0:y=470:w=280:h=120:color=0x24203b:t=fill",
            "drawbox=x=300:y=430:w=330:h=160:color=0x211d38:t=fill",
            "drawbox=x=660:y=485:w=260:h=105:color=0x292340:t=fill",
            "drawbox=x=950:y=410:w=394:h=180:color=0x201b36:t=fill",
            "drawbox=x=125:y=505:w=18:h=18:color=0xffd675:t=fill",
            "drawbox=x=390:y=470:w=18:h=18:color=0xffd675:t=fill",
            "drawbox=x=1035:y=455:w=18:h=18:color=0xffd675:t=fill",
            "drawbox=x=420:y=145:w=370:h=75:color=0x9f9ac2@0.16:t=fill",
            "drawbox=x=735:y=295:w=180:h=16:color=0xf7f3da@0.9:t=fill",
        ],
        "dawn_horizon": [
            "drawbox=x=0:y=0:w=1344:h=190:color=0x60466d:t=fill",
            "drawbox=x=0:y=190:w=1344:h=170:color=0xa65e70:t=fill",
            "drawbox=x=0:y=360:w=1344:h=170:color=0xe29273:t=fill",
            "drawbox=x=0:y=530:w=1344:h=226:color=0x2b2636:t=fill",
            "drawbox=x=1040:y=250:w=115:h=115:color=0xffd68b@0.9:t=fill",
            "drawbox=x=130:y=120:w=300:h=385:color=0x281f31@0.92:t=fill",
            "drawbox=x=150:y=145:w=260:h=300:color=0xf3b47f@0.28:t=fill",
            "drawbox=x=570:y=285:w=210:h=18:color=0xfff3db@0.92:t=fill",
            "drawbox=x=650:y=245:w=38:h=98:color=0xfff0b8@0.35:t=fill",
        ],
    }
    try:
        return list(compositions[template])
    except KeyError as exc:
        raise MediaToolError(f"未知 Mock 构图模板：{template}") from exc


def _motion_filter(
    motion: str,
    frame_count: int,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    motion_preset: str | None = None,
) -> str:
    if motion_preset is not None:
        if motion_preset not in MOTION_PRESETS:
            raise MediaToolError(f"未知 motion preset：{motion_preset}")
        denominator = max(1, frame_count - 1)
        if motion_preset == "static":
            return (
                f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,"
                f"fps={fps}"
            )
        if motion_preset == "gentle_zoom":
            work_width = width * MOTION_SUPERSAMPLE
            work_height = height * MOTION_SUPERSAMPLE
            return (
                f"scale={work_width}:{work_height}:flags=lanczos,"
                f"zoompan=z='min(1.018,1+0.018*on/{denominator})':"
                "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':"
                f"d=1:s={work_width}x{work_height}:fps={fps},"
                f"scale={width}:{height}:flags=lanczos"
            )
        work_width = width * MOTION_SUPERSAMPLE
        work_height = height * MOTION_SUPERSAMPLE
        return (
            f"scale={work_width}:{work_height}:flags=lanczos,"
            "zoompan=z=1.04:"
            f"x='(iw-iw/zoom)*(0.10+0.80*on/{denominator})':"
            "y='(ih-ih/zoom)/2':"
            f"d=1:s={work_width}x{work_height}:fps={fps},"
            f"scale={width}:{height}:flags=lanczos"
        )
    common = f"d=1:s={width}x{height}:fps={fps}"
    if motion == "PUSH_IN":
        return (
            "zoompan=z='min(1.06,1+on*0.00036)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':" + common
        )
    if motion == "PULL_OUT":
        return (
            "zoompan=z='max(1.0,1.06-on*0.00036)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':" + common
        )
    if motion == "PAN_RIGHT":
        denominator = max(1, frame_count - 1)
        return (
            "zoompan=z=1.04:"
            f"x='(iw-iw/zoom)*on/{denominator}':"
            "y='ih/2-(ih/zoom/2)':" + common
        )
    if motion == "PUSH_IN_FADE":
        return (
            "zoompan=z='min(1.045,1+on*0.00027)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':" + common
        )
    raise MediaToolError(f"未知镜头运动：{motion}")


def _create_shot(
    *,
    tools: MediaTools,
    font: Path,
    subtitle: BurnedSubtitle,
    shot: dict[str, Any],
    output_path: Path,
    command_log: list[str],
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    motion_preset: str | None = None,
) -> dict[str, Any]:
    duration = float(shot["duration_seconds"])
    parameters = shot["generation_parameters"]
    frame_count = int(round(duration * fps))
    audio_path = output_path.with_suffix(".wav")
    generate_mock_wav(audio_path, duration, float(parameters["audio_frequency_hz"]))

    filters = _composition_filters(str(parameters["composition_template"]))
    filters.append(
        _motion_filter(
            str(parameters["motion"]),
            frame_count,
            width=width,
            height=height,
            fps=fps,
            motion_preset=motion_preset,
        )
    )
    filters.append(_label_filter(font, "MOCK VISUAL / FFMPEG MOTION", 84, 20))
    filters.append(subtitle.filter_expression)
    fade_out_start = max(0.0, duration - 0.35)
    filters.append(f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.3f}:d=0.35")
    filters.append("format=yuv420p")

    temporary = _atomic_media_target(output_path)
    source = (
        f"color=c={parameters['background_color']}:"
        f"s={width + 64}x{height + 36}:r={fps}:d={duration:.3f}"
    )
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            ",".join(filters),
            "-af",
            f"afade=t=in:st=0:d=0.2,afade=t=out:st={max(0.0, duration - 0.3):.3f}:d=0.3",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            temporary,
        ],
        timeout_seconds=240,
        command_log=command_log,
    )
    validation = verify_media(
        tools,
        temporary,
        min_duration=duration - 0.20,
        max_duration=duration + 0.20,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    return {
        "video_path": output_path,
        "audio_path": audio_path,
        "validation": validation,
        "sha256": sha256_file(output_path),
        "narration": subtitle.narration,
        "rendered_subtitle_text": subtitle.rendered_text,
        "subtitle_path": subtitle.text_path,
        "subtitle_filter": subtitle.filter_expression,
        "subtitle_font_path": subtitle.font_path,
        "subtitle_rendering": "burned_in",
    }


def _create_image_shot(
    *,
    tools: MediaTools,
    font: Path,
    subtitle: BurnedSubtitle,
    shot: dict[str, Any],
    keyframe: dict[str, Any],
    output_path: Path,
    command_log: list[str],
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    motion_preset: str | None = None,
) -> dict[str, Any]:
    """把已校验真实 PNG 转为带轻微运镜、Mock 音频和烧录字幕的镜头。"""

    duration = float(shot["duration_seconds"])
    parameters = shot["generation_parameters"]
    frame_count = int(round(duration * fps))
    audio_path = output_path.with_suffix(".wav")
    generate_mock_wav(audio_path, duration, float(parameters["audio_frequency_hz"]))

    canvas_width = width + 64
    canvas_height = height + 36
    filters = [
        (
            f"scale={canvas_width}:{canvas_height}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={canvas_width}:{canvas_height}",
        "setsar=1",
        _motion_filter(
            str(parameters["motion"]),
            frame_count,
            width=width,
            height=height,
            fps=fps,
            motion_preset=motion_preset,
        ),
        subtitle.filter_expression,
    ]
    fade_out_start = max(0.0, duration - 0.35)
    filters.append(f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.3f}:d=0.35")
    filters.append("format=yuv420p")

    temporary = _atomic_media_target(output_path)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            Path(str(keyframe["image_path"])),
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            ",".join(filters),
            "-af",
            f"afade=t=in:st=0:d=0.2,afade=t=out:st={max(0.0, duration - 0.3):.3f}:d=0.3",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            temporary,
        ],
        timeout_seconds=240,
        command_log=command_log,
    )
    validation = verify_media(
        tools,
        temporary,
        min_duration=duration - 0.20,
        max_duration=duration + 0.20,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    return {
        "video_path": output_path,
        "audio_path": audio_path,
        "validation": validation,
        "sha256": sha256_file(output_path),
        "narration": subtitle.narration,
        "rendered_subtitle_text": subtitle.rendered_text,
        "subtitle_path": subtitle.text_path,
        "subtitle_filter": subtitle.filter_expression,
        "subtitle_font_path": subtitle.font_path,
        "subtitle_rendering": "burned_in",
        "keyframe": keyframe,
    }


def _write_srt(shots: list[dict[str, Any]], target: Path) -> None:
    def stamp(value: float) -> str:
        milliseconds = int(round(value * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    lines: list[str] = []
    start = 0.0
    for index, shot in enumerate(shots, start=1):
        end = start + float(shot["duration_seconds"])
        lines.extend(
            [str(index), f"{stamp(start)} --> {stamp(end)}", shot["subtitle_text"], ""]
        )
        start = end
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")


_DEFAULT_SHOT_PARAMETERS: tuple[dict[str, Any], ...] = (
    {
        "seed": 4101,
        "background_color": "0x0d1730",
        "composition_template": "rainy_window",
        "scene_label": "RAINY WINDOW",
        "motion": "PUSH_IN",
        "audio_frequency_hz": 261.63,
    },
    {
        "seed": 4102,
        "background_color": "0x0b2840",
        "composition_template": "glowing_flight",
        "scene_label": "GLOWING FLIGHT",
        "motion": "PULL_OUT",
        "audio_frequency_hz": 329.63,
    },
    {
        "seed": 4103,
        "background_color": "0x17173d",
        "composition_template": "rooftop_clouds",
        "scene_label": "ROOFTOPS AND CLOUDS",
        "motion": "PAN_RIGHT",
        "audio_frequency_hz": 392.0,
    },
    {
        "seed": 4104,
        "background_color": "0x75435f",
        "composition_template": "dawn_horizon",
        "scene_label": "DAWN HORIZON",
        "motion": "PUSH_IN_FADE",
        "audio_frequency_hz": 523.25,
    },
    {
        "seed": 4105,
        "background_color": "0x31506b",
        "composition_template": "dawn_horizon",
        "scene_label": "STORY EPILOGUE",
        "motion": "PULL_OUT",
        "audio_frequency_hz": 659.25,
    },
)


def _safe_file_stem(value: str, fallback: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return stem or fallback


def _normalize_project_shots(
    shots: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    fps: int,
    provider_id: str,
) -> list[dict[str, Any]]:
    if not 3 <= len(shots) <= 5:
        raise MediaToolError(f"项目短片必须包含 3—5 个镜头，实际 {len(shots)}")
    if width < 320 or height < 180 or fps < 1:
        raise MediaToolError("媒体参数无效：分辨率至少 320x180，帧率至少 1 fps")

    normalized: list[dict[str, Any]] = []
    for index, original in enumerate(shots, start=1):
        required = {"shot_id", "title", "visual_description", "duration_seconds"}
        missing = sorted(required - set(original))
        if missing:
            raise MediaToolError(f"第 {index} 个镜头缺少字段：{missing}")
        sequence_no = int(original.get("sequence_no", original.get("shot_index", index)))
        if sequence_no != index:
            raise MediaToolError(
                f"镜头顺序必须连续且从 1 开始；第 {index} 项为 {sequence_no}"
            )
        duration = float(original["duration_seconds"])
        if duration <= 0:
            raise MediaToolError(f"镜头 {original['shot_id']} 时长必须大于 0")
        subtitle_text = str(
            original.get("subtitle_text", original.get("narration", ""))
        ).strip()
        if not subtitle_text:
            raise MediaToolError(f"镜头 {original['shot_id']} 缺少字幕或旁白")

        parameters = dict(_DEFAULT_SHOT_PARAMETERS[index - 1])
        supplied_parameters = original.get("generation_parameters", {})
        if not isinstance(supplied_parameters, dict):
            raise MediaToolError(f"镜头 {original['shot_id']} 的生成参数必须是对象")
        parameters.update(supplied_parameters)
        parameters.update({"width": width, "height": height, "fps": fps})
        normalized.append(
            {
                **original,
                "sequence_no": sequence_no,
                "subtitle_text": subtitle_text,
                "duration_seconds": duration,
                "provider_id": str(original.get("provider_id", provider_id)),
                "source_type": str(
                    original.get("source_type", "DETERMINISTIC_FALLBACK")
                ),
                "generation_parameters": parameters,
            }
        )

    total_duration = sum(float(shot["duration_seconds"]) for shot in normalized)
    if not 20.0 <= total_duration <= 40.0:
        raise MediaToolError(
            f"项目短片计划总时长必须在 20—40 秒内，实际 {total_duration:.3f} 秒"
        )
    return normalized


_REAL_IMAGE_SOURCE_TYPE = "REAL_LOCAL_MODEL"
_EXTERNAL_IMAGE_PROVIDER_ID = "external-import"
_EXTERNAL_IMAGE_SOURCE_TYPE = "EXTERNAL_IMPORT"
_EXTERNAL_IMAGE_GENERATION_MODE = "HUMAN_IN_THE_LOOP"
_REAL_AUDIO_SOURCE_TYPE = "REAL_LOCAL_TTS"
_AUDIO_LEAD_IN_SECONDS = 0.20
_AUDIO_LEAD_OUT_SECONDS = 0.35
_DEFAULT_RENDERED_DURATION_LIMIT_SECONDS = 60.0


def _required_keyframe_text(
    keyframe: dict[str, Any],
    field: str,
    *,
    shot_id: str,
) -> str:
    value = keyframe.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MediaToolError(f"真实关键帧 {shot_id} 缺少非空字段 {field}")
    return value.strip()


def _validate_real_keyframes(
    *,
    tools: MediaTools,
    shots: list[dict[str, Any]],
    keyframes: list[dict[str, Any]],
    command_log: list[str],
    required_shot_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """按实际图片来源校验关键帧；VIDEO_SHOT 的首帧仅保留为血缘。"""

    expected_ids = [str(shot["shot_id"]) for shot in shots]
    required_ids = (
        set(expected_ids) if required_shot_ids is None else set(required_shot_ids)
    )
    if not required_ids <= set(expected_ids):
        raise MediaToolError("真实图片入口要求了 ScriptV1 之外的镜头")
    if required_ids == set(expected_ids) and len(keyframes) != len(shots):
        raise MediaToolError(
            "真实图片镜头数必须与剧本镜头数一致："
            f"剧本 {len(shots)}，图片 {len(keyframes)}"
        )
    by_shot: dict[str, dict[str, Any]] = {}
    observed_ids: set[str] = set()
    local_provider_ids: set[str] = set()
    local_model_ids: set[str] = set()
    for position, original in enumerate(keyframes, start=1):
        if not isinstance(original, dict):
            raise MediaToolError(f"第 {position} 个真实关键帧结果必须是对象")
        keyframe = dict(original)
        shot_id = _required_keyframe_text(keyframe, "shot_id", shot_id=f"#{position}")
        if shot_id in observed_ids:
            raise MediaToolError(f"真实关键帧 shot_id 重复：{shot_id}")
        if shot_id not in expected_ids:
            raise MediaToolError(f"真实关键帧引用未知镜头：{shot_id}")
        observed_ids.add(shot_id)
        if shot_id not in required_ids:
            continue

        provider_id = _required_keyframe_text(
            keyframe, "provider_id", shot_id=shot_id
        )
        if provider_id.lower() == "mock":
            raise MediaToolError(f"真实图片入口禁止使用 Mock 关键帧：{shot_id}")
        source_type = str(
            keyframe.get("source_type", _REAL_IMAGE_SOURCE_TYPE)
        ).strip()
        if not source_type:
            raise MediaToolError(
                f"真实关键帧 {shot_id} 缺少非空字段 source_type"
            )
        is_local_model = source_type == _REAL_IMAGE_SOURCE_TYPE
        is_external_import = source_type == _EXTERNAL_IMAGE_SOURCE_TYPE
        if not is_local_model and not is_external_import:
            raise MediaToolError(
                f"真实关键帧 {shot_id} source_type 不受支持：{source_type}"
            )
        model_id: str | None = None
        seed: int | None = None
        if is_local_model:
            model_id = _required_keyframe_text(
                keyframe, "model_id", shot_id=shot_id
            )
            seed = keyframe.get("seed")
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                raise MediaToolError(f"真实关键帧 {shot_id} seed 必须是非负整数")
            local_provider_ids.add(provider_id)
            local_model_ids.add(model_id)
        else:
            if provider_id != _EXTERNAL_IMAGE_PROVIDER_ID:
                raise MediaToolError(
                    f"外部关键帧 {shot_id} provider_id 必须为 "
                    f"{_EXTERNAL_IMAGE_PROVIDER_ID}"
                )
            generation_mode = _required_keyframe_text(
                keyframe, "generation_mode", shot_id=shot_id
            )
            if generation_mode != _EXTERNAL_IMAGE_GENERATION_MODE:
                raise MediaToolError(f"外部关键帧 {shot_id} 导入模式追溯无效")
            external_source_type = _required_keyframe_text(
                keyframe, "external_source_type", shot_id=shot_id
            )
            if external_source_type not in {
                "AI_GENERATED",
                "HUMAN_CREATED",
                "OTHER",
            }:
                raise MediaToolError(f"外部关键帧 {shot_id} 来源类型追溯无效")
            for field in ("original_filename", "imported_at"):
                _required_keyframe_text(keyframe, field, shot_id=shot_id)
            provider_hint = keyframe.get("provider_hint")
            if provider_hint is not None and (
                not isinstance(provider_hint, str) or not provider_hint.strip()
            ):
                raise MediaToolError(
                    f"外部关键帧 {shot_id} provider_hint 追溯无效"
                )
        width = keyframe.get("width")
        height = keyframe.get("height")
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise MediaToolError(f"真实关键帧 {shot_id} 的 width/height 必须是正整数")

        image_value = _required_keyframe_text(keyframe, "image_path", shot_id=shot_id)
        image_path = Path(image_value).expanduser().resolve()
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            raise MediaToolError(f"真实关键帧不存在或为空：{image_path}")
        allowed_suffixes = {".png"} if is_local_model else {".png", ".jpg", ".jpeg"}
        if image_path.suffix.lower() not in allowed_suffixes:
            raise MediaToolError(f"真实关键帧格式不受支持：{image_path}")
        expected_sha256 = _required_keyframe_text(
            keyframe, "image_sha256", shot_id=shot_id
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise MediaToolError(f"真实关键帧 {shot_id} 的 image_sha256 格式无效")
        actual_sha256 = sha256_file(image_path)
        if actual_sha256 != expected_sha256:
            raise MediaToolError(
                f"真实关键帧 {shot_id} SHA-256 不符："
                f"记录 {expected_sha256}，实际 {actual_sha256}"
            )

        probe = ffprobe_json(tools, image_path, command_log=command_log)
        streams = probe.get("streams")
        videos = (
            [item for item in streams if item.get("codec_type") == "video"]
            if isinstance(streams, list)
            else []
        )
        expected_codec = "png" if image_path.suffix.lower() == ".png" else "mjpeg"
        if len(videos) != 1 or videos[0].get("codec_name") != expected_codec:
            raise MediaToolError(f"真实关键帧 {shot_id} 不是可识别的单流图片")
        actual_size = (videos[0].get("width"), videos[0].get("height"))
        if actual_size != (width, height):
            raise MediaToolError(
                f"真实关键帧 {shot_id} 尺寸不符："
                f"记录 {width}x{height}，实际 {actual_size[0]}x{actual_size[1]}"
            )
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                image_path,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=60,
            command_log=command_log,
        )

        keyframe.update(
            {
                "shot_id": shot_id,
                "provider_id": provider_id,
                "source_type": source_type,
                "width": width,
                "height": height,
                "image_path": str(image_path),
                "image_sha256": actual_sha256,
            }
        )
        if is_local_model:
            keyframe["model_id"] = model_id
            keyframe["seed"] = seed
        warnings = keyframe.get("warnings", [])
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise MediaToolError(f"真实关键帧 {shot_id} warnings 必须是字符串数组")
        keyframe["warnings"] = list(warnings)
        by_shot[shot_id] = keyframe

    missing = [
        shot_id
        for shot_id in expected_ids
        if shot_id in required_ids and shot_id not in by_shot
    ]
    if missing:
        raise MediaToolError(f"真实图片入口缺少镜头关键帧：{missing}")
    if len(local_provider_ids) > 1 or len(local_model_ids) > 1:
        raise MediaToolError(
            "同一真实图片导出不得混用多个 ImageProvider 或模型："
            f"providers={sorted(local_provider_ids)}, models={sorted(local_model_ids)}"
        )
    return by_shot


def _normalize_final_visual_sources(
    *,
    tools: MediaTools,
    shots: list[dict[str, Any]],
    keyframes_by_shot: dict[str, dict[str, Any]],
    visual_sources: Any,
    command_log: list[str],
) -> dict[str, dict[str, Any]]:
    """Validate resolved per-shot sources without changing legacy keyframe behavior."""

    expected_ids = {str(shot["shot_id"]) for shot in shots}
    if visual_sources is None:
        return {
            shot_id: {
                "shot_id": shot_id,
                "visual_source_type": "IMAGE",
                "selection_reason": "LEGACY_IMAGE_JOB_FALLBACK",
                "source_asset_id": keyframe.get("image_asset_id"),
                "source_image_job_id": None,
                "source_video_job_id": None,
                "source_provider": keyframe["provider_id"],
                "source_type": keyframe.get("source_type", _REAL_IMAGE_SOURCE_TYPE),
                "source_path": str(keyframe["image_path"]),
                "source_sha256": keyframe["image_sha256"],
                "source_duration_seconds": None,
                "source_has_audio": False,
            }
            for shot_id, keyframe in keyframes_by_shot.items()
        }
    if not isinstance(visual_sources, (list, tuple)):
        raise MediaToolError("Final Media visual_sources 必须是逐镜头数组")
    normalized: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(visual_sources, start=1):
        source = _trace_mapping(raw, label=f"visual_sources[{position - 1}]")
        shot_id = _required_trace_text(
            source, "shot_id", label=f"visual_sources[{position - 1}]"
        )
        if shot_id not in expected_ids or shot_id in normalized:
            raise MediaToolError(f"Final Media 视觉源 Shot 绑定无效：{shot_id}")
        source_kind = _required_trace_text(
            source, "visual_source_type", label=f"visual_sources {shot_id}"
        )
        if source_kind not in {"IMAGE", "VIDEO_SHOT"}:
            raise MediaToolError(f"Final Media 视觉源类型无效：{source_kind}")
        source_provider = _required_trace_text(
            source, "source_provider", label=f"visual_sources {shot_id}"
        )
        source_type = _required_trace_text(
            source, "source_type", label=f"visual_sources {shot_id}"
        )
        source_path = Path(
            _required_trace_text(
                source, "source_path", label=f"visual_sources {shot_id}"
            )
        ).resolve()
        expected_sha = _required_trace_text(
            source, "source_sha256", label=f"visual_sources {shot_id}"
        ).lower()
        if not source_path.is_file() or source_path.stat().st_size <= 0:
            raise MediaToolError(f"Final Media 视觉源不存在或为空：{source_path}")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise MediaToolError(f"Final Media 视觉源 SHA256 格式无效：{shot_id}")
        if sha256_file(source_path) != expected_sha:
            raise MediaToolError(f"Final Media 视觉源 SHA256 不符：{shot_id}")
        probe = ffprobe_json(tools, source_path, command_log=command_log)
        streams = probe.get("streams")
        videos = (
            [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
            if isinstance(streams, list)
            else []
        )
        if len(videos) != 1:
            raise MediaToolError(f"Final Media 视觉源必须有且只有一个视频流：{shot_id}")
        video = videos[0]
        codec = str(video.get("codec_name") or "")
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if width <= 0 or height <= 0:
            raise MediaToolError(f"Final Media 视觉源尺寸无效：{shot_id}")
        if source_kind == "IMAGE" and codec not in {"png", "mjpeg"}:
            raise MediaToolError(f"Final Media 图片源只接受 PNG/JPEG：{shot_id}")
        if source_kind == "VIDEO_SHOT" and not source.get("source_video_job_id"):
            raise MediaToolError(f"VIDEO_SHOT 缺少显式 source_video_job_id：{shot_id}")
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source_path,
                "-map",
                "0:v:0",
                "-an",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=240,
            command_log=command_log,
        )
        actual_duration = (
            float((probe.get("format") or {}).get("duration") or 0.0)
            if source_kind == "VIDEO_SHOT"
            else None
        )
        if source_kind == "VIDEO_SHOT" and (actual_duration or 0.0) <= 0:
            raise MediaToolError(f"VIDEO_SHOT 时长无效：{shot_id}")
        normalized[shot_id] = {
            **source,
            "shot_id": shot_id,
            "visual_source_type": source_kind,
            "source_provider": source_provider,
            "source_type": source_type,
            "source_path": str(source_path),
            "source_sha256": expected_sha,
            "source_width": width,
            "source_height": height,
            "source_codec": codec,
            "source_duration_seconds": actual_duration,
            "source_has_audio": any(
                isinstance(item, dict) and item.get("codec_type") == "audio"
                for item in streams
            ),
        }
    if set(normalized) != expected_ids:
        raise MediaToolError("Final Media visual_sources 未完整覆盖 ScriptV1")
    return normalized


def _trace_mapping(value: Any, *, label: str) -> dict[str, Any]:
    """把 Provider dataclass 或普通字典收敛为只读追溯字典。"""

    if isinstance(value, dict):
        return dict(value)
    serializer = getattr(value, "as_dict", None)
    if callable(serializer):
        serialized = serializer()
        if isinstance(serialized, dict):
            return dict(serialized)
    raise MediaToolError(f"{label} 必须是对象或提供 as_dict()")


def _required_trace_text(
    payload: dict[str, Any],
    field: str,
    *,
    label: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MediaToolError(f"{label} 缺少非空字段 {field}")
    return value.strip()


def _trace_number(
    payload: dict[str, Any],
    names: tuple[str, ...],
    *,
    label: str,
) -> float:
    for name in names:
        value = payload.get(name)
        if type(value) in (int, float):
            parsed = float(value)
            if math.isfinite(parsed):
                return parsed
    raise MediaToolError(f"{label} 缺少有限数值字段 {'/'.join(names)}")


def _probe_real_audio_asset(
    *,
    tools: MediaTools,
    root: Path,
    shot: dict[str, Any],
    original: Any,
    expected_provider_id: str,
    command_log: list[str],
) -> dict[str, Any]:
    """完整验证单镜真实 WAV；绝不接纳 Mock 或损坏的缓存音频。"""

    shot_id = str(shot["shot_id"])
    label = f"真实旁白 {shot_id}"
    asset = _trace_mapping(original, label=label)
    if _required_trace_text(asset, "shot_id", label=label) != shot_id:
        raise MediaToolError(f"{label} 的 shot_id 与剧本不一致")
    provider_id = _required_trace_text(asset, "provider_id", label=label)
    if provider_id != expected_provider_id or provider_id.lower() == "mock":
        raise MediaToolError(
            f"{label} Provider 不符：预期 {expected_provider_id}，实际 {provider_id}"
        )
    model_id = _required_trace_text(asset, "model_id", label=label)
    model_revision = _required_trace_text(asset, "model_revision", label=label)
    speaker = _required_trace_text(asset, "speaker", label=label)
    language = _required_trace_text(asset, "language", label=label)
    text = asset.get("text")
    if not isinstance(text, str) or text != str(shot["subtitle_text"]):
        raise MediaToolError(f"{label} 文本与 ScriptV1 旁白不完全一致")

    audio_value = asset.get("audio_path", asset.get("path"))
    if not isinstance(audio_value, (str, os.PathLike)) or not str(audio_value):
        raise MediaToolError(f"{label} 缺少 audio_path")
    audio_path = Path(audio_value).expanduser().resolve()
    if not audio_path.is_file() or audio_path.stat().st_size <= 44:
        raise MediaToolError(f"{label} WAV 不存在或为空：{audio_path}")
    if audio_path.suffix.lower() != ".wav":
        raise MediaToolError(f"{label} 必须是 WAV：{audio_path}")
    expected_sha256 = _required_trace_text(
        asset,
        "audio_sha256" if "audio_sha256" in asset else "sha256",
        label=label,
    ).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise MediaToolError(f"{label} audio_sha256 格式无效")
    actual_sha256 = sha256_file(audio_path)
    if actual_sha256 != expected_sha256:
        raise MediaToolError(
            f"{label} SHA-256 不符：记录 {expected_sha256}，实际 {actual_sha256}"
        )

    probe = ffprobe_json(tools, audio_path, command_log=command_log)
    streams = probe.get("streams")
    audio_streams = (
        [item for item in streams if item.get("codec_type") == "audio"]
        if isinstance(streams, list)
        else []
    )
    video_streams = (
        [item for item in streams if item.get("codec_type") == "video"]
        if isinstance(streams, list)
        else []
    )
    if len(audio_streams) != 1 or video_streams:
        raise MediaToolError(f"{label} 必须只包含一个音频流")
    audio_stream = audio_streams[0]
    if audio_stream.get("codec_name") != "pcm_s16le":
        raise MediaToolError(
            f"{label} 必须是 PCM16 WAV，实际 {audio_stream.get('codec_name')}"
        )
    try:
        sample_rate = int(audio_stream["sample_rate"])
        channels = int(audio_stream["channels"])
        actual_duration = float((probe.get("format") or {})["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaToolError(f"{label} ffprobe 元数据不完整") from exc
    if sample_rate <= 0 or channels <= 0 or actual_duration <= 0:
        raise MediaToolError(f"{label} 采样率、声道或时长无效")
    recorded_sample_rate = asset.get("sample_rate")
    recorded_channels = asset.get("channels")
    if recorded_sample_rate != sample_rate or recorded_channels != channels:
        raise MediaToolError(
            f"{label} 采样参数与追溯不一致："
            f"记录 {recorded_sample_rate}Hz/{recorded_channels}ch，"
            f"实际 {sample_rate}Hz/{channels}ch"
        )
    recorded_duration = _trace_number(
        asset,
        ("duration_seconds", "audio_duration_seconds", "audio_duration"),
        label=label,
    )
    sample_tolerance = max(1.0 / sample_rate, 0.000_1)
    if abs(recorded_duration - actual_duration) > sample_tolerance:
        raise MediaToolError(
            f"{label} 时长与追溯不一致：记录 {recorded_duration:.6f} 秒，"
            f"实际 {actual_duration:.6f} 秒"
        )
    run_command(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            audio_path,
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=120,
        command_log=command_log,
    )

    model_sha256 = _required_trace_text(asset, "model_sha256", label=label).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", model_sha256):
        raise MediaToolError(f"{label} model_sha256 格式无效")
    trace_value = asset.get("trace_path")
    trace_path: Path | None = None
    if trace_value not in (None, ""):
        if not isinstance(trace_value, (str, os.PathLike)):
            raise MediaToolError(f"{label} trace_path 格式无效")
        trace_path = Path(trace_value).expanduser().resolve()
        if not trace_path.is_file():
            raise MediaToolError(f"{label} trace_path 不存在：{trace_path}")
    warnings = asset.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise MediaToolError(f"{label} warnings 必须是字符串数组")

    return {
        **asset,
        "provider_id": provider_id,
        "model_id": model_id,
        "model_revision": model_revision,
        "shot_id": shot_id,
        "speaker": speaker,
        "language": language,
        "text": text,
        "audio_path": str(audio_path),
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_seconds": round(actual_duration, 6),
        "generation_seconds": _trace_number(
            asset, ("generation_seconds",), label=label
        ),
        "real_time_factor": _trace_number(
            asset, ("real_time_factor",), label=label
        ),
        "audio_sha256": actual_sha256,
        "model_sha256": model_sha256,
        "trace_path": str(trace_path) if trace_path is not None else None,
        "warnings": list(warnings),
        "reused": bool(asset.get("reused", False)),
        "source_type": _REAL_AUDIO_SOURCE_TYPE,
        "repo_relative_audio_path": _repo_relative(root, audio_path),
    }


def _normalize_real_audio_assets(
    *,
    tools: MediaTools,
    root: Path,
    shots: list[dict[str, Any]],
    audio_assets: list[Any] | tuple[Any, ...],
    provider_id: str,
    command_log: list[str],
) -> dict[str, dict[str, Any]]:
    if provider_id.lower() == "mock":
        raise MediaToolError("真实旁白媒体入口禁止 audio provider=mock")
    if len(audio_assets) != len(shots):
        raise MediaToolError(
            "真实旁白数量必须与剧本镜头数一致："
            f"剧本 {len(shots)}，旁白 {len(audio_assets)}"
        )
    raw_by_id: dict[str, Any] = {}
    for position, original in enumerate(audio_assets, start=1):
        payload = _trace_mapping(original, label=f"第 {position} 个真实旁白")
        shot_id = _required_trace_text(
            payload, "shot_id", label=f"第 {position} 个真实旁白"
        )
        if shot_id in raw_by_id:
            raise MediaToolError(f"真实旁白 shot_id 重复：{shot_id}")
        raw_by_id[shot_id] = original
    expected_ids = [str(shot["shot_id"]) for shot in shots]
    if set(raw_by_id) != set(expected_ids):
        raise MediaToolError(
            "真实旁白镜头集合与剧本不一致："
            f"预期 {expected_ids}，实际 {list(raw_by_id)}"
        )
    return {
        shot_id: _probe_real_audio_asset(
            tools=tools,
            root=root,
            shot=next(shot for shot in shots if str(shot["shot_id"]) == shot_id),
            original=raw_by_id[shot_id],
            expected_provider_id=provider_id,
            command_log=command_log,
        )
        for shot_id in expected_ids
    }


def _normalize_media_timing_plan(
    *,
    shots: list[dict[str, Any]],
    audio_by_shot: dict[str, dict[str, Any]],
    timing_plan: Any,
    fps: int,
    max_total_duration_seconds: float,
) -> tuple[list[dict[str, Any]], float, float]:
    if isinstance(timing_plan, (list, tuple)):
        raw_items = list(timing_plan)
    else:
        plan_payload = _trace_mapping(timing_plan, label="MediaTimingPlan")
        raw_items = plan_payload.get("shots", plan_payload.get("shot_timings"))
    if not isinstance(raw_items, (list, tuple)):
        raise MediaToolError("MediaTimingPlan 缺少 shots 数组")
    raw_items = list(raw_items)
    if len(raw_items) != len(shots):
        raise MediaToolError("MediaTimingPlan 镜头数与 ScriptV1 不一致")
    if not math.isfinite(max_total_duration_seconds) or max_total_duration_seconds <= 0:
        raise MediaToolError("最终渲染时长上限必须大于 0")

    by_id: dict[str, dict[str, Any]] = {}
    for position, original in enumerate(raw_items, start=1):
        item = _trace_mapping(original, label=f"MediaTimingPlan.shots[{position - 1}]")
        shot_id = _required_trace_text(
            item, "shot_id", label=f"MediaTimingPlan.shots[{position - 1}]"
        )
        if shot_id in by_id:
            raise MediaToolError(f"MediaTimingPlan shot_id 重复：{shot_id}")
        by_id[shot_id] = item

    expected_ids = [str(shot["shot_id"]) for shot in shots]
    if set(by_id) != set(expected_ids):
        raise MediaToolError("MediaTimingPlan 镜头集合与 ScriptV1 不一致")

    normalized: list[dict[str, Any]] = []
    source_total = 0.0
    rendered_total = 0.0
    for shot in shots:
        shot_id = str(shot["shot_id"])
        item = by_id[shot_id]
        label = f"MediaTimingPlan {shot_id}"
        source_duration = _trace_number(
            item,
            ("source_shot_duration", "source_duration_seconds"),
            label=label,
        )
        audio_duration = _trace_number(
            item,
            ("audio_duration", "audio_duration_seconds"),
            label=label,
        )
        lead_in = _trace_number(item, ("lead_in_seconds",), label=label)
        lead_out = _trace_number(item, ("lead_out_seconds",), label=label)
        rendered_duration = _trace_number(
            item,
            ("rendered_shot_duration", "rendered_duration_seconds"),
            label=label,
        )
        extended_by = _trace_number(
            item,
            ("extended_by_seconds", "extension_seconds"),
            label=label,
        )
        reason = item.get("extension_reason", item.get("reason"))
        if not isinstance(reason, str) or not reason.strip():
            raise MediaToolError(f"{label} 缺少 extension_reason")
        if abs(source_duration - float(shot["duration_seconds"])) > 1e-6:
            raise MediaToolError(f"{label} 源镜头时长与 ScriptV1 不一致")
        actual_audio_duration = float(audio_by_shot[shot_id]["duration_seconds"])
        sample_rate = int(audio_by_shot[shot_id]["sample_rate"])
        if abs(audio_duration - actual_audio_duration) > max(1 / sample_rate, 1e-4):
            raise MediaToolError(f"{label} 音频时长与 WAV 实测不一致")
        if abs(lead_in - _AUDIO_LEAD_IN_SECONDS) > 1e-6:
            raise MediaToolError(f"{label} lead_in_seconds 必须为 0.20")
        if abs(lead_out - _AUDIO_LEAD_OUT_SECONDS) > 1e-6:
            raise MediaToolError(f"{label} lead_out_seconds 必须为 0.35")
        raw_duration = max(source_duration, audio_duration + lead_in + lead_out)
        expected_frames = math.ceil(raw_duration * fps - 1e-9)
        expected_rendered = expected_frames / fps
        if abs(rendered_duration - expected_rendered) > 1e-6:
            raise MediaToolError(
                f"{label} 未按 {fps}fps 帧边界向上取整："
                f"预期 {expected_rendered:.6f}，实际 {rendered_duration:.6f}"
            )
        expected_extension = max(0.0, expected_rendered - source_duration)
        if abs(extended_by - expected_extension) > 1e-6:
            raise MediaToolError(f"{label} extended_by_seconds 与实算值不一致")
        normalized.append(
            {
                **item,
                "shot_id": shot_id,
                "source_shot_duration": round(source_duration, 6),
                "source_duration_seconds": round(source_duration, 6),
                "audio_duration": round(audio_duration, 6),
                "audio_duration_seconds": round(audio_duration, 6),
                "lead_in_seconds": round(lead_in, 6),
                "lead_out_seconds": round(lead_out, 6),
                "rendered_shot_duration": round(expected_rendered, 6),
                "rendered_duration_seconds": round(expected_rendered, 6),
                "extended_by_seconds": round(expected_extension, 6),
                "extension_seconds": round(expected_extension, 6),
                "extension_reason": reason.strip(),
            }
        )
        source_total += source_duration
        rendered_total += expected_rendered

    if not 20.0 <= source_total <= 40.0:
        raise MediaToolError(
            f"源 ScriptV1 总时长必须在 20—40 秒内，实际 {source_total:.3f} 秒"
        )
    if rendered_total > max_total_duration_seconds + 1e-6:
        raise MediaToolError(
            "AUDIO_TIMING_EXCEEDS_LIMIT："
            f"渲染总时长 {rendered_total:.3f} 秒超过上限 "
            f"{max_total_duration_seconds:.3f} 秒；请改用 Serena 或缩短旁白。"
        )
    return normalized, round(source_total, 6), round(rendered_total, 6)


def _resolve_background_audio(
    background_audio: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not background_audio or background_audio.get("enabled") is not True:
        return None
    payload = dict(background_audio)
    raw_path = payload.get("resolved_path") or payload.get("audio_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise MediaToolError("启用背景音时缺少已解析的受控文件路径")
    path = Path(raw_path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise MediaToolError("背景音文件不存在或为空")
    recorded_sha = payload.get("sha256")
    if not isinstance(recorded_sha, str) or sha256_file(path) != recorded_sha.lower():
        raise MediaToolError("背景音 SHA256 与 Job 快照不一致")
    try:
        volume = float(payload["volume"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaToolError("背景音快照缺少有效音量") from exc
    if not 0.02 <= volume <= 0.35:
        raise MediaToolError("背景音量必须在 0.02—0.35 之间")
    payload["resolved_path"] = str(path)
    payload["volume"] = round(volume, 3)
    return payload


def _mix_background_audio(
    *,
    tools: MediaTools,
    base_video: Path,
    output_path: Path,
    background_audio: dict[str, Any],
    duration: float,
    command_log: list[str],
) -> None:
    fade_in = min(0.6, max(0.1, duration / 5))
    fade_out = min(0.8, max(0.1, duration / 5))
    fade_out_start = max(0.0, duration - fade_out)
    volume = float(background_audio["volume"])
    filter_complex = (
        f"[1:a]aresample={SAMPLE_RATE},volume={volume:.3f},"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_start:.6f}:d={fade_out:.3f},"
        f"atrim=start=0:duration={duration:.6f},asetpts=PTS-STARTPTS[bg];"
        "[bg][0:a]sidechaincompress=threshold=0.020:ratio=8:"
        "attack=20:release=500[ducked];"
        "[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[aout]"
    )
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            base_video,
            "-stream_loop",
            "-1",
            "-i",
            Path(str(background_audio["resolved_path"])),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-t",
            f"{duration:.6f}",
            "-movflags",
            "+faststart",
            output_path,
        ],
        timeout_seconds=300,
        command_log=command_log,
    )


def _background_manifest(
    background_audio: dict[str, Any] | None,
    *,
    duration: float,
) -> dict[str, Any]:
    if background_audio is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "source_type": "USER_UPLOAD",
        "original_filename": background_audio.get("original_filename"),
        "mime_type": background_audio.get("mime_type"),
        "format": background_audio.get("format"),
        "duration_seconds": background_audio.get("duration_seconds"),
        "size_bytes": background_audio.get("size_bytes"),
        "sha256": background_audio.get("sha256"),
        "storage_path": background_audio.get("storage_path"),
        "volume": background_audio["volume"],
        "loop_and_trim_to_seconds": round(duration, 6),
        "fade_in_seconds": min(0.6, max(0.1, duration / 5)),
        "fade_out_seconds": min(0.8, max(0.1, duration / 5)),
        "ducking": {
            "method": "FFmpeg sidechaincompress",
            "threshold": 0.020,
            "ratio": 8,
            "attack_ms": 20,
            "release_ms": 500,
            "narration_is_uncompressed_sidechain": True,
        },
        "rights_notice": background_audio.get(
            "rights_notice",
            "来源为用户上传；平台不声明该音频的版权归属。",
        ),
    }


def _create_poster(
    *,
    tools: MediaTools,
    video_path: Path,
    output_dir: Path,
    first_shot_duration: float,
    command_log: list[str],
) -> dict[str, Any]:
    capture_at = min(0.75, max(0.4, first_shot_duration * 0.12))
    poster_path = output_dir / "poster.jpg"
    temporary = poster_path.with_name("poster.part.jpg")
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{capture_at:.6f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            (
                "scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,setsar=1"
            ),
            "-q:v",
            "2",
            temporary,
        ],
        timeout_seconds=120,
        command_log=command_log,
    )
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise MediaToolError("FFmpeg 未生成有效 poster")
    os.replace(temporary, poster_path)
    return {
        "path": poster_path,
        "sha256": sha256_file(poster_path),
        "width": 1280,
        "height": 720,
        "captured_at_seconds": round(capture_at, 6),
        "format": "jpeg",
    }


def _render_project_short(
    *,
    root: Path,
    project_id: str,
    project_title: str,
    shots: list[dict[str, Any]],
    output_dir: Path,
    output_filename: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    font_path: Path | None = None,
    provider_id: str = "mock",
    generation_context: dict[str, Any] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    keyframes: list[dict[str, Any]] | None = None,
    motion_preset: str | None = None,
    background_audio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用同一 FFmpeg 链路渲染 Mock 画面或已校验的真实关键帧。

    调用方应为每个 GenerationJob 提供独立 output_dir。函数只写该目录下的
    派生文件，使用临时文件完成最终 MP4 和 manifest 的原子替换。
    """

    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = output_dir / "shots"
    subtitle_dir = output_dir / "subtitles"
    shot_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    normalized_shots = _normalize_project_shots(
        shots,
        width=width,
        height=height,
        fps=fps,
        provider_id=provider_id,
    )
    tools = resolve_media_tools()
    font = (font_path or find_chinese_font()).resolve()
    if not font.is_file():
        raise MediaToolError(f"配置的中文字体不存在：{font}")

    requested_name = output_filename or f"{_safe_file_stem(project_id, 'mock_export')}.mp4"
    if Path(requested_name).name != requested_name or Path(requested_name).suffix.lower() != ".mp4":
        raise MediaToolError("output_filename 必须是当前目录下的 .mp4 文件名")

    command_log: list[str] = []
    keyframes_by_shot = (
        _validate_real_keyframes(
            tools=tools,
            shots=normalized_shots,
            keyframes=keyframes,
            command_log=command_log,
        )
        if keyframes is not None
        else {}
    )
    real_image_mode = keyframes is not None
    if motion_preset is not None and motion_preset not in MOTION_PRESETS:
        raise MediaToolError(f"未知 motion preset：{motion_preset}")
    resolved_background = _resolve_background_audio(background_audio)

    if real_image_mode:
        keyframe_provider = next(iter(keyframes_by_shot.values()))["provider_id"]
        if provider_id != keyframe_provider:
            raise MediaToolError(
                f"媒体入口 provider_id={provider_id} 与真实关键帧 "
                f"Provider={keyframe_provider} 不一致"
            )
        configured_providers = (generation_context or {}).get("providers", {})
        if isinstance(configured_providers, dict):
            configured_image_provider = configured_providers.get("image_provider")
            if (
                configured_image_provider is not None
                and str(configured_image_provider) != keyframe_provider
            ):
                raise MediaToolError(
                    "generation_context.providers.image_provider "
                    "与真实关键帧 Provider 不一致"
                )
        for shot in normalized_shots:
            shot_provider = str(shot.get("provider_id", "")).strip()
            visual_provider = str(
                shot.get("generation_parameters", {}).get("visual_provider_id", "")
            ).strip()
            for label, value in (
                ("provider_id", shot_provider),
                ("generation_parameters.visual_provider_id", visual_provider),
            ):
                if value and value != keyframe_provider:
                    raise MediaToolError(
                        f"真实图片镜头 {shot['shot_id']} 的 {label}={value} "
                        f"与关键帧 Provider={keyframe_provider} 不一致"
                    )
            shot["provider_id"] = keyframe_provider
            shot["source_type"] = _REAL_IMAGE_SOURCE_TYPE
            shot["generation_parameters"]["visual_provider_id"] = keyframe_provider
            shot["generation_parameters"]["image_source_type"] = (
                _REAL_IMAGE_SOURCE_TYPE
            )
    shot_outputs: list[dict[str, Any]] = []
    for index, shot in enumerate(normalized_shots, start=1):
        shot_stem = _safe_file_stem(str(shot["shot_id"]), f"shot_{index:02d}")
        subtitle = prepare_burned_subtitle(
            narration=str(shot["subtitle_text"]),
            text_path=subtitle_dir / f"{shot_stem}.txt",
            width=width,
            height=height,
            font_path=font,
        )
        create_arguments = {
            "tools": tools,
            "font": font,
            "subtitle": subtitle,
            "shot": shot,
            "output_path": shot_dir / f"{shot_stem}.mp4",
            "command_log": command_log,
            "width": width,
            "height": height,
            "fps": fps,
            "motion_preset": motion_preset,
        }
        if real_image_mode:
            generated = _create_image_shot(
                **create_arguments,
                keyframe=keyframes_by_shot[str(shot["shot_id"])],
            )
        else:
            generated = _create_shot(**create_arguments)
        shot_outputs.append(generated)
        if progress_callback:
            progress_callback(10 + index * 15)

    concat_path = output_dir / "shots.ffconcat"
    concat_lines = ["ffconcat version 1.0"]
    for generated in shot_outputs:
        path_text = generated["video_path"].resolve().as_posix().replace("'", r"'\''")
        concat_lines.append(f"file '{path_text}'")
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    output_path = output_dir / requested_name
    temporary = _atomic_media_target(output_path)
    base_temporary = (
        output_dir / f"{output_path.stem}.base.part.mp4"
        if resolved_background is not None
        else temporary
    )
    base_temporary.unlink(missing_ok=True)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            base_temporary,
        ],
        timeout_seconds=180,
        command_log=command_log,
    )
    planned_duration = sum(
        float(shot["duration_seconds"]) for shot in normalized_shots
    )
    if resolved_background is not None:
        try:
            _mix_background_audio(
                tools=tools,
                base_video=base_temporary,
                output_path=temporary,
                background_audio=resolved_background,
                duration=planned_duration,
                command_log=command_log,
            )
        finally:
            base_temporary.unlink(missing_ok=True)
    validation = verify_media(
        tools,
        temporary,
        planned_duration_seconds=planned_duration,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    poster = _create_poster(
        tools=tools,
        video_path=output_path,
        output_dir=output_dir,
        first_shot_duration=float(normalized_shots[0]["duration_seconds"]),
        command_log=command_log,
    )
    if progress_callback:
        progress_callback(90)

    subtitle_sidecar = output_dir / "subtitles.srt"
    _write_srt(normalized_shots, subtitle_sidecar)
    digest = sha256_file(output_path)
    shot_manifest: list[dict[str, Any]] = []
    for shot, generated in zip(normalized_shots, shot_outputs, strict=True):
        item = {
            "shot_id": shot["shot_id"],
            "sequence_no": shot["sequence_no"],
            "title": shot["title"],
            "visual_description": shot["visual_description"],
            "duration_seconds": shot["duration_seconds"],
            "subtitle": shot["subtitle_text"],
            "narration": shot["subtitle_text"],
            "provider_id": shot["provider_id"],
            "script_provider_id": shot.get("script_provider_id", provider_id),
            "source_type": shot["source_type"],
            "generation_parameters": shot["generation_parameters"],
            "motion_preset": motion_preset or "legacy_shot_motion",
            "subtitle_file": _repo_relative(root, generated["subtitle_path"]),
            "subtitle_text_path": _repo_relative(root, generated["subtitle_path"]),
            "rendered_subtitle_text": generated["rendered_subtitle_text"],
            "font_path": str(generated["subtitle_font_path"]),
            "subtitle_rendering": generated["subtitle_rendering"],
            "subtitle_filter": generated["subtitle_filter"],
            "clip_path": _repo_relative(root, generated["video_path"]),
            "audio_path": _repo_relative(root, generated["audio_path"]),
            "audio_sha256": sha256_file(generated["audio_path"]),
            "clip_sha256": generated["sha256"],
            "clip_validation": generated["validation"],
        }
        if real_image_mode:
            keyframe = dict(generated["keyframe"])
            keyframe["image_path"] = _repo_relative(
                root, Path(str(keyframe["image_path"]))
            )
            for trace_field in ("workflow_path", "trace_path"):
                trace_value = keyframe.get(trace_field)
                if isinstance(trace_value, str) and trace_value:
                    keyframe[trace_field] = _repo_relative(root, Path(trace_value))
            item["keyframe"] = keyframe
            item["keyframe_path"] = keyframe["image_path"]
            item["keyframe_sha256"] = keyframe["image_sha256"]
        shot_manifest.append(item)

    context = dict(generation_context or {})
    provider_trace = context.get("providers", {})
    if not isinstance(provider_trace, dict):
        provider_trace = {}
    else:
        provider_trace = dict(provider_trace)
    script_provider = str(provider_trace.get("script_provider", provider_id))
    detected_image_provider = (
        str(next(iter(keyframes_by_shot.values()))["provider_id"])
        if real_image_mode
        else "mock"
    )
    context_image_provider = provider_trace.get("image_provider")
    if (
        real_image_mode
        and context_image_provider is not None
        and str(context_image_provider) != detected_image_provider
    ):
        raise MediaToolError(
            "generation_context.providers.image_provider 与真实关键帧 Provider 不一致"
        )
    image_provider = str(context_image_provider or detected_image_provider)
    audio_provider = str(provider_trace.get("audio_provider", "mock"))
    video_source_type = str(
        provider_trace.get(
            "video_source_type",
            "FFMPEG_KEYFRAME_MOTION" if real_image_mode else "DETERMINISTIC_FALLBACK",
        )
    )
    provider_trace.update(
        {
            "image_provider": image_provider,
            "audio_provider": str(provider_trace.get("audio_provider", "mock")),
            "video_source_type": video_source_type,
        }
    )
    context["providers"] = provider_trace
    script_validation_warnings = context.get(
        "script_validation_warnings",
        {"unused_scene_ids": [], "unused_character_ids": []},
    )
    if not isinstance(script_validation_warnings, dict):
        raise MediaToolError("generation_context.script_validation_warnings 必须是对象")
    manifest = {
        "manifest_version": (
            "m4.real-image-export.v1"
            if real_image_mode
            else "m3.mixed-provider-export.v1"
        ),
        "project": {"id": project_id, "title": project_title},
        "generation_context": context,
        "script_provider": script_provider,
        "image_provider": image_provider,
        "audio_provider": audio_provider,
        "video_source_type": video_source_type,
        "motion_preset": motion_preset or "legacy_shot_motion",
        "background_audio": _background_manifest(
            resolved_background, duration=planned_duration
        ),
        "script_validation_warnings": script_validation_warnings,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {**runtime_summary(tools), "operating_system": platform.platform()},
        "media_spec": {
            "resolution": f"{width}x{height}",
            "frame_rate": fps,
            "planned_duration_seconds": planned_duration,
            "encoded_duration_seconds": validation["encoded_duration_seconds"],
            "actual_duration_seconds": validation["encoded_duration_seconds"],
            "duration_delta_seconds": validation["duration_delta_seconds"],
            "duration_tolerance_seconds": validation[
                "duration_tolerance_seconds"
            ],
            "duration_validation": validation["duration_validation"],
        },
        "pipeline": {
            "provider_id": provider_id,
            "source_type": (
                "REAL_IMAGE_KEYFRAME_FFMPEG_MOTION"
                if real_image_mode
                else "DETERMINISTIC_FALLBACK"
            ),
            "script_provider": script_provider,
            "image_provider": image_provider,
            "audio_provider": audio_provider,
            "video_source_type": video_source_type,
            "visual_method": (
                "validated real PNG keyframes -> FFmpeg structured motion/fade"
                if real_image_mode
                else "FFmpeg mock composition/structured motion/fade filters"
            ),
            "motion_preset": motion_preset or "legacy_shot_motion",
            "audio_method": (
                "mock PCM WAV + user-upload background ducking -> FFmpeg AAC"
                if resolved_background is not None
                else "Python standard-library deterministic PCM WAV -> FFmpeg AAC"
            ),
            "subtitle_method": "FFmpeg drawtext + independent UTF-8 textfile",
            "subtitle_rendering": "burned_in",
            "chinese_font_path": str(font),
            "network_required": False,
            "api_key_required": False,
            "model_weights_required": real_image_mode,
        },
        "shot_count": len(shot_manifest),
        "shots": shot_manifest,
        "output": {
            "file_path": _repo_relative(root, output_path),
            "subtitle_sidecar_path": _repo_relative(root, subtitle_sidecar),
            "file_size_bytes": output_path.stat().st_size,
            "sha256": digest,
            "poster_path": _repo_relative(root, poster["path"]),
            "poster_sha256": poster["sha256"],
            "poster_width": poster["width"],
            "poster_height": poster["height"],
            "poster_captured_at_seconds": poster["captured_at_seconds"],
        },
        "ffprobe_validation": validation,
        "safe_command_log": command_log,
    }
    manifest_path = output_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    if progress_callback:
        progress_callback(95)
    return {
        "status": "PASS",
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "subtitle_path": str(subtitle_sidecar),
        "font_path": str(font),
        "sha256": digest,
        "validation": validation,
        "shots": shot_outputs,
        "manifest": manifest,
        "poster_path": str(poster["path"]),
    }


def render_mock_project_short(
    *,
    root: Path,
    project_id: str,
    project_title: str,
    shots: list[dict[str, Any]],
    output_dir: Path,
    output_filename: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    font_path: Path | None = None,
    provider_id: str = "mock",
    generation_context: dict[str, Any] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    motion_preset: str | None = None,
    background_audio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保持 M0—M3 行为不变的确定性 Mock 媒体入口。"""

    return _render_project_short(
        root=root,
        project_id=project_id,
        project_title=project_title,
        shots=shots,
        output_dir=output_dir,
        output_filename=output_filename,
        width=width,
        height=height,
        fps=fps,
        font_path=font_path,
        provider_id=provider_id,
        generation_context=generation_context,
        progress_callback=progress_callback,
        keyframes=None,
        motion_preset=motion_preset,
        background_audio=background_audio,
    )


def render_image_project_short(
    *,
    root: Path,
    project_id: str,
    project_title: str,
    shots: list[dict[str, Any]],
    keyframes: list[dict[str, Any]],
    output_dir: Path,
    output_filename: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    font_path: Path | None = None,
    provider_id: str = "comfyui-animagine-xl-4",
    generation_context: dict[str, Any] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    motion_preset: str | None = None,
    background_audio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用完整的一组真实 PNG 关键帧生成 MP4；禁止缺图或混用 Mock。"""

    return _render_project_short(
        root=root,
        project_id=project_id,
        project_title=project_title,
        shots=shots,
        output_dir=output_dir,
        output_filename=output_filename,
        width=width,
        height=height,
        fps=fps,
        font_path=font_path,
        provider_id=provider_id,
        generation_context=generation_context,
        progress_callback=progress_callback,
        keyframes=keyframes,
        motion_preset=motion_preset,
        background_audio=background_audio,
    )


def _create_real_audio_image_shot(
    *,
    tools: MediaTools,
    font: Path,
    subtitle: BurnedSubtitle,
    shot: dict[str, Any],
    keyframe: dict[str, Any],
    audio: dict[str, Any],
    timing: dict[str, Any],
    output_path: Path,
    command_log: list[str],
    width: int,
    height: int,
    fps: int,
    motion_preset: str | None = None,
    visual_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把真实 PNG 与完整真实旁白合成为单镜头，不变速也不截断旁白。"""

    rendered_duration = float(timing["rendered_shot_duration"])
    audio_duration = float(timing["audio_duration"])
    lead_in = float(timing["lead_in_seconds"])
    lead_out = float(timing["lead_out_seconds"])
    if lead_in + audio_duration + lead_out > rendered_duration + 1e-6:
        raise MediaToolError(
            f"镜头 {shot['shot_id']} 的渲染时长不足以容纳完整旁白与前后留白"
        )
    frame_count = int(round(rendered_duration * fps))
    if frame_count <= 0 or abs(frame_count / fps - rendered_duration) > 1e-6:
        raise MediaToolError(f"镜头 {shot['shot_id']} 渲染时长未对齐视频帧")

    parameters = shot["generation_parameters"]
    canvas_width = width + 64
    canvas_height = height + 36
    subtitle_end = lead_in + audio_duration
    timed_subtitle_filter = (
        subtitle.filter_expression
        + f":enable='between(t,{lead_in:.6f},{subtitle_end:.6f})'"
    )
    filters = [
        (
            f"scale={canvas_width}:{canvas_height}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={canvas_width}:{canvas_height}",
        "setsar=1",
        _motion_filter(
            str(parameters["motion"]),
            frame_count,
            width=width,
            height=height,
            fps=fps,
            motion_preset=motion_preset,
        ),
        timed_subtitle_filter,
        (
            f"fade=t=in:st=0:d=0.35,"
            f"fade=t=out:st={max(0.0, rendered_duration - 0.35):.6f}:d=0.35"
        ),
        "format=yuv420p",
    ]
    # 先延迟真实旁白，再只补静音到目标时长。atrim 只会移除 apad 产生的
    # 多余静音；TimingPlan 已保证旁白末尾之后仍有 0.35 秒，不会截断语音。
    audio_filter = (
        f"adelay={int(round(lead_in * 1000))}:all=1,"
        f"apad=whole_dur={rendered_duration:.6f},"
        f"atrim=start=0:duration={rendered_duration:.6f},"
        "asetpts=N/SR/TB,aresample=48000"
    )

    temporary = _atomic_media_target(output_path)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            Path(str(keyframe["image_path"])),
            "-i",
            Path(str(audio["audio_path"])),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            ",".join(filters),
            "-af",
            audio_filter,
            "-t",
            f"{rendered_duration:.6f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            temporary,
        ],
        timeout_seconds=300,
        command_log=command_log,
    )
    validation = verify_media(
        tools,
        temporary,
        expected_duration_seconds=rendered_duration,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    return {
        "video_path": output_path,
        "validation": validation,
        "sha256": sha256_file(output_path),
        "narration": subtitle.narration,
        "rendered_subtitle_text": subtitle.rendered_text,
        "subtitle_path": subtitle.text_path,
        "subtitle_filter": timed_subtitle_filter,
        "subtitle_font_path": subtitle.font_path,
        "subtitle_rendering": "burned_in",
        "subtitle_start_seconds": round(lead_in, 6),
        "subtitle_end_seconds": round(subtitle_end, 6),
        "keyframe": keyframe,
        "visual_source": visual_source,
        "audio": audio,
        "timing": timing,
    }


def _create_real_audio_video_shot(
    *,
    tools: MediaTools,
    subtitle: BurnedSubtitle,
    shot: dict[str, Any],
    visual_source: dict[str, Any],
    audio: dict[str, Any],
    timing: dict[str, Any],
    output_path: Path,
    command_log: list[str],
    width: int,
    height: int,
    fps: int,
) -> dict[str, Any]:
    """Normalize one VIDEO_SHOT and combine only its video track with project audio."""

    rendered_duration = float(timing["rendered_shot_duration"])
    audio_duration = float(timing["audio_duration"])
    lead_in = float(timing["lead_in_seconds"])
    lead_out = float(timing["lead_out_seconds"])
    if lead_in + audio_duration + lead_out > rendered_duration + 1e-6:
        raise MediaToolError(
            f"镜头 {shot['shot_id']} 的渲染时长不足以容纳完整旁白与前后留白"
        )
    frame_count = int(round(rendered_duration * fps))
    if frame_count <= 0 or abs(frame_count / fps - rendered_duration) > 1e-6:
        raise MediaToolError(f"镜头 {shot['shot_id']} 渲染时长未对齐视频帧")

    subtitle_end = lead_in + audio_duration
    timed_subtitle_filter = (
        subtitle.filter_expression
        + f":enable='between(t,{lead_in:.6f},{subtitle_end:.6f})'"
    )
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
        f"fps={fps}",
        f"tpad=stop_mode=clone:stop_duration={rendered_duration:.6f}",
        f"trim=start=0:duration={rendered_duration:.6f}",
        "setpts=PTS-STARTPTS",
        timed_subtitle_filter,
        (
            f"fade=t=in:st=0:d=0.35,"
            f"fade=t=out:st={max(0.0, rendered_duration - 0.35):.6f}:d=0.35"
        ),
        "format=yuv420p",
    ]
    audio_filter = (
        f"adelay={int(round(lead_in * 1000))}:all=1,"
        f"apad=whole_dur={rendered_duration:.6f},"
        f"atrim=start=0:duration={rendered_duration:.6f},"
        "asetpts=N/SR/TB,aresample=48000"
    )
    temporary = _atomic_media_target(output_path)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            Path(str(visual_source["source_path"])),
            "-i",
            Path(str(audio["audio_path"])),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            ",".join(filters),
            "-af",
            audio_filter,
            "-t",
            f"{rendered_duration:.6f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            temporary,
        ],
        timeout_seconds=300,
        command_log=command_log,
    )
    validation = verify_media(
        tools,
        temporary,
        expected_duration_seconds=rendered_duration,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    source_duration = float(visual_source["source_duration_seconds"])
    frame_tolerance = 1.0 / fps
    if source_duration < rendered_duration - frame_tolerance:
        duration_normalization = "PLAY_THEN_FREEZE_LAST_FRAME"
    elif source_duration > rendered_duration + frame_tolerance:
        duration_normalization = "TRIM_TO_TARGET"
    else:
        duration_normalization = "MATCH_TARGET"
    return {
        "video_path": output_path,
        "validation": validation,
        "sha256": sha256_file(output_path),
        "narration": subtitle.narration,
        "rendered_subtitle_text": subtitle.rendered_text,
        "subtitle_path": subtitle.text_path,
        "subtitle_filter": timed_subtitle_filter,
        "subtitle_font_path": subtitle.font_path,
        "subtitle_rendering": "burned_in",
        "subtitle_start_seconds": round(lead_in, 6),
        "subtitle_end_seconds": round(subtitle_end, 6),
        "keyframe": None,
        "visual_source": {
            **visual_source,
            "source_duration_seconds": source_duration,
            "target_duration_seconds": rendered_duration,
            "duration_normalization": duration_normalization,
            "source_audio_ignored": True,
        },
        "audio": audio,
        "timing": timing,
    }


def _write_real_audio_srt(
    timings: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    target: Path,
) -> None:
    def stamp(value: float) -> str:
        milliseconds = int(round(value * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    lines: list[str] = []
    shot_start = 0.0
    for index, (timing, shot) in enumerate(zip(timings, shots, strict=True), start=1):
        subtitle_start = shot_start + float(timing["lead_in_seconds"])
        subtitle_end = subtitle_start + float(timing["audio_duration"])
        lines.extend(
            [
                str(index),
                f"{stamp(subtitle_start)} --> {stamp(subtitle_end)}",
                str(shot["subtitle_text"]),
                "",
            ]
        )
        shot_start += float(timing["rendered_shot_duration"])
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def render_real_audio_project_short(
    *,
    root: Path,
    project_id: str,
    project_title: str,
    shots: list[dict[str, Any]],
    keyframes: list[Any] | tuple[Any, ...],
    audio_assets: list[Any] | tuple[Any, ...],
    timing_plan: Any,
    visual_sources: Any = None,
    output_dir: Path,
    output_filename: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    font_path: Path | None = None,
    provider_id: str = "qwen3-tts-0.6b-customvoice",
    generation_context: dict[str, Any] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    max_total_duration_seconds: float = _DEFAULT_RENDERED_DURATION_LIMIT_SECONDS,
    timing_plan_path: Path | None = None,
    motion_preset: str | None = None,
    background_audio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用复用真实 PNG、逐镜真实中文 WAV 与 FFmpeg 生成 M5-B 成片。"""

    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = output_dir / "shots"
    subtitle_dir = output_dir / "subtitles"
    shot_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    resolved_timing_plan_path = (
        Path(timing_plan_path).resolve()
        if timing_plan_path is not None
        else None
    )
    if motion_preset is not None and motion_preset not in MOTION_PRESETS:
        raise MediaToolError(f"未知 motion preset：{motion_preset}")
    resolved_background = _resolve_background_audio(background_audio)
    if (
        resolved_timing_plan_path is not None
        and not resolved_timing_plan_path.is_file()
    ):
        raise MediaToolError(f"timing_plan_path 不存在：{resolved_timing_plan_path}")
    if not 3 <= len(shots) <= 5:
        raise MediaToolError(f"真实旁白短片必须包含 3—5 个镜头，实际 {len(shots)}")
    if not keyframes and visual_sources is None:
        raise MediaToolError("真实旁白短片缺少复用的真实 PNG")

    normalized_keyframes = [
        _trace_mapping(item, label=f"第 {index} 个真实关键帧")
        for index, item in enumerate(keyframes, start=1)
    ]
    image_provider_id = (
        str(normalized_keyframes[0].get("provider_id") or "selected-visuals")
        if normalized_keyframes
        else "selected-visuals"
    )
    normalized_shots = _normalize_project_shots(
        shots,
        width=width,
        height=height,
        fps=fps,
        provider_id=image_provider_id,
    )
    tools = resolve_media_tools()
    font = (font_path or find_chinese_font()).resolve()
    if not font.is_file():
        raise MediaToolError(f"配置的中文字体不存在：{font}")
    requested_name = output_filename or f"{_safe_file_stem(project_id, 'm5_export')}.mp4"
    if (
        Path(requested_name).name != requested_name
        or Path(requested_name).suffix.lower() != ".mp4"
    ):
        raise MediaToolError("output_filename 必须是当前目录下的 .mp4 文件名")

    command_log: list[str] = []
    if visual_sources is None:
        keyframes_by_shot = _validate_real_keyframes(
            tools=tools,
            shots=normalized_shots,
            keyframes=normalized_keyframes,
            command_log=command_log,
        )
        visual_sources_by_shot = _normalize_final_visual_sources(
            tools=tools,
            shots=normalized_shots,
            keyframes_by_shot=keyframes_by_shot,
            visual_sources=None,
            command_log=command_log,
        )
    else:
        # 先确定最终逐镜头视觉源。VIDEO_SHOT 的 source image 只属于血缘，
        # 不得再被套用本地 ImageProvider 的 model_id/seed 合同。
        visual_sources_by_shot = _normalize_final_visual_sources(
            tools=tools,
            shots=normalized_shots,
            keyframes_by_shot={},
            visual_sources=visual_sources,
            command_log=command_log,
        )
        image_shot_ids = {
            shot_id
            for shot_id, source in visual_sources_by_shot.items()
            if source["visual_source_type"] == "IMAGE"
        }
        keyframes_by_shot = _validate_real_keyframes(
            tools=tools,
            shots=normalized_shots,
            keyframes=normalized_keyframes,
            command_log=command_log,
            required_shot_ids=image_shot_ids,
        )
    actual_image_provider_ids = {
        str(source["source_provider"])
        for source in visual_sources_by_shot.values()
        if source["visual_source_type"] == "IMAGE"
    }
    actual_image_source_types = {
        str(source["source_type"])
        for source in visual_sources_by_shot.values()
        if source["visual_source_type"] == "IMAGE"
    }
    if any(value.lower() == "mock" for value in actual_image_provider_ids):
        raise MediaToolError("真实旁白短片禁止复用 Mock 图片")
    actual_image_provider_id = (
        next(iter(actual_image_provider_ids))
        if len(actual_image_provider_ids) == 1
        else None
    )
    actual_image_source_type = (
        next(iter(actual_image_source_types))
        if len(actual_image_source_types) == 1
        else ("MIXED_SELECTED_IMAGE_ASSETS" if actual_image_source_types else None)
    )
    for shot in normalized_shots:
        source = visual_sources_by_shot[str(shot["shot_id"])]
        shot["provider_id"] = source["source_provider"]
        shot["source_type"] = source["source_type"]
        shot["generation_parameters"]["visual_provider_id"] = (
            source["source_provider"]
        )
        shot["generation_parameters"]["image_source_type"] = (
            source["source_type"]
        )
        shot["generation_parameters"]["visual_source_type"] = source[
            "visual_source_type"
        ]
        shot["generation_parameters"]["audio_provider_id"] = provider_id
        shot["generation_parameters"]["audio_source_type"] = (
            _REAL_AUDIO_SOURCE_TYPE
        )

    audio_by_shot = _normalize_real_audio_assets(
        tools=tools,
        root=root,
        shots=normalized_shots,
        audio_assets=audio_assets,
        provider_id=provider_id,
        command_log=command_log,
    )
    speaker_values = {str(item["speaker"]) for item in audio_by_shot.values()}
    language_values = {str(item["language"]) for item in audio_by_shot.values()}
    model_values = {str(item["model_id"]) for item in audio_by_shot.values()}
    revision_values = {
        str(item["model_revision"]) for item in audio_by_shot.values()
    }
    model_sha256_values = {
        str(item["model_sha256"]) for item in audio_by_shot.values()
    }
    if any(
        len(values) != 1
        for values in (
            speaker_values,
            language_values,
            model_values,
            revision_values,
            model_sha256_values,
        )
    ):
        raise MediaToolError(
            "同一真实旁白 Job 不得混用音色、语言、模型、revision 或模型哈希"
        )
    if not speaker_values <= {"Serena", "Vivian"}:
        raise MediaToolError("M5-B 真实旁白音色只允许 Serena 或 Vivian")
    if language_values != {"Chinese"}:
        raise MediaToolError("M5-B 真实旁白 language 必须为 Chinese")
    normalized_timings, source_total, rendered_total = (
        _normalize_media_timing_plan(
            shots=normalized_shots,
            audio_by_shot=audio_by_shot,
            timing_plan=timing_plan,
            fps=fps,
            max_total_duration_seconds=float(max_total_duration_seconds),
        )
    )

    shot_outputs: list[dict[str, Any]] = []
    for index, (shot, timing) in enumerate(
        zip(normalized_shots, normalized_timings, strict=True),
        start=1,
    ):
        shot_id = str(shot["shot_id"])
        shot_stem = f"shot-{index:02d}"
        subtitle = prepare_burned_subtitle(
            narration=str(shot["subtitle_text"]),
            text_path=subtitle_dir / f"{shot_stem}.txt",
            width=width,
            height=height,
            font_path=font,
        )
        visual_source = visual_sources_by_shot[shot_id]
        if visual_source["visual_source_type"] == "VIDEO_SHOT":
            generated = _create_real_audio_video_shot(
                tools=tools,
                subtitle=subtitle,
                shot=shot,
                visual_source=visual_source,
                audio=audio_by_shot[shot_id],
                timing=timing,
                output_path=shot_dir / f"{shot_stem}.mp4",
                command_log=command_log,
                width=width,
                height=height,
                fps=fps,
            )
        else:
            keyframe = dict(keyframes_by_shot[shot_id])
            if visual_source.get("selection_reason") == "EXPLICIT_IMAGE_ASSET":
                keyframe = {
                    "shot_id": shot_id,
                    "provider_id": visual_source["source_provider"],
                    "source_type": visual_source["source_type"],
                    "image_path": visual_source["source_path"],
                    "image_sha256": visual_source["source_sha256"],
                    "width": visual_source.get("source_width"),
                    "height": visual_source.get("source_height"),
                    "image_asset_id": visual_source.get("source_asset_id"),
                }
            generated = _create_real_audio_image_shot(
                tools=tools,
                font=font,
                subtitle=subtitle,
                shot=shot,
                keyframe=keyframe,
                visual_source=visual_source,
                audio=audio_by_shot[shot_id],
                timing=timing,
                output_path=shot_dir / f"{shot_stem}.mp4",
                command_log=command_log,
                width=width,
                height=height,
                fps=fps,
                motion_preset=motion_preset,
            )
        shot_outputs.append(generated)
        if progress_callback:
            progress_callback(65 + int(index * 25 / len(normalized_shots)))

    concat_path = output_dir / "shots.ffconcat"
    concat_lines = ["ffconcat version 1.0"]
    for generated in shot_outputs:
        path_text = generated["video_path"].resolve().as_posix().replace("'", r"'\''")
        concat_lines.append(f"file '{path_text}'")
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    output_path = output_dir / requested_name
    temporary = _atomic_media_target(output_path)
    base_temporary = (
        output_dir / f"{output_path.stem}.base.part.mp4"
        if resolved_background is not None
        else temporary
    )
    base_temporary.unlink(missing_ok=True)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            base_temporary,
        ],
        timeout_seconds=240,
        command_log=command_log,
    )
    if resolved_background is not None:
        try:
            _mix_background_audio(
                tools=tools,
                base_video=base_temporary,
                output_path=temporary,
                background_audio=resolved_background,
                duration=rendered_total,
                command_log=command_log,
            )
        finally:
            base_temporary.unlink(missing_ok=True)
    validation = verify_media(
        tools,
        temporary,
        expected_duration_seconds=rendered_total,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    render_warnings: list[dict[str, str]] = []
    try:
        poster = _create_poster(
            tools=tools,
            video_path=output_path,
            output_dir=output_dir,
            first_shot_duration=float(
                normalized_timings[0]["rendered_shot_duration"]
            ),
            command_log=command_log,
        )
    except (MediaToolError, OSError) as exc:
        poster = None
        render_warnings.append(
            {
                "code": "POSTER_GENERATION_FAILED",
                "summary": f"成片已成功，但封面生成失败：{str(exc)[:300]}",
            }
        )
    if progress_callback:
        progress_callback(94)

    subtitle_sidecar = output_dir / "subtitles.srt"
    _write_real_audio_srt(normalized_timings, normalized_shots, subtitle_sidecar)
    digest = sha256_file(output_path)

    context = dict(generation_context or {})
    media_only = bool(context.get("media_only"))
    video_shot_count = sum(
        source["visual_source_type"] == "VIDEO_SHOT"
        for source in visual_sources_by_shot.values()
    )
    image_shot_count = len(normalized_shots) - video_shot_count
    explicit_image_shot_count = sum(
        source.get("selection_reason") == "EXPLICIT_IMAGE_ASSET"
        for source in visual_sources_by_shot.values()
    )
    media_video_source_type = (
        "VIDEO_SHOT_WITH_IMAGE_FALLBACK"
        if video_shot_count
        else (
            "MEDIA_ONLY_RERENDER_FFMPEG"
            if media_only
            else "REAL_IMAGE_REAL_TTS_FFMPEG_MOTION"
        )
    )
    shot_manifest: list[dict[str, Any]] = []
    for shot, generated in zip(normalized_shots, shot_outputs, strict=True):
        raw_keyframe = generated.get("keyframe")
        keyframe = dict(raw_keyframe) if isinstance(raw_keyframe, dict) else None
        if keyframe is not None:
            keyframe["image_path"] = _repo_relative(
                root, Path(str(keyframe["image_path"]))
            )
            for trace_field in ("workflow_path", "trace_path"):
                trace_value = keyframe.get(trace_field)
                if isinstance(trace_value, str) and trace_value:
                    keyframe[trace_field] = _repo_relative(root, Path(trace_value))
        visual_source = dict(generated["visual_source"])
        visual_source["source_path"] = _repo_relative(
            root, Path(str(visual_source["source_path"]))
        )
        audio = generated["audio"]
        timing = generated["timing"]
        audio_trace_path = audio.get("trace_path")
        shot_manifest.append(
            {
                "shot_id": shot["shot_id"],
                "sequence_no": shot["sequence_no"],
                "title": shot["title"],
                "visual_description": shot["visual_description"],
                "duration_seconds": timing["rendered_shot_duration"],
                "source_shot_duration": timing["source_shot_duration"],
                "source_duration_seconds": timing["source_duration_seconds"],
                "rendered_shot_duration": timing["rendered_shot_duration"],
                "rendered_duration_seconds": timing["rendered_duration_seconds"],
                "extended_by_seconds": timing["extended_by_seconds"],
                "extension_seconds": timing["extension_seconds"],
                "extension_reason": timing["extension_reason"],
                "motion_preset": motion_preset or "legacy_shot_motion",
                "lead_in_seconds": timing["lead_in_seconds"],
                "lead_out_seconds": timing["lead_out_seconds"],
                "subtitle": shot["subtitle_text"],
                "narration": shot["subtitle_text"],
                "subtitle_file": _repo_relative(root, generated["subtitle_path"]),
                "subtitle_text_path": _repo_relative(
                    root, generated["subtitle_path"]
                ),
                "rendered_subtitle_text": generated["rendered_subtitle_text"],
                "subtitle_start_seconds": generated["subtitle_start_seconds"],
                "subtitle_end_seconds": generated["subtitle_end_seconds"],
                "font_path": str(generated["subtitle_font_path"]),
                "subtitle_rendering": "burned_in",
                "subtitle_filter": generated["subtitle_filter"],
                "clip_path": _repo_relative(root, generated["video_path"]),
                "clip_sha256": generated["sha256"],
                "clip_validation": generated["validation"],
                "keyframe": keyframe,
                "keyframe_path": keyframe["image_path"] if keyframe else None,
                "keyframe_sha256": keyframe["image_sha256"] if keyframe else None,
                "image_provider": keyframe["provider_id"] if keyframe else None,
                "image_source_type": (
                    "REUSED_REAL_LOCAL_MODEL"
                    if visual_source.get("selection_reason")
                    == "LEGACY_IMAGE_JOB_FALLBACK"
                    else visual_source["source_type"]
                ),
                "visual_source": visual_source,
                "visual_source_type": visual_source["visual_source_type"],
                "source_asset_id": visual_source.get("source_asset_id"),
                "source_provider": visual_source["source_provider"],
                "source_type": visual_source["source_type"],
                "source_video_job_id": visual_source.get("source_video_job_id"),
                "source_image_asset_id": visual_source.get(
                    "source_image_asset_id"
                ),
                "source_video_duration_seconds": visual_source.get(
                    "source_duration_seconds"
                ),
                "video_duration_normalization": visual_source.get(
                    "duration_normalization"
                ),
                "source_video_audio_ignored": visual_source.get(
                    "source_audio_ignored", False
                ),
                "audio_provider": audio["provider_id"],
                "audio_source_type": _REAL_AUDIO_SOURCE_TYPE,
                "audio_model_id": audio["model_id"],
                "audio_model_revision": audio["model_revision"],
                "speaker": audio["speaker"],
                "language": audio["language"],
                "audio_text": audio["text"],
                "audio_path": audio["repo_relative_audio_path"],
                "audio_sha256": audio["audio_sha256"],
                "audio_duration": audio["duration_seconds"],
                "audio_duration_seconds": audio["duration_seconds"],
                "audio_sample_rate": audio["sample_rate"],
                "audio_channels": audio["channels"],
                "audio_sample_width_bytes": audio.get("sample_width_bytes"),
                "audio_seed": audio.get("seed"),
                "audio_peak_amplitude": audio.get("peak_amplitude"),
                "audio_rms": audio.get("rms"),
                "audio_generation_seconds": audio["generation_seconds"],
                "audio_real_time_factor": audio["real_time_factor"],
                "audio_model_sha256": audio["model_sha256"],
                "audio_trace_path": (
                    _repo_relative(root, Path(audio_trace_path))
                    if isinstance(audio_trace_path, str) and audio_trace_path
                    else None
                ),
                "audio_warnings": list(audio["warnings"]),
                "audio_reused": bool(audio["reused"]),
                "media_reuse": {
                    "media_only": media_only,
                    "script": media_only,
                    "keyframe": (
                        media_only
                        and visual_source["visual_source_type"] == "IMAGE"
                    ),
                    "audio": media_only,
                    "source_jobs": context.get("source_jobs", {}),
                },
            }
        )

    provider_trace = context.get("providers", {})
    if not isinstance(provider_trace, dict):
        provider_trace = {}
    else:
        provider_trace = dict(provider_trace)
    configured_image_provider = provider_trace.get("image_provider")
    if (
        configured_image_provider is not None
        and actual_image_provider_id is not None
        and str(configured_image_provider) not in {
            actual_image_provider_id,
            "reused",
            "selected-assets",
        }
    ):
        raise MediaToolError(
            "generation_context.providers.image_provider 与复用关键帧 Provider 不一致"
        )
    configured_audio_provider = provider_trace.get("audio_provider")
    if (
        configured_audio_provider is not None
        and str(configured_audio_provider) != provider_id
    ):
        raise MediaToolError(
            "generation_context.providers.audio_provider 与真实旁白 Provider 不一致"
        )
    provider_trace.update(
        {
            "image_provider": actual_image_provider_id,
            "image_providers": sorted(actual_image_provider_ids),
            "image_source_type": actual_image_source_type,
            "audio_provider": provider_id,
            "audio_source_type": _REAL_AUDIO_SOURCE_TYPE,
            "video_source_type": media_video_source_type,
        }
    )
    context["providers"] = provider_trace
    context["timing_plan"] = {
        "source_planned_duration_seconds": source_total,
        "rendered_planned_duration_seconds": rendered_total,
        "max_total_duration_seconds": float(max_total_duration_seconds),
        "path": (
            _repo_relative(root, resolved_timing_plan_path)
            if resolved_timing_plan_path is not None
            else None
        ),
    }
    context["visual_sources"] = {
        "video_shot_count": video_shot_count,
        "image_shot_count": image_shot_count,
        "explicit_image_shot_count": explicit_image_shot_count,
        "priority": ["VIDEO_SHOT", "EXPLICIT_IMAGE_ASSET", "LEGACY_IMAGE_JOB"],
    }
    script_provider = str(provider_trace.get("script_provider", "reused"))
    extension_total = round(rendered_total - source_total, 6)
    manifest = {
        "manifest_version": M6_MEDIA_MANIFEST_VERSION,
        "selection_mode": context.get("selection_mode", "MANUAL"),
        "selection_plan": context.get("selection_plan"),
        "media_only": media_only,
        "reused_providers": context.get("reused_providers"),
        "provider_calls": context.get("provider_calls", {}),
        "media_reuse": {
            "script": media_only,
            "keyframe": media_only,
            "audio": media_only,
            "source_jobs": context.get("source_jobs", {}),
        },
        "project": {"id": project_id, "title": project_title},
        "generation_context": context,
        "script_provider": script_provider,
        "image_provider": actual_image_provider_id,
        "audio_provider": provider_id,
        "audio_model_id": next(iter(model_values)),
        "audio_model_revision": next(iter(revision_values)),
        "audio_model_sha256": next(iter(model_sha256_values)),
        "speaker": next(iter(speaker_values)),
        "language": next(iter(language_values)),
        "video_source_type": media_video_source_type,
        "visual_source_summary": {
            "video_shot_count": video_shot_count,
            "image_shot_count": image_shot_count,
            "explicit_image_shot_count": explicit_image_shot_count,
        },
        "motion_preset": motion_preset or "legacy_shot_motion",
        "background_audio": _background_manifest(
            resolved_background, duration=rendered_total
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "runtime": {**runtime_summary(tools), "operating_system": platform.platform()},
        "media_spec": {
            "resolution": f"{width}x{height}",
            "frame_rate": fps,
            "source_planned_duration_seconds": source_total,
            "rendered_planned_duration_seconds": rendered_total,
            "planned_duration_seconds": rendered_total,
            "encoded_duration_seconds": validation["encoded_duration_seconds"],
            "actual_duration_seconds": validation["encoded_duration_seconds"],
            "duration_delta_seconds": validation["duration_delta_seconds"],
            "duration_tolerance_seconds": validation[
                "duration_tolerance_seconds"
            ],
            "duration_validation": validation["duration_validation"],
            "extended_by_seconds": extension_total,
            "max_total_duration_seconds": float(max_total_duration_seconds),
            "timing_plan_path": (
                _repo_relative(root, resolved_timing_plan_path)
                if resolved_timing_plan_path is not None
                else None
            ),
        },
        "pipeline": {
            "provider_id": provider_id,
            "source_type": media_video_source_type,
            "script_provider": script_provider,
            "image_provider": actual_image_provider_id,
            "image_providers": sorted(actual_image_provider_ids),
            "image_source_type": actual_image_source_type,
            "audio_provider": provider_id,
            "audio_source_type": _REAL_AUDIO_SOURCE_TYPE,
            "video_source_type": media_video_source_type,
            "visual_method": (
                "explicit VIDEO_SHOT normalized to target duration with image "
                "fallback -> FFmpeg final media"
                if video_shot_count
                else (
                    "selected validated image assets with legacy image fallback -> "
                    "FFmpeg deterministic structured motion/fade"
                    if explicit_image_shot_count
                    else "reused validated real PNG keyframes -> FFmpeg deterministic "
                    "structured motion/fade"
                )
            ),
            "motion_preset": motion_preset or "legacy_shot_motion",
            "audio_method": (
                "Qwen3-TTS WAV + user-upload background sidechain ducking -> "
                "FFmpeg 48kHz AAC"
                if resolved_background is not None
                else "Qwen3-TTS CustomVoice PCM16 WAV -> lead-in/silence padding -> FFmpeg 48kHz AAC"
            ),
            "audio_speed_changed": False,
            "audio_truncated": False,
            "mock_audio_used": False,
            "subtitle_method": "FFmpeg drawtext + independent UTF-8 LF textfile",
            "subtitle_rendering": "burned_in",
            "chinese_font_path": str(font),
            "network_required": False,
            "cloud_api_used": False,
            "api_key_required": False,
            "model_weights_required": not bool(context.get("media_only")),
        },
        "warnings": render_warnings,
        "shot_count": len(shot_manifest),
        "shots": shot_manifest,
        "timing_plan": {
            "source_planned_duration_seconds": source_total,
            "rendered_planned_duration_seconds": rendered_total,
            "extended_by_seconds": extension_total,
            "max_total_duration_seconds": float(max_total_duration_seconds),
            "shots": normalized_timings,
        },
        "output": {
            "file_path": _repo_relative(root, output_path),
            "subtitle_sidecar_path": _repo_relative(root, subtitle_sidecar),
            "file_size_bytes": output_path.stat().st_size,
            "sha256": digest,
            "poster_path": (
                _repo_relative(root, poster["path"]) if poster is not None else None
            ),
            "poster_sha256": poster["sha256"] if poster is not None else None,
            "poster_width": poster["width"] if poster is not None else None,
            "poster_height": poster["height"] if poster is not None else None,
            "poster_captured_at_seconds": (
                poster["captured_at_seconds"] if poster is not None else None
            ),
        },
        "ffprobe_validation": {
            **validation,
            "rendered_planned_duration_seconds": rendered_total,
        },
        "safe_command_log": command_log,
    }
    manifest_path = output_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    if progress_callback:
        progress_callback(97)
    return {
        "status": "PASS",
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "subtitle_path": str(subtitle_sidecar),
        "font_path": str(font),
        "sha256": digest,
        "validation": {
            **validation,
            "source_planned_duration_seconds": source_total,
            "rendered_planned_duration_seconds": rendered_total,
        },
        "source_planned_duration_seconds": source_total,
        "rendered_planned_duration_seconds": rendered_total,
        "extended_by_seconds": extension_total,
        "shots": shot_outputs,
        "manifest": manifest,
        "poster_path": str(poster["path"]) if poster is not None else None,
        "warnings": render_warnings,
    }


def resume_mock_project_short(
    *,
    root: Path,
    project_id: str,
    project_title: str,
    shots: list[dict[str, Any]],
    output_dir: Path,
    source_media_path: Path,
    output_filename: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    font_path: Path | None = None,
    provider_id: str = "mock",
    generation_context: dict[str, Any] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    motion_preset: str | None = None,
    background_audio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """复用 MEDIA_RENDER 失败时已编码完成的 MP4，只解码、探测和登记。"""

    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source = source_media_path.resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise MediaToolError(f"MEDIA_RENDER 恢复源 MP4 不存在或为空：{source}")

    normalized_shots = _normalize_project_shots(
        shots,
        width=width,
        height=height,
        fps=fps,
        provider_id=provider_id,
    )
    planned_duration = sum(
        float(shot["duration_seconds"]) for shot in normalized_shots
    )
    requested_name = output_filename or f"{_safe_file_stem(project_id, 'mock_export')}.mp4"
    if Path(requested_name).name != requested_name or Path(requested_name).suffix.lower() != ".mp4":
        raise MediaToolError("output_filename 必须是当前目录下的 .mp4 文件名")

    tools = resolve_media_tools()
    font = (font_path or find_chinese_font()).resolve()
    if not font.is_file():
        raise MediaToolError(f"配置的中文字体不存在：{font}")
    command_log: list[str] = []
    output_path = output_dir / requested_name
    temporary = _atomic_media_target(output_path)
    if temporary.resolve() == source:
        raise MediaToolError("恢复目标临时文件不得覆盖源 MP4")
    shutil.copyfile(source, temporary)
    validation = verify_media(
        tools,
        temporary,
        planned_duration_seconds=planned_duration,
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    full_decode = decode_media_fully(
        tools,
        temporary,
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    poster = _create_poster(
        tools=tools,
        video_path=output_path,
        output_dir=output_dir,
        first_shot_duration=float(normalized_shots[0]["duration_seconds"]),
        command_log=command_log,
    )
    if progress_callback:
        progress_callback(90)

    source_dir = source.parent
    subtitle_dir = output_dir / "subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    shot_manifest: list[dict[str, Any]] = []
    for index, shot in enumerate(normalized_shots, start=1):
        shot_stem = _safe_file_stem(str(shot["shot_id"]), f"shot_{index:02d}")
        source_clip = source_dir / "shots" / f"{shot_stem}.mp4"
        source_audio = source_dir / "shots" / f"{shot_stem}.wav"
        if not source_clip.is_file() or source_clip.stat().st_size <= 0:
            raise MediaToolError(f"MEDIA_RENDER 恢复缺少镜头文件：{source_clip}")
        if not source_audio.is_file() or source_audio.stat().st_size <= 0:
            raise MediaToolError(f"MEDIA_RENDER 恢复缺少音频文件：{source_audio}")
        subtitle = prepare_burned_subtitle(
            narration=str(shot["subtitle_text"]),
            text_path=subtitle_dir / f"{shot_stem}.txt",
            width=width,
            height=height,
            font_path=font,
        )
        clip_duration = float(shot["duration_seconds"])
        clip_validation = verify_media(
            tools,
            source_clip,
            min_duration=clip_duration - 0.20,
            max_duration=clip_duration + 0.20,
            expected_width=width,
            expected_height=height,
            expected_fps=float(fps),
            command_log=command_log,
        )
        shot_manifest.append(
            {
                "shot_id": shot["shot_id"],
                "sequence_no": shot["sequence_no"],
                "title": shot["title"],
                "visual_description": shot["visual_description"],
                "duration_seconds": shot["duration_seconds"],
                "subtitle": shot["subtitle_text"],
                "narration": shot["subtitle_text"],
                "provider_id": shot["provider_id"],
                "script_provider_id": shot.get("script_provider_id", provider_id),
                "source_type": shot["source_type"],
                "generation_parameters": shot["generation_parameters"],
                "motion_preset": motion_preset or "legacy_shot_motion",
                "subtitle_file": _repo_relative(root, subtitle.text_path),
                "subtitle_text_path": _repo_relative(root, subtitle.text_path),
                "rendered_subtitle_text": subtitle.rendered_text,
                "font_path": str(font),
                "subtitle_rendering": "burned_in",
                "subtitle_filter": subtitle.filter_expression,
                "clip_path": _repo_relative(root, source_clip),
                "audio_path": _repo_relative(root, source_audio),
                "audio_sha256": sha256_file(source_audio),
                "clip_sha256": sha256_file(source_clip),
                "clip_validation": clip_validation,
            }
        )

    subtitle_sidecar = output_dir / "subtitles.srt"
    _write_srt(normalized_shots, subtitle_sidecar)
    digest = sha256_file(output_path)
    context = dict(generation_context or {})
    provider_trace = context.get("providers", {})
    if not isinstance(provider_trace, dict):
        provider_trace = {}
    script_provider = str(provider_trace.get("script_provider", provider_id))
    image_provider = str(provider_trace.get("image_provider", "mock"))
    audio_provider = str(provider_trace.get("audio_provider", "mock"))
    video_source_type = str(
        provider_trace.get("video_source_type", "DETERMINISTIC_FALLBACK")
    )
    script_validation_warnings = context.get(
        "script_validation_warnings",
        {"unused_scene_ids": [], "unused_character_ids": []},
    )
    manifest = {
        "manifest_version": "m3.mixed-provider-export.v1",
        "project": {"id": project_id, "title": project_title},
        "generation_context": context,
        "script_provider": script_provider,
        "image_provider": image_provider,
        "audio_provider": audio_provider,
        "video_source_type": video_source_type,
        "motion_preset": motion_preset or "legacy_shot_motion",
        "background_audio": _background_manifest(
            dict(background_audio) if background_audio and background_audio.get("enabled") is True else None,
            duration=planned_duration,
        ),
        "script_validation_warnings": script_validation_warnings,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {**runtime_summary(tools), "operating_system": platform.platform()},
        "media_spec": {
            "resolution": f"{width}x{height}",
            "frame_rate": fps,
            "planned_duration_seconds": validation["planned_duration_seconds"],
            "encoded_duration_seconds": validation["encoded_duration_seconds"],
            "actual_duration_seconds": validation["encoded_duration_seconds"],
            "duration_delta_seconds": validation["duration_delta_seconds"],
            "duration_tolerance_seconds": validation[
                "duration_tolerance_seconds"
            ],
            "duration_validation": validation["duration_validation"],
        },
        "pipeline": {
            "provider_id": provider_id,
            "source_type": "DETERMINISTIC_FALLBACK",
            "script_provider": script_provider,
            "image_provider": image_provider,
            "audio_provider": audio_provider,
            "video_source_type": video_source_type,
            "visual_method": "复用已编码的 FFmpeg Mock 媒体",
            "motion_preset": motion_preset or "legacy_shot_motion",
            "audio_method": "复用已编码的 AAC 音频流",
            "subtitle_method": "复用已烧录画面并重建 UTF-8 LF 字幕追溯文件",
            "subtitle_rendering": "burned_in",
            "chinese_font_path": str(font),
            "network_required": False,
            "api_key_required": False,
            "model_weights_required": False,
        },
        "recovery": {
            "resumed_from_stage": "MEDIA_RENDER",
            "source_media_path": _repo_relative(root, source),
            "source_media_sha256": sha256_file(source),
            "media_reused": True,
            "reencoded": False,
            "full_decode": full_decode,
        },
        "shot_count": len(shot_manifest),
        "shots": shot_manifest,
        "output": {
            "file_path": _repo_relative(root, output_path),
            "subtitle_sidecar_path": _repo_relative(root, subtitle_sidecar),
            "file_size_bytes": output_path.stat().st_size,
            "sha256": digest,
            "poster_path": _repo_relative(root, poster["path"]),
            "poster_sha256": poster["sha256"],
            "poster_width": poster["width"],
            "poster_height": poster["height"],
            "poster_captured_at_seconds": poster["captured_at_seconds"],
        },
        "ffprobe_validation": validation,
        "safe_command_log": command_log,
    }
    manifest_path = output_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    if progress_callback:
        progress_callback(95)
    return {
        "status": "PASS",
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "subtitle_path": str(subtitle_sidecar),
        "font_path": str(font),
        "sha256": digest,
        "validation": validation,
        "shots": [],
        "manifest": manifest,
        "media_reused": True,
        "reencoded": False,
        "source_media_path": str(source),
        "poster_path": str(poster["path"]),
    }


def generate_m0_smoke(root: Path, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    output_dir = (output_dir or root / "data" / "generated" / "m0").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tools = resolve_media_tools()
    font = find_chinese_font()
    subtitle_path = root / "fixtures" / "paper-crane" / "subtitles" / "m0.txt"
    if not subtitle_path.is_file():
        raise MediaToolError(f"找不到 M0 字幕文件：{subtitle_path}")
    subtitle = prepare_burned_subtitle(
        narration=subtitle_path.read_text(encoding="utf-8"),
        text_path=output_dir / "subtitles" / "m0.txt",
        width=WIDTH,
        height=HEIGHT,
        font_path=font,
    )

    audio_path = output_dir / "mock_audio.wav"
    output_path = output_dir / "smoke_test.mp4"
    generate_mock_wav(audio_path, 5.0, 440.0)

    filters = [
        "drawbox=x=70:y=70:w=1140:h=500:color=0x31577f@0.42:t=fill",
        "drawbox=x=410:y=185:w=460:h=245:color=0xa9d9ff@0.18:t=fill",
        "drawbox=x=410:y=185:w=460:h=245:color=0xcbe9ff@0.75:t=7",
        _label_filter(font, "M0 MEDIA SMOKE TEST", 42, 28),
        subtitle.filter_expression,
        "fade=t=in:st=0:d=0.25,fade=t=out:st=4.75:d=0.25",
        "format=yuv420p",
    ]
    command_log: list[str] = []
    temporary = _atomic_media_target(output_path)
    run_command(
        [
            tools.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x101832:s={WIDTH}x{HEIGHT}:r={FPS}:d=5",
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            ",".join(filters),
            "-t",
            "5",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            temporary,
        ],
        timeout_seconds=180,
        command_log=command_log,
    )
    validation = verify_media(
        tools,
        temporary,
        min_duration=4.80,
        max_duration=5.20,
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    result = {
        "status": "PASS",
        "output_path": str(output_path),
        "font_path": str(font),
        "narration": subtitle.narration,
        "subtitle_text_path": str(subtitle.text_path),
        "rendered_subtitle_text": subtitle.rendered_text,
        "subtitle_rendering": "burned_in",
        "subtitle_filter": subtitle.filter_expression,
        "sha256": sha256_file(output_path),
        "validation": validation,
        "commands": command_log,
    }
    _atomic_json(output_dir / "smoke_test.validation.json", result)
    return result


def generate_m1_short(root: Path, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    output_dir = (output_dir or root / "data" / "generated" / "m1").resolve()
    fixture_path = root / "fixtures" / "paper-crane" / "script.v1.json"
    fixture = load_script_fixture(fixture_path)
    rendered = render_mock_project_short(
        root=root,
        project_id=str(fixture["project"]["project_id"]),
        project_title=str(fixture["project"]["title"]),
        shots=fixture["shots"],
        output_dir=output_dir,
        output_filename="paper_crane_night_flight.mp4",
    )
    output_path = Path(rendered["output_path"])
    validation = rendered["validation"]
    if abs(float(validation["duration_seconds"]) - 28.0) > 0.50:
        raise MediaToolError(
            "M1 总时长偏离计划超过 0.5 秒："
            f"实际 {validation['duration_seconds']} 秒"
        )
    subtitle_sidecar = Path(rendered["subtitle_path"])
    digest = str(rendered["sha256"])
    versions = dict(rendered["manifest"]["runtime"])
    versions.pop("operating_system", None)
    font = Path(rendered["font_path"])
    command_log = list(rendered["manifest"]["safe_command_log"])
    shot_outputs = rendered["shots"]
    shot_manifest: list[dict[str, Any]] = []
    for shot, generated in zip(fixture["shots"], shot_outputs, strict=True):
        shot_manifest.append(
            {
                "shot_id": shot["shot_id"],
                "sequence_no": shot["sequence_no"],
                "title": shot["title"],
                "visual_description": shot["visual_description"],
                "duration_seconds": shot["duration_seconds"],
                "subtitle": shot["subtitle_text"],
                "narration": shot["subtitle_text"],
                "subtitle_file": shot["subtitle_file"],
                "subtitle_text_path": _repo_relative(
                    root, generated["subtitle_path"]
                ),
                "rendered_subtitle_text": generated["rendered_subtitle_text"],
                "font_path": str(generated["subtitle_font_path"]),
                "subtitle_rendering": generated["subtitle_rendering"],
                "subtitle_filter": generated["subtitle_filter"],
                "provider_id": "mock",
                "source_type": "DETERMINISTIC_FALLBACK",
                "generation_parameters": shot["generation_parameters"],
                "clip_path": _repo_relative(root, generated["video_path"]),
                "clip_sha256": generated["sha256"],
                "clip_validation": generated["validation"],
            }
        )

    manifest = {
        "manifest_version": "m1.manifest.v1",
        "project_name": fixture["project"]["title"],
        "fixture_version": fixture["fixture_version"],
        "fixture_path": _repo_relative(root, fixture_path),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {
            **versions,
            "operating_system": platform.platform(),
        },
        "media_spec": {
            "resolution": f"{WIDTH}x{HEIGHT}",
            "frame_rate": FPS,
            "planned_duration_seconds": 28.0,
            "encoded_duration_seconds": validation["encoded_duration_seconds"],
            "actual_duration_seconds": validation["encoded_duration_seconds"],
            "duration_delta_seconds": validation["duration_delta_seconds"],
            "duration_tolerance_seconds": validation[
                "duration_tolerance_seconds"
            ],
            "duration_validation": validation["duration_validation"],
        },
        "pipeline": {
            "provider_id": "mock",
            "source_type": "DETERMINISTIC_FALLBACK",
            "visual_method": "FFmpeg color/drawbox/drawtext/zoompan/fade filters",
            "audio_method": "Python standard-library deterministic PCM WAV -> FFmpeg AAC",
            "subtitle_method": "FFmpeg drawtext + independent UTF-8 textfile",
            "subtitle_rendering": "burned_in",
            "chinese_font_path": str(font),
            "network_required": False,
            "api_key_required": False,
            "model_weights_required": False,
        },
        "shot_count": len(shot_manifest),
        "shots": shot_manifest,
        "output": {
            "file_path": _repo_relative(root, output_path),
            "subtitle_sidecar_path": _repo_relative(root, subtitle_sidecar),
            "file_size_bytes": output_path.stat().st_size,
            "sha256": digest,
        },
        "ffprobe_validation": validation,
        "safe_command_log": command_log,
    }
    manifest_path = output_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        "status": "PASS",
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "font_path": str(font),
        "sha256": digest,
        "validation": validation,
    }

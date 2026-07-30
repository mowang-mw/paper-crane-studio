"""《纸鹤的夜航》M0/M1 确定性 Mock 媒体流水线。"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ffmpeg import (
    MediaToolError,
    MediaTools,
    ffmpeg_filter_path,
    find_chinese_font,
    resolve_media_tools,
    run_command,
    runtime_summary,
    sha256_file,
    verify_media,
)


WIDTH = 1280
HEIGHT = 720
CANVAS_WIDTH = 1344
CANVAS_HEIGHT = 756
FPS = 24
SAMPLE_RATE = 48_000


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


def _subtitle_filter(font: Path, text_file: Path) -> str:
    return (
        "drawtext="
        f"fontfile={ffmpeg_filter_path(font)}:"
        f"textfile={ffmpeg_filter_path(text_file)}:"
        "fontcolor=white:fontsize=38:line_spacing=10:"
        "x=(w-text_w)/2:y=h-text_h-58:"
        "box=1:boxcolor=black@0.58:boxborderw=16"
    )


def _label_filter(font: Path, text: str, y: int, fontsize: int = 30) -> str:
    safe_text = text.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:")
    return (
        "drawtext="
        f"fontfile={ffmpeg_filter_path(font)}:"
        f"text='{safe_text}':fontcolor=white:fontsize={fontsize}:"
        f"x=36:y={y}:box=1:boxcolor=black@0.42:boxborderw=10"
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
) -> str:
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
    subtitle_path: Path,
    shot: dict[str, Any],
    output_path: Path,
    command_log: list[str],
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
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
        )
    )
    filters.append(_label_filter(font, f"SHOT {int(shot['sequence_no']):02d} - {parameters['scene_label']}", 30, 30))
    filters.append(_label_filter(font, "MOCK VISUAL / FFMPEG MOTION", 84, 20))
    filters.append(_subtitle_filter(font, subtitle_path))
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
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    return {
        "video_path": output_path,
        "audio_path": audio_path,
        "validation": validation,
        "sha256": sha256_file(output_path),
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
    target.write_text("\n".join(lines), encoding="utf-8")


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
    if len(shots) != 4:
        raise MediaToolError(f"M2 Mock 短片必须包含 4 个镜头，实际 {len(shots)}")
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
            f"M2 短片计划总时长必须在 20—40 秒内，实际 {total_duration:.3f} 秒"
        )
    return normalized


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
) -> dict[str, Any]:
    """用 M1 的同一 FFmpeg 链路渲染一个隔离的项目导出。

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
    shot_outputs: list[dict[str, Any]] = []
    for index, shot in enumerate(normalized_shots, start=1):
        shot_stem = _safe_file_stem(str(shot["shot_id"]), f"shot_{index:02d}")
        subtitle_path = subtitle_dir / f"{shot_stem}.txt"
        subtitle_path.write_text(str(shot["subtitle_text"]).strip() + "\n", encoding="utf-8")
        generated = _create_shot(
            tools=tools,
            font=font,
            subtitle_path=subtitle_path,
            shot=shot,
            output_path=shot_dir / f"{shot_stem}.mp4",
            command_log=command_log,
            width=width,
            height=height,
            fps=fps,
        )
        generated["subtitle_path"] = subtitle_path
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
            temporary,
        ],
        timeout_seconds=180,
        command_log=command_log,
    )
    planned_duration = sum(
        float(shot["duration_seconds"]) for shot in normalized_shots
    )
    validation = verify_media(
        tools,
        temporary,
        min_duration=max(20.0, planned_duration - 0.50),
        max_duration=min(40.0, planned_duration + 0.50),
        expected_width=width,
        expected_height=height,
        expected_fps=float(fps),
        command_log=command_log,
    )
    os.replace(temporary, output_path)
    if progress_callback:
        progress_callback(90)

    subtitle_sidecar = output_dir / "subtitles.srt"
    _write_srt(normalized_shots, subtitle_sidecar)
    digest = sha256_file(output_path)
    shot_manifest: list[dict[str, Any]] = []
    for shot, generated in zip(normalized_shots, shot_outputs, strict=True):
        shot_manifest.append(
            {
                "shot_id": shot["shot_id"],
                "sequence_no": shot["sequence_no"],
                "title": shot["title"],
                "visual_description": shot["visual_description"],
                "duration_seconds": shot["duration_seconds"],
                "subtitle": shot["subtitle_text"],
                "provider_id": shot["provider_id"],
                "source_type": shot["source_type"],
                "generation_parameters": shot["generation_parameters"],
                "subtitle_file": _repo_relative(root, generated["subtitle_path"]),
                "clip_path": _repo_relative(root, generated["video_path"]),
                "audio_path": _repo_relative(root, generated["audio_path"]),
                "audio_sha256": sha256_file(generated["audio_path"]),
                "clip_sha256": generated["sha256"],
                "clip_validation": generated["validation"],
            }
        )

    manifest = {
        "manifest_version": "m2.mock-export.v1",
        "project": {"id": project_id, "title": project_title},
        "generation_context": generation_context or {},
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {**runtime_summary(tools), "operating_system": platform.platform()},
        "media_spec": {
            "resolution": f"{width}x{height}",
            "frame_rate": fps,
            "planned_duration_seconds": planned_duration,
            "actual_duration_seconds": validation["duration_seconds"],
        },
        "pipeline": {
            "provider_id": provider_id,
            "source_type": "DETERMINISTIC_FALLBACK",
            "visual_method": "FFmpeg color/drawbox/drawtext/zoompan/fade filters",
            "audio_method": "Python standard-library deterministic PCM WAV -> FFmpeg AAC",
            "subtitle_method": "FFmpeg drawtext + independent UTF-8 textfile",
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

    audio_path = output_dir / "mock_audio.wav"
    output_path = output_dir / "smoke_test.mp4"
    generate_mock_wav(audio_path, 5.0, 440.0)

    filters = [
        "drawbox=x=70:y=70:w=1140:h=500:color=0x31577f@0.42:t=fill",
        "drawbox=x=410:y=185:w=460:h=245:color=0xa9d9ff@0.18:t=fill",
        "drawbox=x=410:y=185:w=460:h=245:color=0xcbe9ff@0.75:t=7",
        _label_filter(font, "M0 MEDIA SMOKE TEST", 42, 28),
        _subtitle_filter(font, subtitle_path),
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
                "subtitle_file": shot["subtitle_file"],
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
            "actual_duration_seconds": validation["duration_seconds"],
        },
        "pipeline": {
            "provider_id": "mock",
            "source_type": "DETERMINISTIC_FALLBACK",
            "visual_method": "FFmpeg color/drawbox/drawtext/zoompan/fade filters",
            "audio_method": "Python standard-library deterministic PCM WAV -> FFmpeg AAC",
            "subtitle_method": "FFmpeg drawtext + independent UTF-8 textfile",
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

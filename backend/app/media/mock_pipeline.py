"""《纸鹤的夜航》M0/M1 确定性 Mock 媒体流水线。"""

from __future__ import annotations

import json
import math
import os
import platform
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _motion_filter(motion: str, frame_count: int) -> str:
    common = f"d=1:s={WIDTH}x{HEIGHT}:fps={FPS}"
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
    fixture_root: Path,
    shot: dict[str, Any],
    output_path: Path,
    command_log: list[str],
) -> dict[str, Any]:
    duration = float(shot["duration_seconds"])
    parameters = shot["generation_parameters"]
    frame_count = int(round(duration * FPS))
    subtitle_path = fixture_root / str(shot["subtitle_file"])
    audio_path = output_path.with_suffix(".wav")
    generate_mock_wav(audio_path, duration, float(parameters["audio_frequency_hz"]))

    filters = _composition_filters(str(parameters["composition_template"]))
    filters.append(_motion_filter(str(parameters["motion"]), frame_count))
    filters.append(_label_filter(font, f"SHOT {int(shot['sequence_no']):02d} - {parameters['scene_label']}", 30, 30))
    filters.append(_label_filter(font, "MOCK VISUAL / FFMPEG MOTION", 84, 20))
    filters.append(_subtitle_filter(font, subtitle_path))
    fade_out_start = max(0.0, duration - 0.35)
    filters.append(f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.3f}:d=0.35")
    filters.append("format=yuv420p")

    temporary = _atomic_media_target(output_path)
    source = (
        f"color=c={parameters['background_color']}:"
        f"s={CANVAS_WIDTH}x{CANVAS_HEIGHT}:r={FPS}:d={duration:.3f}"
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
    output_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = output_dir / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    fixture_path = root / "fixtures" / "paper-crane" / "script.v1.json"
    fixture = load_script_fixture(fixture_path)
    fixture_root = fixture_path.parent
    tools = resolve_media_tools()
    font = find_chinese_font()
    command_log: list[str] = []
    shot_outputs: list[dict[str, Any]] = []

    for shot in fixture["shots"]:
        output_path = shot_dir / f"{shot['shot_id']}.mp4"
        generated = _create_shot(
            tools=tools,
            font=font,
            fixture_root=fixture_root,
            shot=shot,
            output_path=output_path,
            command_log=command_log,
        )
        shot_outputs.append(generated)

    concat_path = output_dir / "shots.ffconcat"
    concat_lines = ["ffconcat version 1.0"]
    for generated in shot_outputs:
        path_text = generated["video_path"].resolve().as_posix().replace("'", r"'\''")
        concat_lines.append(f"file '{path_text}'")
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    output_path = output_dir / "paper_crane_night_flight.mp4"
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
    validation = verify_media(
        tools,
        temporary,
        min_duration=20.0,
        max_duration=40.0,
        command_log=command_log,
    )
    if abs(float(validation["duration_seconds"]) - 28.0) > 0.50:
        raise MediaToolError(
            "M1 总时长偏离计划超过 0.5 秒："
            f"实际 {validation['duration_seconds']} 秒"
        )
    os.replace(temporary, output_path)

    subtitle_sidecar = output_dir / "subtitles.srt"
    _write_srt(fixture["shots"], subtitle_sidecar)
    digest = sha256_file(output_path)
    versions = runtime_summary(tools)
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

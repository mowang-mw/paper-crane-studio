"""FFmpeg/ffprobe 的安全调用、探测与验证工具。

本模块只使用 Python 标准库。所有外部命令均通过参数列表执行，绝不使用
``shell=True``。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence


class MediaToolError(RuntimeError):
    """媒体命令、输出或环境不满足要求。"""


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path


FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
)


def _resolve_executable(command: str, env_name: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file():
            raise MediaToolError(
                f"{env_name} 指向的文件不存在：{candidate}。"
                "请配置当前 anime-platform 环境中的可执行文件。"
            )
        return candidate.resolve()

    found = shutil.which(command)
    if not found:
        raise MediaToolError(
            f"找不到 {command}。请先激活 anime-platform Conda 环境，"
            f"或设置 {env_name} 为已验证的绝对路径。"
        )
    candidate = Path(found)
    if not candidate.is_file():
        raise MediaToolError(f"{command} 解析结果不是文件：{candidate}")
    return candidate.resolve()


def resolve_media_tools() -> MediaTools:
    """从显式配置或当前 PATH 解析真实媒体工具路径。"""

    return MediaTools(
        ffmpeg=_resolve_executable("ffmpeg", "FFMPEG_BIN"),
        ffprobe=_resolve_executable("ffprobe", "FFPROBE_BIN"),
    )


def find_chinese_font(candidates: Iterable[Path] = FONT_CANDIDATES) -> Path:
    checked: list[str] = []
    for font in candidates:
        checked.append(str(font))
        if font.is_file():
            return font.resolve()
    raise MediaToolError(
        "未找到可用中文字体。已按顺序检查：" + "、".join(checked)
    )


def format_command(args: Sequence[str | os.PathLike[str]]) -> str:
    """返回适合日志展示的命令，不经 shell 执行。"""

    values = [str(arg).replace("\r", " ").replace("\n", " ") for arg in args]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def run_command(
    args: Sequence[str | os.PathLike[str]],
    *,
    timeout_seconds: int = 180,
    command_log: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行命令并在失败时提供可理解且已截断的诊断。"""

    normalized = [str(arg) for arg in args]
    display = format_command(normalized)
    print(f"[command] {display}")
    if command_log is not None:
        command_log.append(display)

    try:
        completed = subprocess.run(
            normalized,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(
            f"命令在 {timeout_seconds} 秒后超时：{display}"
        ) from exc
    except OSError as exc:
        raise MediaToolError(f"无法启动媒体命令：{display}\n原因：{exc}") from exc

    if completed.returncode != 0:
        stderr_lines = (completed.stderr or "").splitlines()
        stderr_tail = "\n".join(stderr_lines[-40:]) or "<无 stderr>"
        raise MediaToolError(
            f"媒体命令失败（退出码 {completed.returncode}）：{display}\n"
            f"stderr 尾部：\n{stderr_tail}"
        )
    return completed


def version_line(executable: Path) -> str:
    completed = run_command([executable, "-version"], timeout_seconds=30)
    for line in completed.stdout.splitlines():
        if line.strip():
            return line.strip()
    raise MediaToolError(f"{executable} -version 未返回版本信息")


def ffmpeg_filter_path(path: Path) -> str:
    """将 Windows 路径转为 FFmpeg filter 可安全解析的单引号值。"""

    absolute = path.resolve()
    normalized = absolute.as_posix()
    normalized = normalized.replace("\\", "\\\\")
    normalized = normalized.replace(":", r"\:")
    normalized = normalized.replace("'", r"\'")
    return f"'{normalized}'"


def ffprobe_json(
    tools: MediaTools,
    media_path: Path,
    *,
    command_log: list[str] | None = None,
) -> dict:
    if not media_path.is_file() or media_path.stat().st_size <= 0:
        raise MediaToolError(f"媒体文件不存在或为空：{media_path}")
    completed = run_command(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            media_path,
        ],
        timeout_seconds=60,
        command_log=command_log,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"ffprobe 返回的不是有效 JSON：{media_path}") from exc
    if not isinstance(payload, dict):
        raise MediaToolError(f"ffprobe JSON 顶层结构无效：{media_path}")
    return payload


def _parse_frame_rate(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if raw and raw != "0/0":
            try:
                return float(Fraction(str(raw)))
            except (ValueError, ZeroDivisionError):
                continue
    raise MediaToolError("ffprobe 未返回可解析的视频帧率")


def verify_media(
    tools: MediaTools,
    media_path: Path,
    *,
    expected_width: int = 1280,
    expected_height: int = 720,
    expected_fps: float = 24.0,
    min_duration: float,
    max_duration: float,
    expected_video_codec: str = "h264",
    expected_audio_codec: str = "aac",
    command_log: list[str] | None = None,
) -> dict:
    """验证一个 MP4 的关键媒体契约并返回精简摘要。"""

    payload = ffprobe_json(tools, media_path, command_log=command_log)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaToolError("ffprobe JSON 缺少 streams 数组")

    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise MediaToolError(f"预期 1 个视频流，实际 {len(videos)} 个")
    if len(audios) != 1:
        raise MediaToolError(f"预期 1 个音频流，实际 {len(audios)} 个")

    video = videos[0]
    audio = audios[0]
    if video.get("codec_name") != expected_video_codec:
        raise MediaToolError(
            f"视频编码不符：预期 {expected_video_codec}，实际 {video.get('codec_name')}"
        )
    if audio.get("codec_name") != expected_audio_codec:
        raise MediaToolError(
            f"音频编码不符：预期 {expected_audio_codec}，实际 {audio.get('codec_name')}"
        )
    if video.get("width") != expected_width or video.get("height") != expected_height:
        raise MediaToolError(
            "分辨率不符："
            f"预期 {expected_width}x{expected_height}，"
            f"实际 {video.get('width')}x{video.get('height')}"
        )

    fps = _parse_frame_rate(video)
    if abs(fps - expected_fps) > 0.05:
        raise MediaToolError(f"帧率不符：预期约 {expected_fps}，实际 {fps:.6f}")

    format_data = payload.get("format") or {}
    try:
        duration = float(format_data.get("duration"))
    except (TypeError, ValueError) as exc:
        raise MediaToolError("ffprobe 未返回可解析的容器时长") from exc
    if not min_duration <= duration <= max_duration:
        raise MediaToolError(
            f"时长不符：要求 {min_duration:.3f}—{max_duration:.3f} 秒，"
            f"实际 {duration:.6f} 秒"
        )

    pixel_format = video.get("pix_fmt")
    if pixel_format != "yuv420p":
        raise MediaToolError(f"像素格式不符：预期 yuv420p，实际 {pixel_format}")

    return {
        "ffprobe_ok": True,
        "format_name": format_data.get("format_name"),
        "duration_seconds": round(duration, 6),
        "video_stream_count": len(videos),
        "audio_stream_count": len(audios),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": round(fps, 6),
        "pixel_format": pixel_format,
        "audio_sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "audio_channels": audio.get("channels"),
        "file_size_bytes": media_path.stat().st_size,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_summary(tools: MediaTools) -> dict:
    return {
        "python_version": sys.version.split()[0],
        "ffmpeg_version": version_line(tools.ffmpeg),
        "ffprobe_version": version_line(tools.ffprobe),
        "ffmpeg_path": str(tools.ffmpeg),
        "ffprobe_path": str(tools.ffprobe),
    }

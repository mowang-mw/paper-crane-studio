"""Final Media 显式视觉资产的文件校验与安全路径解析。"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from ..media.ffmpeg import (
    MediaToolError,
    ffprobe_json,
    resolve_media_tools,
    run_command,
    sha256_file,
)
from ..models import Asset


class FinalMediaVisualError(ValueError):
    pass


def _safe_asset_path(*, data_dir: Path, project_dir: Path, asset: Asset) -> Path:
    stored = Path(asset.file_path)
    if stored.is_absolute():
        raise FinalMediaVisualError("视觉资产路径必须是相对路径。")
    candidate = (Path(data_dir).resolve() / stored).resolve()
    try:
        candidate.relative_to(Path(project_dir).resolve())
    except ValueError as exc:
        raise FinalMediaVisualError("视觉资产路径越过当前项目目录。") from exc
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise FinalMediaVisualError("视觉资产文件不存在或为空。")
    if sha256_file(candidate) != asset.sha256:
        raise FinalMediaVisualError("视觉资产 SHA256 与数据库记录不一致。")
    return candidate


def _frame_rate(stream: dict[str, Any]) -> float | None:
    value = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def validate_video_asset_file(
    *, data_dir: Path, project_dir: Path, asset: Asset
) -> tuple[Path, dict[str, Any]]:
    if asset.asset_type != "VIDEO_SHOT":
        raise FinalMediaVisualError("所选资产不是 VIDEO_SHOT。")
    path = _safe_asset_path(data_dir=data_dir, project_dir=project_dir, asset=asset)
    try:
        tools = resolve_media_tools()
        probe = ffprobe_json(tools, path)
        streams = probe.get("streams")
        videos = (
            [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
            if isinstance(streams, list)
            else []
        )
        if len(videos) != 1:
            raise ValueError("expected one video stream")
        video = videos[0]
        width = int(video["width"])
        height = int(video["height"])
        duration = float((probe.get("format") or {})["duration"])
        if width <= 0 or height <= 0 or duration <= 0:
            raise ValueError("invalid dimensions or duration")
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-an",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=180,
        )
    except (KeyError, TypeError, ValueError, OSError, MediaToolError) as exc:
        raise FinalMediaVisualError("VIDEO_SHOT 无法完整解码或缺少合法视频流。") from exc
    return path, {
        "codec_name": str(video.get("codec_name") or ""),
        "width": width,
        "height": height,
        "fps": _frame_rate(video),
        "duration_seconds": duration,
        "has_audio": any(
            isinstance(item, dict) and item.get("codec_type") == "audio"
            for item in streams
        ),
        "sha256": asset.sha256,
    }


__all__ = ["FinalMediaVisualError", "validate_video_asset_file"]

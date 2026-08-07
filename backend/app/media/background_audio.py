"""用户上传背景音的受控存储、探测和追溯。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from .ffmpeg import (
    MediaToolError,
    ffprobe_json,
    resolve_media_tools,
    run_command,
    sha256_file,
)


MAX_BACKGROUND_AUDIO_BYTES = 20 * 1024 * 1024
DEFAULT_BACKGROUND_VOLUME = 0.12
MIN_BACKGROUND_VOLUME = 0.02
MAX_BACKGROUND_VOLUME = 0.35
ALLOWED_BACKGROUND_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg"}
ALLOWED_BACKGROUND_MIME_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/x-m4a",
    "audio/ogg",
    "application/ogg",
    "application/octet-stream",
}
RIGHTS_NOTICE = (
    "来源为用户上传；用户须确认拥有使用权，平台不声明该音频的版权归属。"
)


def background_audio_directory(data_dir: Path, project_id: str) -> Path:
    return Path(data_dir).resolve() / "projects" / project_id / "background-audio"


def background_audio_metadata_path(data_dir: Path, project_id: str) -> Path:
    return background_audio_directory(data_dir, project_id) / "current.json"


def validate_background_filename(filename: str, mime_type: str) -> str:
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename or len(safe_name) > 255:
        raise MediaToolError("背景音文件名无效")
    extension = Path(safe_name).suffix.lower()
    if extension not in ALLOWED_BACKGROUND_EXTENSIONS:
        raise MediaToolError("背景音只支持 WAV、MP3、M4A 或 OGG")
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    if normalized_mime not in ALLOWED_BACKGROUND_MIME_TYPES:
        raise MediaToolError("背景音 MIME 类型不受支持")
    return extension


def inspect_background_audio(path: Path) -> dict[str, Any]:
    tools = resolve_media_tools()
    probe = ffprobe_json(tools, path)
    streams = probe.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(item, dict) and item.get("codec_type") == "audio"
        for item in streams
    ):
        raise MediaToolError("上传文件没有可解码的音轨")
    format_payload = probe.get("format")
    duration_raw = format_payload.get("duration") if isinstance(format_payload, dict) else None
    if duration_raw is None:
        duration_raw = next(
            (
                item.get("duration")
                for item in streams
                if isinstance(item, dict)
                and item.get("codec_type") == "audio"
                and item.get("duration") is not None
            ),
            None,
        )
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise MediaToolError("ffprobe 未返回有效背景音时长") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > 6 * 60 * 60:
        raise MediaToolError("背景音时长异常")

    run_command(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=120,
    )
    audio_stream = next(
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    )
    return {
        "duration_seconds": round(duration, 6),
        "codec_name": str(audio_stream.get("codec_name") or "unknown"),
        "sample_rate": int(audio_stream["sample_rate"])
        if str(audio_stream.get("sample_rate") or "").isdigit()
        else None,
        "channels": int(audio_stream["channels"])
        if type(audio_stream.get("channels")) is int
        else None,
    }


def write_background_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def load_background_metadata(data_dir: Path, project_id: str) -> dict[str, Any] | None:
    metadata_path = background_audio_metadata_path(data_dir, project_id)
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaToolError("背景音元数据损坏") from exc
    if not isinstance(payload, dict):
        raise MediaToolError("背景音元数据格式无效")
    stored_path = payload.get("storage_path")
    if not isinstance(stored_path, str) or not stored_path:
        raise MediaToolError("背景音元数据缺少存储路径")
    candidate = (Path(data_dir).resolve() / stored_path).resolve()
    project_root = (Path(data_dir).resolve() / "projects" / project_id).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise MediaToolError("背景音存储路径越界") from exc
    if not candidate.is_file() or sha256_file(candidate) != payload.get("sha256"):
        raise MediaToolError("背景音文件缺失或 SHA256 不匹配")
    return payload


def background_job_snapshot(
    *,
    data_dir: Path,
    project_id: str,
    enabled: bool,
    volume: float,
) -> dict[str, Any]:
    normalized_volume = float(volume)
    if not MIN_BACKGROUND_VOLUME <= normalized_volume <= MAX_BACKGROUND_VOLUME:
        raise MediaToolError(
            f"背景音量必须在 {MIN_BACKGROUND_VOLUME:.2f}—{MAX_BACKGROUND_VOLUME:.2f} 之间"
        )
    if not enabled:
        return {"enabled": False, "volume": round(normalized_volume, 3)}
    metadata = load_background_metadata(data_dir, project_id)
    if metadata is None:
        raise MediaToolError("尚未上传背景音，不能启用背景音")
    return {
        **metadata,
        "enabled": True,
        "volume": round(normalized_volume, 3),
        "rights_notice": RIGHTS_NOTICE,
    }


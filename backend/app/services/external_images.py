"""Human-in-the-loop 外部图片提示词、导入校验与资产序列化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..media.ffmpeg import (
    MediaToolError,
    ffprobe_json,
    resolve_media_tools,
    run_command,
    sha256_file,
)
from ..models import Asset, Shot as DatabaseShot
from ..script_schema import ScriptV1, Shot


EXTERNAL_PROMPT_ADAPTER_ID = "external-natural-language-v1"
EXTERNAL_IMAGE_PROVIDER_ID = "external-import"
EXTERNAL_IMAGE_SOURCE_TYPE = "EXTERNAL_IMPORT"
EXTERNAL_IMAGE_GENERATION_MODE = "HUMAN_IN_THE_LOOP"
MAX_EXTERNAL_IMAGE_BYTES = 10 * 1024 * 1024
MAX_EXTERNAL_IMAGE_SIDE = 4096
MAX_EXTERNAL_IMAGE_PIXELS = 4096 * 4096


class ExternalImageError(ValueError):
    pass


def database_shot_for_script_shot(
    database_shots: Iterable[DatabaseShot], script_shot: Shot
) -> DatabaseShot:
    by_index: DatabaseShot | None = None
    for item in database_shots:
        parameters = item.parameters_json if isinstance(item.parameters_json, dict) else {}
        if parameters.get("provider_shot_id") == script_shot.id:
            return item
        if item.shot_index == script_shot.index:
            by_index = item
    if by_index is None:
        raise ExternalImageError(f"镜头 {script_shot.id} 没有对应的数据库记录。")
    return by_index


def selected_script_shot(script: ScriptV1, shot_id: str) -> Shot:
    selected = next((item for item in script.shots if item.id == shot_id), None)
    if selected is None:
        raise ExternalImageError(f"ScriptV1 中不存在镜头 {shot_id}。")
    return selected


def build_external_image_prompt_bundle(
    *, project_story: str, script: ScriptV1, shot_id: str
) -> dict[str, Any]:
    """把已完成的 ScriptV1 单镜头规划确定性适配为自然语言提示词。"""

    shot = selected_script_shot(script, shot_id)
    characters_by_id = {item.id: item for item in script.characters}
    scene_by_id = {item.id: item for item in script.scenes}
    characters = [characters_by_id[item] for item in shot.character_ids]
    scene = scene_by_id[shot.scene_id]
    character_lines = [
        (
            f"- {item.name}（{item.role}）：{item.appearance}；服装：{item.costume}；"
            f"一致性要求：{item.consistency_prompt}"
        )
        for item in characters
    ]
    prompt = "\n".join(
        [
            "请生成一张 16:9 的电影感二维动漫关键帧。",
            "",
            "这是整个故事中已经完成结构化规划的一个指定镜头，只生成当前镜头；"
            "不要重新改编整个故事，也不要增加新的剧情事件。",
            "",
            f"镜头：{shot.title}",
            "角色：",
            *character_lines,
            "",
            (
                f"场景：{scene.name}。{scene.description}；时间：{scene.time}；"
                f"光线：{scene.lighting}；一致性要求：{scene.consistency_prompt}"
            ),
            f"当前画面与动作、空间关系：{shot.visual_description}",
            f"构图与镜头意图：{shot.camera}",
            f"补充视觉规划：{shot.image_prompt}",
            "",
            "保持角色外观与本项目其他镜头一致。画面应明确表达当前 Shot 的动作意图。"
            "不要让人物无理由面对镜头站立。不要增加新角色或改变剧情的道具。"
            "不要生成文字、水印或 UI。",
        ]
    )
    return {
        "shot_id": shot.id,
        "shot_title": shot.title,
        "adapter": EXTERNAL_PROMPT_ADAPTER_ID,
        "prompt": prompt,
        "source_fields": {
            "characters": [
                {
                    "id": item.id,
                    "name": item.name,
                    "role": item.role,
                    "appearance": item.appearance,
                    "costume": item.costume,
                    "consistency_prompt": item.consistency_prompt,
                }
                for item in characters
            ],
            "scene": {
                "id": scene.id,
                "name": scene.name,
                "description": scene.description,
                "time": scene.time,
                "lighting": scene.lighting,
                "consistency_prompt": scene.consistency_prompt,
            },
            "visual_description": shot.visual_description,
            "camera": shot.camera,
            "image_prompt": shot.image_prompt,
        },
        "lineage": {
            "project_story_present": bool(project_story.strip()),
            "structured_script_schema": script.schema_version,
            "selected_shot_id": shot.id,
        },
    }


def validate_external_filename(filename: str) -> str:
    normalized = filename.strip()
    if (
        not normalized
        or len(normalized) > 255
        or "\x00" in normalized
        or "/" in normalized
        or "\\" in normalized
        or Path(normalized).name != normalized
        or normalized in {".", ".."}
    ):
        raise ExternalImageError("原始文件名无效或包含路径跳转。")
    return normalized


def image_signature(path: Path) -> tuple[str, str]:
    try:
        prefix = path.read_bytes()[:16]
    except OSError as exc:
        raise ExternalImageError("无法读取上传图片。") from exc
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    raise ExternalImageError("只接受内容可识别的 PNG 或 JPEG 图片。")


def inspect_external_image(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExternalImageError("图片文件不存在。")
    size = path.stat().st_size
    if size <= 0:
        raise ExternalImageError("图片文件为空。")
    if size > MAX_EXTERNAL_IMAGE_BYTES:
        raise ExternalImageError("图片文件不能超过 10MB。")
    extension, mime_type = image_signature(path)
    try:
        tools = resolve_media_tools()
        probe = ffprobe_json(tools, path)
        streams = probe.get("streams")
        if not isinstance(streams, list):
            raise ValueError("missing streams")
        videos = [item for item in streams if item.get("codec_type") == "video"]
        if len(videos) != 1:
            raise ValueError("expected one image stream")
        video = videos[0]
        codec = str(video.get("codec_name") or "")
        expected_codec = "png" if extension == ".png" else "mjpeg"
        width = int(video["width"])
        height = int(video["height"])
        if codec != expected_codec or width <= 0 or height <= 0:
            raise ValueError("invalid codec or dimensions")
        if (
            width > MAX_EXTERNAL_IMAGE_SIDE
            or height > MAX_EXTERNAL_IMAGE_SIDE
            or width * height > MAX_EXTERNAL_IMAGE_PIXELS
        ):
            raise ValueError("image dimensions exceed safety bounds")
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
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=60,
        )
    except (KeyError, TypeError, ValueError, OSError, MediaToolError) as exc:
        raise ExternalImageError("图片无法完整解码或尺寸超出安全范围。") from exc
    return {
        "format": extension.removeprefix("."),
        "mime_type": mime_type,
        "codec_name": codec,
        "width": width,
        "height": height,
        "size_bytes": size,
    }


def validate_image_asset_file(
    *, data_dir: Path, project_dir: Path, asset: Asset
) -> tuple[Path, dict[str, Any]]:
    stored = Path(asset.file_path)
    if stored.is_absolute():
        raise ExternalImageError("图片资产路径必须是相对路径。")
    candidate = (Path(data_dir).resolve() / stored).resolve()
    try:
        candidate.relative_to(Path(project_dir).resolve())
    except ValueError as exc:
        raise ExternalImageError("图片资产路径越过当前项目目录。") from exc
    probe = inspect_external_image(candidate)
    if sha256_file(candidate) != asset.sha256:
        raise ExternalImageError("图片资产 SHA256 与数据库记录不一致。")
    return candidate, probe


def serialize_image_asset(asset: Asset) -> dict[str, Any]:
    metadata = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
    return {
        "asset_id": asset.id,
        "project_id": asset.project_id,
        "shot_id": metadata.get("shot_id"),
        "database_shot_id": asset.shot_id,
        "asset_type": asset.asset_type,
        "provider_id": asset.provider_id,
        "source_type": asset.source_type,
        "generation_mode": metadata.get("generation_mode"),
        "external_source_type": metadata.get("external_source_type"),
        "provider_hint": metadata.get("provider_hint"),
        "original_filename": metadata.get("original_filename"),
        "sha256": asset.sha256,
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "size_bytes": metadata.get("size_bytes"),
        "imported_at": metadata.get("imported_at"),
        "exported_prompt": metadata.get("exported_prompt"),
        "image_url": f"/api/projects/{asset.project_id}/assets/{asset.id}/content",
    }

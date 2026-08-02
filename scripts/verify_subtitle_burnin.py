"""完整解码短片并抽取每个镜头中点帧，辅助人工确认烧录字幕。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.media import (  # noqa: E402
    MediaToolError,
    decode_media_fully,
    extract_shot_midpoint_frames,
    resolve_media_tools,
)
from backend.app.media.ffmpeg import ffprobe_json  # noqa: E402


def _path_from_manifest(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise MediaToolError("Manifest 缺少有效的 subtitle_text_path")
    candidate = Path(value)
    return candidate if candidate.is_absolute() else ROOT / candidate


def _compact_text(value: str) -> str:
    return "".join(value.replace("\r\n", "\n").replace("\r", "\n").split())


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MediaToolError(f"Manifest 不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"Manifest 不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise MediaToolError("Manifest 顶层必须为对象")
    return payload


def _subtitle_trace(manifest: dict[str, Any]) -> tuple[list[float], list[dict[str, Any]]]:
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise MediaToolError("Manifest 缺少 shots")

    durations: list[float] = []
    trace: list[dict[str, Any]] = []
    for index, raw_shot in enumerate(shots, start=1):
        if not isinstance(raw_shot, dict):
            raise MediaToolError(f"Manifest shots[{index - 1}] 必须为对象")
        try:
            duration = float(raw_shot["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaToolError(f"第 {index} 个镜头缺少有效时长") from exc
        if duration <= 0:
            raise MediaToolError(f"第 {index} 个镜头时长必须大于 0")

        narration = raw_shot.get("narration", raw_shot.get("subtitle"))
        if not isinstance(narration, str) or not narration.strip():
            raise MediaToolError(f"第 {index} 个镜头缺少旁白")
        subtitle_path = _path_from_manifest(raw_shot.get("subtitle_text_path"))
        try:
            subtitle_bytes = subtitle_path.read_bytes()
            subtitle_text = subtitle_bytes.decode("utf-8")
        except FileNotFoundError as exc:
            raise MediaToolError(f"字幕文件不存在：{subtitle_path}") from exc
        except UnicodeDecodeError as exc:
            raise MediaToolError(f"字幕文件不是有效 UTF-8：{subtitle_path}") from exc
        if b"\r" in subtitle_bytes:
            raise MediaToolError(f"字幕文件不是 LF 换行：{subtitle_path}")
        if _compact_text(subtitle_text) != _compact_text(narration):
            raise MediaToolError(f"字幕文件内容与镜头旁白不一致：{subtitle_path}")
        if raw_shot.get("subtitle_rendering") != "burned_in":
            raise MediaToolError(f"第 {index} 个镜头未声明 subtitle_rendering=burned_in")
        subtitle_filter = raw_shot.get("subtitle_filter")
        if not isinstance(subtitle_filter, str) or "textfile=" not in subtitle_filter:
            raise MediaToolError(f"第 {index} 个镜头未记录有效 textfile 字幕滤镜")

        durations.append(duration)
        trace.append(
            {
                "shot_index": index,
                "shot_id": raw_shot.get("shot_id"),
                "narration": narration,
                "subtitle_text_path": str(subtitle_path.resolve()),
                "subtitle_rendering": "burned_in",
                "font_path": raw_shot.get("font_path"),
                "subtitle_filter": subtitle_filter,
                "utf8_lf_ok": True,
            }
        )
    return durations, trace


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--video",
        type=Path,
        default=ROOT / "data" / "generated" / "m1" / "paper_crane_night_flight.mp4",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "generated" / "m1" / "manifest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "generated" / "subtitle-check",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    video_path = args.video.resolve()
    manifest_path = args.manifest.resolve()
    output_dir = (args.output_dir / video_path.stem).resolve()
    try:
        manifest = _load_manifest(manifest_path)
        durations, subtitle_trace = _subtitle_trace(manifest)
        tools = resolve_media_tools()
        command_log: list[str] = []
        probe = ffprobe_json(tools, video_path, command_log=command_log)
        decode = decode_media_fully(tools, video_path, command_log=command_log)
        frames = extract_shot_midpoint_frames(
            tools,
            video_path,
            shot_durations=durations,
            output_dir=output_dir,
            filename_prefix=video_path.stem,
            command_log=command_log,
        )
        report = {
            "status": "PASS",
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "video_path": str(video_path),
            "manifest_path": str(manifest_path),
            "full_decode": decode,
            "ffprobe": probe,
            "shots": subtitle_trace,
            "midpoint_frames": frames,
            "safe_command_log": command_log,
            "visual_confirmation": (
                "中点帧已生成；烧录文字是否可见须以人工查看帧为准，"
                "本脚本不以 OCR 作为主要验收。"
            ),
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "subtitle_check.report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print("SUBTITLE CHECK PASS")
        print(
            json.dumps(
                {
                    "video_path": str(video_path),
                    "report_path": str(report_path),
                    "frame_paths": [item["frame_path"] for item in frames],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (MediaToolError, OSError, ValueError) as exc:
        print(f"SUBTITLE CHECK FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""独立验证 M1 MP4、fixture 与追溯清单。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.media.ffmpeg import (  # noqa: E402
    MediaToolError,
    resolve_media_tools,
    sha256_file,
    verify_media,
)
from backend.app.media.mock_pipeline import load_script_fixture  # noqa: E402


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MediaToolError(message)


def main() -> int:
    output_dir = ROOT / "data" / "generated" / "m1"
    output_path = output_dir / "paper_crane_night_flight.mp4"
    manifest_path = output_dir / "manifest.json"
    fixture_path = ROOT / "fixtures" / "paper-crane" / "script.v1.json"

    try:
        fixture = load_script_fixture(fixture_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _require(manifest.get("manifest_version") == "m1.manifest.v1", "manifest 版本错误")
        _require(manifest.get("fixture_version") == "script.v1", "fixture 版本追溯错误")
        _require(manifest.get("shot_count") == 4, "manifest 必须记录 4 个镜头")
        _require(len(manifest.get("shots", [])) == 4, "manifest shots 数组必须为 4 项")
        _require(len(fixture["shots"]) == 4, "fixture 必须为 4 个镜头")
        _require(manifest.get("pipeline", {}).get("provider_id") == "mock", "provider_id 必须为 mock")
        _require(
            manifest.get("pipeline", {}).get("source_type") == "DETERMINISTIC_FALLBACK",
            "source_type 必须为 DETERMINISTIC_FALLBACK",
        )
        font_path = Path(manifest.get("pipeline", {}).get("chinese_font_path", ""))
        _require(font_path.is_file(), f"manifest 记录的字体不存在：{font_path}")
        _require(output_path.is_file() and output_path.stat().st_size > 0, "M1 MP4 不存在或为空")

        actual_digest = sha256_file(output_path)
        _require(
            manifest.get("output", {}).get("sha256") == actual_digest,
            "MP4 SHA-256 与 manifest 不一致",
        )
        tools = resolve_media_tools()
        validation = verify_media(
            tools,
            output_path,
            min_duration=20.0,
            max_duration=40.0,
        )
        _require(
            abs(float(validation["duration_seconds"]) - 28.0) <= 0.50,
            f"M1 时长不是约 28 秒：{validation['duration_seconds']}",
        )
        _require(validation == manifest.get("ffprobe_validation"), "ffprobe 摘要与 manifest 不一致")
        for fixture_shot, manifest_shot in zip(fixture["shots"], manifest["shots"], strict=True):
            _require(fixture_shot["shot_id"] == manifest_shot.get("shot_id"), "镜头 ID 追溯错误")
            _require(fixture_shot["subtitle_text"] == manifest_shot.get("subtitle"), "字幕追溯错误")
            _require(manifest_shot.get("provider_id") == "mock", "镜头 provider_id 错误")
            _require(
                manifest_shot.get("source_type") == "DETERMINISTIC_FALLBACK",
                "镜头 source_type 错误",
            )

        print("M1 VERIFY PASS")
        print(
            json.dumps(
                {
                    "output_path": str(output_path),
                    "manifest_path": str(manifest_path),
                    "sha256": actual_digest,
                    "font_path": str(font_path),
                    "validation": validation,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (FileNotFoundError, json.JSONDecodeError, MediaToolError, OSError, ValueError) as exc:
        print(f"M1 VERIFY FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.media import (
    MediaToolError,
    decode_media_fully,
    extract_shot_midpoint_frames,
    render_image_project_short,
    resolve_media_tools,
    verify_media,
)
from backend.app.media.ffmpeg import run_command, sha256_file


PROVIDER_ID = "comfyui-animagine-xl-4"
MODEL_ID = "cagliostrolab/animagine-xl-4.0"


def _make_png(path: Path, color: str) -> None:
    tools = resolve_media_tools()
    path.parent.mkdir(parents=True, exist_ok=True)
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
            f"color=c={color}:s=1024x576:d=1",
            "-frames:v",
            "1",
            "-update",
            "1",
            path,
        ],
        timeout_seconds=60,
    )


def _shots() -> list[dict]:
    durations = (7.0, 7.0, 6.0)
    return [
        {
            "shot_id": f"shot_{index:02d}",
            "sequence_no": index,
            "title": f"真实关键帧镜头 {index}",
            "visual_description": f"第 {index} 张真实动漫关键帧。",
            "subtitle_text": f"第 {index} 镜，真实图片经过平滑运镜并保留中文字幕。",
            "duration_seconds": duration,
            "provider_id": PROVIDER_ID,
            "source_type": "REAL_LOCAL_MODEL",
            "script_provider_id": "reused",
            "generation_parameters": {
                "visual_provider_id": PROVIDER_ID,
                "image_source_type": "REAL_LOCAL_MODEL",
            },
        }
        for index, duration in enumerate(durations, start=1)
    ]


def _keyframes(tmp_path: Path) -> list[dict]:
    colors = ("0x183f7a", "0x6c367c", "0xc36b52")
    output: list[dict] = []
    for index, color in enumerate(colors, start=1):
        image = tmp_path / "images" / f"shot-{index:02d}.png"
        workflow = tmp_path / "images" / f"shot-{index:02d}.workflow.json"
        trace = tmp_path / "images" / f"shot-{index:02d}.result.json"
        _make_png(image, color)
        workflow.write_text(json.dumps({"shot": index}), encoding="utf-8")
        trace.write_text(json.dumps({"success": True}), encoding="utf-8")
        output.append(
            {
                "shot_id": f"shot_{index:02d}",
                "provider_id": PROVIDER_ID,
                "source_type": "REAL_LOCAL_MODEL",
                "model_id": MODEL_ID,
                "model_sha256": "6" * 64,
                "image_path": str(image),
                "image_sha256": sha256_file(image),
                "width": 1024,
                "height": 576,
                "seed": 20260802 + index,
                "positive_prompt": f"masterpiece, original character, shot {index}",
                "negative_prompt": "text, watermark, low quality",
                "generation_seconds": 1.25 + index,
                "workflow_path": str(workflow),
                "trace_path": str(trace),
                "warnings": [],
            }
        )
    return output


def test_real_png_keyframes_render_complete_traced_video(tmp_path: Path) -> None:
    shots = _shots()
    keyframes = _keyframes(tmp_path)
    output_dir = tmp_path / "export"

    rendered = render_image_project_short(
        root=tmp_path,
        project_id="m4-media-test",
        project_title="M4 真实图片媒体测试",
        shots=shots,
        keyframes=keyframes,
        output_dir=output_dir,
        output_filename="real_keyframes.mp4",
        generation_context={
            "script_source_job_id": "source-script-job",
            "providers": {
                "script_provider": "reused",
                "image_provider": PROVIDER_ID,
                "audio_provider": "mock",
                "video_source_type": "FFMPEG_KEYFRAME_MOTION",
            },
        },
    )

    video_path = Path(rendered["output_path"])
    manifest_path = Path(rendered["manifest_path"])
    assert video_path.is_file() and video_path.stat().st_size > 0
    assert manifest_path.is_file() and manifest_path.stat().st_size > 0
    assert rendered["sha256"] == sha256_file(video_path)

    tools = resolve_media_tools()
    verification = verify_media(
        tools,
        video_path,
        expected_width=1280,
        expected_height=720,
        expected_fps=24.0,
        planned_duration_seconds=20.0,
    )
    assert verification["video_codec"] == "h264"
    assert verification["audio_codec"] == "aac"
    assert verification["pixel_format"] == "yuv420p"
    assert verification["duration_validation"] in {
        "passed_exactly",
        "passed_with_media_tolerance",
    }
    assert decode_media_fully(tools, video_path)["decode_ok"] is True

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "m4.real-image-export.v1"
    assert manifest["image_provider"] == PROVIDER_ID
    assert manifest["audio_provider"] == "mock"
    assert manifest["video_source_type"] == "FFMPEG_KEYFRAME_MOTION"
    assert manifest["generation_context"]["script_source_job_id"] == (
        "source-script-job"
    )
    assert manifest["pipeline"]["source_type"] == (
        "REAL_IMAGE_KEYFRAME_FFMPEG_MOTION"
    )
    assert manifest["pipeline"]["model_weights_required"] is True
    assert manifest["pipeline"]["subtitle_rendering"] == "burned_in"
    assert manifest["media_spec"]["planned_duration_seconds"] == 20.0
    assert manifest["media_spec"]["encoded_duration_seconds"] == pytest.approx(
        verification["encoded_duration_seconds"]
    )
    assert manifest["media_spec"]["duration_tolerance_seconds"] > 0

    for expected, shot in zip(keyframes, manifest["shots"], strict=True):
        traced = shot["keyframe"]
        assert shot["provider_id"] == PROVIDER_ID
        assert shot["source_type"] == "REAL_LOCAL_MODEL"
        assert shot["subtitle_rendering"] == "burned_in"
        assert shot["keyframe_sha256"] == expected["image_sha256"]
        assert traced["image_sha256"] == expected["image_sha256"]
        assert traced["provider_id"] == PROVIDER_ID
        assert traced["model_id"] == MODEL_ID
        assert traced["seed"] == expected["seed"]
        assert (traced["width"], traced["height"]) == (1024, 576)
        assert traced["workflow_path"].endswith(".workflow.json")
        assert traced["trace_path"].endswith(".result.json")

    render_commands = [
        command
        for command in manifest["safe_command_log"]
        if "-loop 1" in command and ".png" in command
    ]
    assert len(render_commands) == 3
    assert all("zoompan=" in command for command in render_commands)
    assert all("textfile=" in command for command in render_commands)
    assert all("SHOT " not in command for command in render_commands)
    assert all("RAINY WINDOW" not in command for command in render_commands)
    assert all("GLOWING FLIGHT" not in command for command in render_commands)
    assert all("ROOFTOPS AND CLOUDS" not in command for command in render_commands)
    assert all("MOCK VISUAL / FFMPEG MOTION" not in command for command in render_commands)
    for keyframe in keyframes:
        assert sum(str(Path(keyframe["image_path"])) in item for item in render_commands) == 1

    frames = extract_shot_midpoint_frames(
        tools,
        video_path,
        shot_durations=[7.0, 7.0, 6.0],
        output_dir=tmp_path / "midpoints",
        filename_prefix="real-keyframe",
    )
    assert len(frames) == 3
    assert len({sha256_file(Path(item["frame_path"])) for item in frames}) == 3


def test_real_image_renderer_rejects_missing_mismatched_and_mock_keyframes(
    tmp_path: Path,
) -> None:
    shots = _shots()
    keyframes = _keyframes(tmp_path)
    common = {
        "root": tmp_path,
        "project_id": "invalid-real-media",
        "project_title": "无效真实图片测试",
        "shots": shots,
        "output_dir": tmp_path / "invalid-export",
    }

    with pytest.raises(MediaToolError, match="镜头数必须与剧本镜头数一致"):
        render_image_project_short(**common, keyframes=keyframes[:2])

    bad_sha = [dict(item) for item in keyframes]
    bad_sha[0]["image_sha256"] = "0" * 64
    with pytest.raises(MediaToolError, match="SHA-256 不符"):
        render_image_project_short(**common, keyframes=bad_sha)

    wrong_size = [dict(item) for item in keyframes]
    wrong_size[0]["width"] = 896
    with pytest.raises(MediaToolError, match="尺寸不符"):
        render_image_project_short(**common, keyframes=wrong_size)

    mock_mixed = [dict(item) for item in keyframes]
    mock_mixed[0]["provider_id"] = "mock"
    with pytest.raises(MediaToolError, match="禁止使用 Mock"):
        render_image_project_short(**common, keyframes=mock_mixed)

    assert not (tmp_path / "invalid-export" / "shots" / "shot_01.mp4").exists()

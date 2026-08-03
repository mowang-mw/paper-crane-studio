from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from backend.app.media import (
    MediaToolError,
    decode_media_fully,
    render_real_audio_project_short,
    resolve_media_tools,
    validate_expected_encoded_duration,
    validate_planned_encoded_duration,
    verify_media,
)
from backend.app.media.ffmpeg import run_command, sha256_file


IMAGE_PROVIDER_ID = "comfyui-animagine-xl-4"
AUDIO_PROVIDER_ID = "qwen3-tts-0.6b-customvoice"
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_SHA256 = "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb"


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
            f"color=c={color}:s=512x288:d=1",
            "-frames:v",
            "1",
            "-update",
            "1",
            path,
        ],
        timeout_seconds=60,
    )


def _make_wav(path: Path, duration: float, frequency: float) -> None:
    sample_rate = 24_000
    frame_count = int(round(sample_rate * duration))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = int(4_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", value))
        output.writeframes(frames)


def _fixtures(tmp_path: Path) -> tuple[list[dict], list[dict], list[dict], dict]:
    source_durations = (7.0, 7.0, 6.0)
    audio_durations = (1.0, 7.3, 1.25)
    narrations = (
        "少女翻开发光画册。",
        "蓝色鲸鱼从书页中游出，带她穿过寂静星光。",
        "黎明到来，城市恢复原样。",
    )
    colors = ("0x173d78", "0x633a82", "0xb46762")
    shots: list[dict] = []
    keyframes: list[dict] = []
    audio_assets: list[dict] = []
    timing_shots: list[dict] = []
    for index, (source_duration, audio_duration, narration, color) in enumerate(
        zip(source_durations, audio_durations, narrations, colors, strict=True),
        start=1,
    ):
        shot_id = f"shot{index}"
        image = tmp_path / "source" / "images" / f"shot-{index:02d}.png"
        image_trace = image.with_suffix(".result.json")
        image_workflow = image.with_suffix(".workflow.json")
        _make_png(image, color)
        image_trace.write_text("{}\n", encoding="utf-8")
        image_workflow.write_text("{}\n", encoding="utf-8")
        audio = tmp_path / "job" / "audio" / f"shot-{index:02d}.wav"
        audio_trace = audio.with_suffix(".result.json")
        _make_wav(audio, audio_duration, 220.0 + index * 55)
        audio_trace.write_text("{}\n", encoding="utf-8")
        shots.append(
            {
                "shot_id": shot_id,
                "sequence_no": index,
                "title": f"镜头 {index}",
                "visual_description": f"第 {index} 个真实动漫关键帧。",
                "subtitle_text": narration,
                "duration_seconds": source_duration,
                "provider_id": IMAGE_PROVIDER_ID,
                "source_type": "REAL_LOCAL_MODEL",
                "generation_parameters": {},
            }
        )
        keyframes.append(
            {
                "shot_id": shot_id,
                "provider_id": IMAGE_PROVIDER_ID,
                "source_type": "REAL_LOCAL_MODEL",
                "model_id": "cagliostrolab/animagine-xl-4.0",
                "model_sha256": "6" * 64,
                "image_path": str(image),
                "image_sha256": sha256_file(image),
                "width": 512,
                "height": 288,
                "seed": 20260802 + index,
                "positive_prompt": f"masterpiece, original anime shot {index}",
                "negative_prompt": "text, watermark",
                "generation_seconds": 1.0,
                "workflow_path": str(image_workflow),
                "trace_path": str(image_trace),
                "warnings": [],
            }
        )
        audio_assets.append(
            {
                "provider_id": AUDIO_PROVIDER_ID,
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "shot_id": shot_id,
                "speaker": "Serena",
                "language": "Chinese",
                "text": narration,
                "audio_path": str(audio),
                "sample_rate": 24_000,
                "channels": 1,
                "duration_seconds": audio_duration,
                "generation_seconds": 2.0 + index,
                "real_time_factor": (2.0 + index) / audio_duration,
                "audio_sha256": sha256_file(audio),
                "model_sha256": MODEL_SHA256,
                "trace_path": str(audio_trace),
                "warnings": [],
                "reused": False,
            }
        )
        raw_duration = max(source_duration, audio_duration + 0.20 + 0.35)
        rendered_duration = math.ceil(raw_duration * 24 - 1e-9) / 24
        timing_shots.append(
            {
                "shot_id": shot_id,
                "source_shot_duration": source_duration,
                "audio_duration": audio_duration,
                "lead_in_seconds": 0.20,
                "lead_out_seconds": 0.35,
                "rendered_shot_duration": rendered_duration,
                "extended_by_seconds": rendered_duration - source_duration,
                "extension_reason": (
                    "audio_plus_padding_exceeds_source_shot"
                    if rendered_duration > source_duration
                    else "source_shot_duration_sufficient"
                ),
            }
        )
    timing_plan = {"plan_version": "m5.media-timing.v1", "shots": timing_shots}
    return shots, keyframes, audio_assets, timing_plan


def test_expected_media_duration_can_exceed_script_business_limit() -> None:
    accepted = validate_expected_encoded_duration(
        expected_duration_seconds=42.0,
        encoded_duration_seconds=42.021333,
        video_fps=24.0,
        audio_sample_rate=48_000,
    )
    assert accepted["duration_validation"] == "passed_with_media_tolerance"
    with pytest.raises(MediaToolError, match="剧本计划时长越界"):
        validate_planned_encoded_duration(
            planned_duration_seconds=42.0,
            encoded_duration_seconds=42.021333,
            video_fps=24.0,
            audio_sample_rate=48_000,
        )


def test_real_audio_renderer_pads_short_audio_and_extends_long_audio(
    tmp_path: Path,
) -> None:
    shots, keyframes, audio_assets, timing_plan = _fixtures(tmp_path)
    timing_path = tmp_path / "job" / "timing_plan.json"
    timing_path.write_text(
        json.dumps(timing_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_hashes = {
        item["shot_id"]: sha256_file(Path(item["audio_path"]))
        for item in audio_assets
    }

    rendered = render_real_audio_project_short(
        root=tmp_path,
        project_id="m5-media-test",
        project_title="M5 真实旁白媒体测试",
        shots=shots,
        keyframes=keyframes,
        audio_assets=audio_assets,
        timing_plan=timing_plan,
        timing_plan_path=timing_path,
        output_dir=tmp_path / "export",
        output_filename="real_audio.mp4",
        width=320,
        height=180,
        fps=24,
        provider_id=AUDIO_PROVIDER_ID,
        generation_context={
            "source_script_job_id": "source-script-job",
            "source_image_job_id": "source-image-job",
            "providers": {
                "script_provider": "reused",
                "image_provider": IMAGE_PROVIDER_ID,
                "audio_provider": AUDIO_PROVIDER_ID,
            },
        },
    )

    output_path = Path(rendered["output_path"])
    manifest_path = Path(rendered["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output_path.is_file() and output_path.stat().st_size > 0
    assert rendered["sha256"] == sha256_file(output_path)
    assert manifest["manifest_version"] == "m5.real-audio-export.v1"
    assert manifest["script_provider"] == "reused"
    assert manifest["image_provider"] == IMAGE_PROVIDER_ID
    assert manifest["audio_provider"] == AUDIO_PROVIDER_ID
    assert manifest["speaker"] == "Serena"
    assert manifest["language"] == "Chinese"
    assert manifest["pipeline"]["mock_audio_used"] is False
    assert manifest["pipeline"]["audio_speed_changed"] is False
    assert manifest["pipeline"]["audio_truncated"] is False

    expected_rendered = sum(
        item["rendered_shot_duration"] for item in timing_plan["shots"]
    )
    assert rendered["source_planned_duration_seconds"] == 20.0
    assert rendered["rendered_planned_duration_seconds"] == pytest.approx(
        expected_rendered
    )
    assert expected_rendered > 20.0
    media = manifest["media_spec"]
    assert media["source_planned_duration_seconds"] == 20.0
    assert media["rendered_planned_duration_seconds"] == pytest.approx(
        expected_rendered
    )
    assert media["timing_plan_path"].endswith("job/timing_plan.json")

    tools = resolve_media_tools()
    verification = verify_media(
        tools,
        output_path,
        expected_width=320,
        expected_height=180,
        expected_fps=24.0,
        expected_duration_seconds=expected_rendered,
    )
    assert verification["video_codec"] == "h264"
    assert verification["audio_codec"] == "aac"
    assert verification["audio_sample_rate"] == 48_000
    assert verification["audio_channels"] == 2
    assert decode_media_fully(tools, output_path)["decode_ok"] is True

    assert len(manifest["shots"]) == 3
    second = manifest["shots"][1]
    assert second["source_shot_duration"] == 7.0
    assert second["audio_duration"] == 7.3
    assert second["rendered_shot_duration"] == pytest.approx(7.875)
    assert second["extended_by_seconds"] == pytest.approx(0.875)
    assert second["subtitle_start_seconds"] == 0.2
    assert second["subtitle_end_seconds"] == 7.5
    for asset, shot in zip(audio_assets, manifest["shots"], strict=True):
        assert shot["audio_provider"] == AUDIO_PROVIDER_ID
        assert shot["audio_model_id"] == MODEL_ID
        assert shot["audio_model_revision"] == MODEL_REVISION
        assert shot["audio_source_type"] == "REAL_LOCAL_TTS"
        assert shot["audio_text"] == shot["narration"]
        assert shot["audio_sha256"] == source_hashes[asset["shot_id"]]
        assert sha256_file(Path(asset["audio_path"])) == source_hashes[asset["shot_id"]]
        assert shot["subtitle_rendering"] == "burned_in"
        assert "textfile=" in shot["subtitle_filter"]
        assert "enable='between(t," in shot["subtitle_filter"]

    render_commands = [
        command
        for command in manifest["safe_command_log"]
        if "-loop 1" in command and ".wav" in command and ".png" in command
    ]
    assert len(render_commands) == 3
    assert all("adelay=200:all=1" in command for command in render_commands)
    assert all("apad=whole_dur=" in command for command in render_commands)
    assert all("aresample=48000" in command for command in render_commands)
    assert all("atempo=" not in command for command in render_commands)
    assert all("-shortest" not in command for command in render_commands)


def test_real_audio_renderer_enforces_configured_rendered_duration_limit(
    tmp_path: Path,
) -> None:
    shots, keyframes, audio_assets, timing_plan = _fixtures(tmp_path)
    with pytest.raises(MediaToolError, match="AUDIO_TIMING_EXCEEDS_LIMIT"):
        render_real_audio_project_short(
            root=tmp_path,
            project_id="m5-media-limit",
            project_title="M5 时长上限",
            shots=shots,
            keyframes=keyframes,
            audio_assets=audio_assets,
            timing_plan=timing_plan,
            output_dir=tmp_path / "limit-export",
            width=320,
            height=180,
            fps=24,
            provider_id=AUDIO_PROVIDER_ID,
            max_total_duration_seconds=20.5,
        )
    assert not (tmp_path / "limit-export" / "shots" / "shot-01.mp4").exists()

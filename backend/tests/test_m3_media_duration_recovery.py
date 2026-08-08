from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import crud
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.media.ffmpeg import (
    MediaToolError,
    media_duration_tolerance_seconds,
    sha256_file,
    validate_planned_encoded_duration,
)
from backend.app.models import JobStatus
from backend.app.providers.base import ScriptProvider, ScriptResult
from backend.app.providers.llama_cpp import LlamaCppScriptProvider
from backend.app.providers.mock import MockAudioProvider, MockImageProvider
from backend.app.services.generation import GenerationService
from backend.app.worker import Worker


def _five_shot_script() -> dict[str, Any]:
    durations = [8.0, 6.0, 9.0, 7.0, 10.0]
    return {
        "schema_version": "script.v1",
        "title": "画册里的蓝鲸",
        "synopsis": "少女跟随蓝鲸完成夜航，并在黎明回到现实。",
        "characters": [
            {
                "id": "girl",
                "name": "少女",
                "role": "主角",
                "appearance": "深色短发，黑色外套",
                "personality": "好奇而勇敢",
                "costume": "黑色外套与长裙",
                "consistency_prompt": "保持深色短发和黑色外套",
            }
        ],
        "scenes": [
            {
                "id": f"scene{index}",
                "name": f"场景{index}",
                "description": f"故事节点{index}",
                "time": "黎明" if index == 5 else "夜晚",
                "lighting": "晨光" if index == 5 else "月光",
                "consistency_prompt": f"保持场景{index}空间一致",
            }
            for index in range(1, 6)
        ],
        "shots": [
            {
                "id": f"shot{index}",
                "index": index,
                "title": f"镜头{index}",
                "scene_id": f"scene{index}",
                "character_ids": ["girl"],
                "visual_description": f"少女经历故事节点{index}",
                "camera": "缓慢推进",
                "image_prompt": f"原创动漫场景{index}",
                "negative_prompt": None,
                "narration": f"少女来到第{index}个故事节点。",
                "duration_seconds": durations[index - 1],
            }
            for index in range(1, 6)
        ],
    }


@pytest.mark.parametrize(
    ("planned", "encoded", "expected_validation"),
    [
        (40.0, 40.021333, "passed_with_media_tolerance"),
        (40.0, 39.98, "passed_with_media_tolerance"),
        (28.0, 28.021333, "passed_with_media_tolerance"),
        (28.0, 28.0, "passed_exactly"),
    ],
)
def test_planned_and_encoded_duration_accept_media_quantization(
    planned: float,
    encoded: float,
    expected_validation: str,
) -> None:
    result = validate_planned_encoded_duration(
        planned_duration_seconds=planned,
        encoded_duration_seconds=encoded,
        video_fps=24.0,
        audio_sample_rate=48_000,
    )
    assert result["duration_validation"] == expected_validation
    assert result["planned_duration_seconds"] == planned
    assert result["encoded_duration_seconds"] == encoded


def test_encoded_duration_outside_frame_tolerance_fails() -> None:
    with pytest.raises(MediaToolError, match="超出媒体帧量化容差"):
        validate_planned_encoded_duration(
            planned_duration_seconds=40.0,
            encoded_duration_seconds=40.2,
            video_fps=24.0,
            audio_sample_rate=48_000,
        )


def test_planned_duration_business_limit_is_never_relaxed() -> None:
    with pytest.raises(MediaToolError, match="剧本计划时长越界"):
        validate_planned_encoded_duration(
            planned_duration_seconds=40.1,
            encoded_duration_seconds=40.1,
            video_fps=24.0,
            audio_sample_rate=48_000,
        )


def test_24fps_and_48khz_produce_explainable_tolerance() -> None:
    tolerance = media_duration_tolerance_seconds(
        video_fps=24.0,
        audio_sample_rate=48_000,
    )
    assert tolerance == pytest.approx(max(1 / 24, 1024 / 48_000) + 0.010)
    assert 0.05 <= tolerance <= 0.10


def test_generation_and_repair_prompts_require_explicit_story_ending() -> None:
    generation = LlamaCppScriptProvider._generation_messages(
        "标题",
        "开端、发展与结局。",
        None,
    )
    repair = LlamaCppScriptProvider._repair_messages(
        title="标题",
        story="开端、发展与结局。",
        invalid_output="{}",
        validation_errors=[],
        desired_shot_count=None,
        actual_shot_count=4,
    )
    generation_text = "\n".join(item["content"] for item in generation)
    repair_text = "\n".join(item["content"] for item in repair)
    for text in (generation_text, repair_text):
        assert "开端" in text
        assert "主要发展" in text
        assert "明确结局" in text
        assert "最后一镜" in text
        assert "合并相邻" in text
        assert "不得删除" in text
        assert "单张静态动漫关键帧" in text
        assert "frozen moment" in text
        assert "不要描述镜头缓缓推进" in text
        assert "空间关系必须明确、无歧义" in text
    assert "自动规划" in generation_text
    assert "3—5" in generation_text


class _NeverCalledScriptProvider(ScriptProvider):
    provider_id = "llamacpp"

    def __init__(self) -> None:
        self.calls = 0

    def generate(
        self,
        *,
        title: str,
        story: str,
        desired_shot_count: int | None = None,
    ) -> ScriptResult:
        self.calls += 1
        raise AssertionError("MEDIA_RENDER 恢复不得调用 ScriptProvider")


def test_media_render_retry_reuses_script_and_media_without_provider_call(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    script = _five_shot_script()
    story = "少女发现蓝鲸，穿过城市星光，最终在黎明看见鲸鱼回到画册。"
    with database.session() as session:
        project = crud.create_project(session, title="媒体恢复", story=story)
        source_job = crud.create_job(
            session,
            project=project,
            provider_id="llamacpp",
            request_json={
                "project_id": project.id,
                "script_provider": "llamacpp",
                "desired_shot_count": 5,
                "story_char_count": len(story),
                "output": {"width": 1280, "height": 720, "fps": 24},
            },
        )
        source_job.status = JobStatus.FAILED
        source_job.progress = 20
        project_id = project.id
        source_job_id = source_job.id
        trace_dir = (
            settings.project_dir(project_id)
            / "jobs"
            / source_job_id
            / "llm-responses"
            / "trace-id"
        )
        trace_dir.mkdir(parents=True, exist_ok=True)
        validation_report = trace_dir / "validation_report.json"
        validation_report.write_text("{}\n", encoding="utf-8")
        (trace_dir / "trace.json").write_text(
            json.dumps({"validated_script": script}, ensure_ascii=False),
            encoding="utf-8",
        )
        source_job.result_json = {
            "script_provider": "llamacpp",
            "script_source_type": "LOCAL_MODEL",
            "script_trace": {
                "provider_id": "llamacpp",
                "model": "test-model.gguf",
                "validation_report_path": str(validation_report),
                "repair_used": False,
            },
            "generation_error": {
                "code": "MEDIA_RENDER_FAILED",
                "stage": "MEDIA_RENDER",
                "summary": "旧边界误判",
            },
        }
        source_job.error_message = "MEDIA_RENDER_FAILED: 旧边界误判"
        source_output_dir = settings.project_dir(project_id) / "exports" / source_job_id
        source_output_dir.mkdir(parents=True, exist_ok=True)
        source_media = source_output_dir / f"short_{source_job_id}.part.mp4"
        source_media.write_bytes(b"already-encoded-mp4")
        session.commit()

    retry_response = client.post(f"/api/jobs/{source_job_id}/retry")
    assert retry_response.status_code == 202, retry_response.text
    retry_job_id = retry_response.json()["job_id"]
    queued = client.get(f"/api/jobs/{retry_job_id}").json()
    assert queued["request_json"]["resumed_from_stage"] == "MEDIA_RENDER"

    provider = _NeverCalledScriptProvider()
    service = GenerationService(
        script_provider=provider,
        image_provider=MockImageProvider(),
        audio_provider=MockAudioProvider(),
    )
    resume_calls = 0

    def fake_resume_renderer(**kwargs: object) -> dict[str, Any]:
        nonlocal resume_calls
        resume_calls += 1
        assert Path(str(kwargs["source_media_path"])) == source_media
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / str(kwargs["output_filename"])
        video.write_bytes(source_media.read_bytes())
        manifest = output_dir / "manifest.json"
        manifest_payload = {
            "media_spec": {
                "planned_duration_seconds": 40.0,
                "encoded_duration_seconds": 40.021333,
                "duration_delta_seconds": 0.021333,
                "duration_tolerance_seconds": 0.051667,
                "duration_validation": "passed_with_media_tolerance",
            },
            "recovery": {"media_reused": True, "reencoded": False},
        }
        manifest.write_text(
            json.dumps(manifest_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "output_path": str(video),
            "manifest_path": str(manifest),
            "sha256": sha256_file(video),
            "font_path": "test-font",
            "media_reused": True,
            "reencoded": False,
            "validation": {
                "duration_seconds": 40.021333,
                **manifest_payload["media_spec"],
            },
        }

    worker = Worker(
        settings=settings,
        database=database,
        generation_service=service,
        resume_renderer=fake_resume_renderer,
    )
    assert worker.run_once() is True
    assert provider.calls == 0
    assert resume_calls == 1

    recovered = client.get(f"/api/jobs/{retry_job_id}").json()
    assert recovered["status"] == "SUCCEEDED"
    result = recovered["result_json"]
    assert result["resumed_from_stage"] == "MEDIA_RENDER"
    assert result["resumed_from_job_id"] == source_job_id
    assert result["script_provider_calls_during_resume"] == 0
    assert result["actual_shot_count"] == 5
    assert result["planned_duration_seconds"] == 40.0
    assert result["encoded_duration_seconds"] == 40.021333
    assert result["duration_delta_seconds"] == 0.021333
    assert result["duration_tolerance_seconds"] == 0.051667
    assert result["duration_validation"] == "passed_with_media_tolerance"
    assert result["media_reused"] is True
    assert result["reencoded"] is False
    assert source_media.read_bytes() == b"already-encoded-mp4"

    original = client.get(f"/api/jobs/{source_job_id}").json()
    assert original["status"] == "FAILED"

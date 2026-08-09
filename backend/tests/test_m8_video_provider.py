from __future__ import annotations

from pathlib import Path
from typing import Sequence

from fastapi.testclient import TestClient

from backend.app import crud
from backend.app.database import Database
from backend.app.models import JobStatus
from backend.app.providers.base import (
    ScriptShot,
    VideoGenerationOptions,
    VideoGenerationRequest,
)
from backend.app.providers.mock import MockVideoProvider
from backend.app.script_schema import Character, Scene, ScriptV1, Shot
from backend.app.services.video_jobs import VIDEO_JOB_TYPE
from backend.app.worker import Worker


def _script() -> ScriptV1:
    character = Character(
        id="character_01",
        name="少女",
        role="主角",
        appearance="短发少女",
        personality="安静",
        costume="深色外套",
        consistency_prompt="同一名短发少女",
    )
    scene = Scene(
        id="scene_01",
        name="车站",
        description="雨夜车站",
        time="夜晚",
        lighting="站台灯光",
        consistency_prompt="同一座雨夜车站",
    )
    shots = [
        Shot(
            id=f"shot_{index:02d}",
            index=index,
            title=f"镜头 {index}",
            scene_id=scene.id,
            character_ids=[character.id],
            visual_description=f"少女在雨夜车站的静态关键帧 {index}",
            narration=f"这是第 {index} 个镜头。",
            duration_seconds=7.0,
            camera="中心缓慢推进",
            image_prompt=f"anime keyframe {index}",
            negative_prompt="text, watermark",
        )
        for index in range(1, 4)
    ]
    return ScriptV1(
        schema_version="script.v1",
        title="雨夜车站",
        synopsis="少女在雨夜车站等待列车。",
        characters=[character],
        scenes=[scene],
        shots=shots,
    )


def _write_source_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-png")


def _seed_source_image_job(database: Database, settings) -> tuple[str, str]:
    script = _script()
    with database.session() as session:
        project = crud.create_project(session, title=script.title, story=script.synopsis)
        project.script_json = script.model_dump(mode="json")
        source_job = crud.create_job(
            session,
            project=project,
            provider_id="comfyui-animagine-xl-4",
            job_type="GENERATE_REAL_IMAGE_VIDEO",
        )
        image_shots = []
        for shot in script.shots:
            image_path = settings.project_dir(project.id) / "source" / f"{shot.id}.png"
            _write_source_image(image_path)
            image_shots.append(
                {
                    "shot_id": shot.id,
                    "shot_index": shot.index,
                    "status": "SUCCEEDED",
                    "image_path": image_path.relative_to(settings.data_dir).as_posix(),
                    "image_asset_id": f"source-{shot.id}",
                }
            )
        source_job.status = JobStatus.SUCCEEDED
        source_job.progress = 100
        source_job.result_json = {"image_shots": image_shots}
        session.commit()
        return project.id, source_job.id


def _fake_video_runner(command: Sequence[str]) -> None:
    Path(command[-1]).write_bytes(b"deterministic-mock-mp4")


def test_mock_video_provider_is_deterministic(tmp_path: Path) -> None:
    image_path = tmp_path / "source.png"
    _write_source_image(image_path)
    shot = ScriptShot(
        provider_shot_id="shot_01",
        shot_index=1,
        title="镜头 1",
        visual_description="少女站在车站",
        narration="旁白",
        duration_seconds=7.0,
        camera="中心缓慢推进",
        image_prompt="anime station",
    )
    provider = MockVideoProvider(
        ffmpeg_path=tmp_path / "ffmpeg.exe",
        command_runner=_fake_video_runner,
    )
    options = VideoGenerationOptions(duration_seconds=2.0)
    first = provider.generate(
        request=VideoGenerationRequest(
            project_id="project",
            job_id="job-a",
            shot=shot,
            source_image_path=image_path,
            prompt="anime station",
            motion_description="center zoom",
            output_dir=tmp_path / "a",
            options=options,
        )
    )
    second = provider.generate(
        request=VideoGenerationRequest(
            project_id="project",
            job_id="job-b",
            shot=shot,
            source_image_path=image_path,
            prompt="anime station",
            motion_description="center zoom",
            output_dir=tmp_path / "b",
            options=options,
        )
    )
    assert provider.plan(shot=shot) == provider.plan(shot=shot)
    assert first.video_sha256 == second.video_sha256
    assert first.source_type == second.source_type == "MOCK"
    assert first.metadata["ai_video_generated"] is False


def test_video_job_succeeds_and_exposes_mock_assets(
    client: TestClient, database: Database, settings
) -> None:
    project_id, source_job_id = _seed_source_image_job(database, settings)
    queued = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_job_id": source_job_id, "video_provider": "mock-video"},
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    worker = Worker(
        settings=settings,
        database=database,
        video_provider_factory=lambda _settings: MockVideoProvider(
            ffmpeg_path=Path("ffmpeg.exe"), command_runner=_fake_video_runner
        ),
    )
    assert worker.run_once() is True

    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["status"] == "SUCCEEDED"
    result = payload["result_json"]
    assert result["video_provider"] == "mock-video"
    assert result["video_source_type"] == "MOCK"
    assert result["mock_video_fallback"] is True
    assert result["final_media_consumes_video"] is False
    assert len(result["video_shots"]) == 3
    assert all(item["video_url"] for item in result["video_shots"])


def test_video_provider_failure_marks_job_failed_without_success_fallback(
    client: TestClient, database: Database, settings
) -> None:
    project_id, source_job_id = _seed_source_image_job(database, settings)
    queued = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_job_id": source_job_id},
    )
    job_id = queued.json()["job_id"]

    def fail(_command: Sequence[str]) -> None:
        raise RuntimeError("synthetic video failure")

    worker = Worker(
        settings=settings,
        database=database,
        video_provider_factory=lambda _settings: MockVideoProvider(
            ffmpeg_path=Path("ffmpeg.exe"), command_runner=fail
        ),
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["status"] == "FAILED"
    assert payload["result_json"]["generation_error"]["code"] == "VIDEO_GENERATION_FAILED"
    assert payload["result_json"].get("video_shots") in (None, [])


def test_default_generation_request_keeps_m7_path_without_video_provider(
    client: TestClient,
) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "旧路径", "story": "这是一个用于确认旧路径不变的完整故事。"},
    ).json()
    queued = client.post(
        f"/api/projects/{project['id']}/generate",
        json={"script_provider": "mock", "desired_shot_count": 3},
    )
    assert queued.status_code == 202
    job = client.get(f"/api/jobs/{queued.json()['job_id']}").json()
    assert job["job_type"] == "GENERATE_SHORT_VIDEO"
    assert "video_provider" not in job["request_json"]
    assert job["job_type"] != VIDEO_JOB_TYPE

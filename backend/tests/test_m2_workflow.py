from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.api.projects import _project_directory_for_delete
from backend.app.media.ffmpeg import resolve_media_tools, sha256_file, verify_media
from backend.app.models import Asset, Export, GenerationJob, JobStatus, Project, Shot
from backend.app.worker import Worker


def _create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/projects",
        json={
            "title": "纸鹤的夜航测试",
            "story": "少女在窗边折出纸鹤，纸鹤穿过夜空，在黎明飞向远方。",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _queue_job(client: TestClient, project_id: str) -> str:
    response = client.post(f"/api/projects/{project_id}/generate")
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "QUEUED"
    assert payload["job_id"]
    return payload["job_id"]


def test_health_projects_demo_and_persistence(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {
        "service": "ok",
        "database": "ok",
        "ffmpeg_available": True,
        "ffprobe_available": True,
        "data_dir": str(settings.data_dir),
        "stage": "M3",
    }
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"

    created = _create_project(client)
    demo_response = client.post("/api/projects/demo")
    assert demo_response.status_code == 201, demo_response.text
    demo = demo_response.json()
    assert demo["title"] == "纸鹤的夜航"
    assert "纸鹤" in demo["story"]

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {created["id"], demo["id"]}

    detail = client.get(f"/api/projects/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["project"]["id"] == created["id"]
    assert detail.json()["shots"] == []
    assert detail.json()["recent_jobs"] == []
    assert detail.json()["latest_export"] is None

    # A new app/engine against the same file proves persistence beyond one app
    # object's sessions.  No in-memory SQLite shortcut is used.
    second_database = Database(str(settings.database_url))
    second_app = create_app(settings, database=second_database)
    try:
        with TestClient(second_app) as second_client:
            persisted = second_client.get(f"/api/projects/{created['id']}")
            assert persisted.status_code == 200
            assert persisted.json()["project"]["story"] == created["story"]
    finally:
        second_database.dispose()


def test_generate_only_queues_and_missing_project_is_404(client: TestClient) -> None:
    missing = client.post("/api/projects/not-a-project/generate")
    assert missing.status_code == 404

    project = _create_project(client)
    job_id = _queue_job(client, project["id"])

    # No worker has run.  Therefore the request handler must not create shots,
    # media, or exports, and the job must remain QUEUED at progress zero.
    job = client.get(f"/api/jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["status"] == "QUEUED"
    assert job.json()["progress"] == 0

    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["shots"] == []
    assert detail["latest_export"] is None
    assert detail["recent_jobs"][0]["id"] == job_id


def test_failed_job_requires_manual_retry_and_creates_new_job(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    project = _create_project(client)
    original_job_id = _queue_job(client, project["id"])

    conflict = client.post(f"/api/jobs/{original_job_id}/retry")
    assert conflict.status_code == 409

    def failing_renderer(**_kwargs: object) -> dict:
        raise RuntimeError("intentional M2 test failure")

    worker = Worker(settings=settings, database=database, renderer=failing_renderer)
    assert worker.run_once() is True

    failed = client.get(f"/api/jobs/{original_job_id}")
    assert failed.status_code == 200
    failed_payload = failed.json()
    assert failed_payload["status"] == "FAILED"
    assert "intentional M2 test failure" in failed_payload["error_message"]

    retry = client.post(f"/api/jobs/{original_job_id}/retry")
    assert retry.status_code == 202, retry.text
    retried_job_id = retry.json()["job_id"]
    assert retried_job_id != original_job_id
    assert retry.json()["status"] == "QUEUED"

    original_after_retry = client.get(f"/api/jobs/{original_job_id}").json()
    retried = client.get(f"/api/jobs/{retried_job_id}").json()
    assert original_after_retry["status"] == "FAILED"
    assert retried["status"] == "QUEUED"
    assert retried["progress"] == 0
    assert retried["request_json"]["retry_of_job_id"] == original_job_id


def test_worker_generates_verified_media_and_persists_export(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    demo = client.post("/api/projects/demo").json()
    job_id = _queue_job(client, demo["id"])

    worker = Worker(settings=settings, database=database)
    assert worker.run_once() is True
    assert worker.run_once() is False

    job = client.get(f"/api/jobs/{job_id}")
    assert job.status_code == 200
    job_payload = job.json()
    assert job_payload["status"] == "SUCCEEDED"
    assert job_payload["progress"] == 100
    assert job_payload["provider_id"] == "mock"
    assert job_payload["result_json"]["source_type"] == "DETERMINISTIC_FALLBACK"
    assert job_payload["result_json"]["script_validation_warnings"] == {
        "unused_scene_ids": [],
        "unused_character_ids": [],
    }
    assert job_payload["result_json"]["planned_duration_seconds"] == 28.0
    assert job_payload["result_json"]["encoded_duration_seconds"] == pytest.approx(
        28.021333
    )
    assert job_payload["result_json"]["duration_delta_seconds"] == pytest.approx(
        0.021333
    )
    assert 0.05 <= job_payload["result_json"]["duration_tolerance_seconds"] <= 0.10
    assert job_payload["result_json"]["duration_validation"] == (
        "passed_with_media_tolerance"
    )

    detail_response = client.get(f"/api/projects/{demo['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["project"]["status"] == "EXPORTED"
    assert [shot["shot_index"] for shot in detail["shots"]] == [1, 2, 3, 4]
    assert {shot["provider_id"] for shot in detail["shots"]} == {"mock"}
    export = detail["latest_export"]
    assert export is not None
    assert export["job_id"] == job_id
    assert 20.0 <= export["duration_seconds"] <= 40.0

    video_path = Path(settings.data_dir) / export["file_path"]
    manifest_path = Path(settings.data_dir) / export["manifest_path"]
    assert video_path.is_file() and video_path.stat().st_size > 0
    assert manifest_path.is_file() and manifest_path.stat().st_size > 0
    assert sha256_file(video_path) == export["sha256"]

    verification = verify_media(
        resolve_media_tools(),
        video_path,
        expected_width=1280,
        expected_height=720,
        expected_fps=24.0,
        min_duration=20.0,
        max_duration=40.0,
    )
    assert verification["video_codec"] == "h264"
    assert verification["audio_codec"] == "aac"
    assert verification["frame_rate"] == 24.0

    video_response = client.get(export["video_url"])
    assert video_response.status_code == 200
    assert video_response.headers["content-type"].startswith("video/mp4")
    assert video_response.content == video_path.read_bytes()

    manifest_response = client.get(export["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["project"]["id"] == demo["id"]
    assert manifest["shot_count"] == 4
    assert manifest["pipeline"]["provider_id"] == "mock"
    assert manifest["output"]["sha256"] == export["sha256"]
    assert manifest["generation_context"]["generation_job_id"] == job_id
    assert manifest["generation_context"]["script"]["fixture_version"] == "script.v1"
    assert manifest["script_validation_warnings"] == {
        "unused_scene_ids": [],
        "unused_character_ids": [],
    }
    assert manifest["media_spec"]["planned_duration_seconds"] == 28.0
    assert manifest["media_spec"]["encoded_duration_seconds"] == pytest.approx(
        28.021333
    )
    assert manifest["media_spec"]["duration_delta_seconds"] == pytest.approx(
        0.021333
    )
    assert 0.05 <= manifest["media_spec"]["duration_tolerance_seconds"] <= 0.10
    assert manifest["media_spec"]["duration_validation"] == (
        "passed_with_media_tolerance"
    )
    assert all(item.get("audio_sha256") for item in manifest["shots"])

    download_response = client.get(export["download_url"])
    assert download_response.status_code == 200
    assert "attachment" in download_response.headers["content-disposition"]
    assert download_response.content == video_path.read_bytes()

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(Project).where(Project.id == demo["id"])
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.SUCCEEDED)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(Export).where(Export.job_id == job_id)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == demo["id"])
        ) == 2


def test_export_routes_reject_wrong_project(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    """A registered export cannot be read through another project's URL."""

    project = _create_project(client)
    other = client.post("/api/projects/demo").json()
    job_id = _queue_job(client, project["id"])

    # Use a tiny deterministic fake renderer; this test covers route ownership,
    # while the previous test exercises real FFmpeg output.
    def fake_renderer(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / "fake.mp4"
        manifest = output_dir / "manifest.json"
        video.write_bytes(b"not-empty-test-media")
        manifest.write_text(json.dumps({"test": True}), encoding="utf-8")
        return {
            "output_path": str(video),
            "manifest_path": str(manifest),
            "sha256": sha256_file(video),
            "font_path": "test-font",
            "validation": {"duration_seconds": 28.0},
        }

    worker = Worker(settings=settings, database=database, renderer=fake_renderer)
    assert worker.run_once() is True
    detail = client.get(f"/api/projects/{project['id']}").json()
    export = detail["latest_export"]
    assert export["job_id"] == job_id

    wrong_video_url = (
        f"/api/projects/{other['id']}/exports/{export['id']}/video"
    )
    wrong_manifest_url = (
        f"/api/projects/{other['id']}/exports/{export['id']}/manifest"
    )
    assert client.get(wrong_video_url).status_code == 404
    assert client.get(wrong_manifest_url).status_code == 404

    # Even a compromised database row cannot use ``..`` to escape the project root.
    with database.session() as session:
        stored_export = session.get(Export, export["id"])
        assert stored_export is not None
        stored_export.file_path = "../app.db"
        session.commit()
    assert client.get(export["video_url"]).status_code == 404


def test_delete_project_removes_database_rows_and_only_its_directory(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    project = _create_project(client)
    other = client.post(
        "/api/projects",
        json={"title": "保留项目", "story": "这个项目和它的文件不能被误删。"},
    ).json()
    job_id = _queue_job(client, project["id"])

    def fake_renderer(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / "delete-test.mp4"
        manifest = output_dir / "manifest.json"
        video.write_bytes(b"delete-test-media")
        manifest.write_text(json.dumps({"delete_test": True}), encoding="utf-8")
        return {
            "output_path": str(video),
            "manifest_path": str(manifest),
            "sha256": sha256_file(video),
            "font_path": "test-font",
            "validation": {"duration_seconds": 28.0},
        }

    worker = Worker(settings=settings, database=database, renderer=fake_renderer)
    assert worker.run_once() is True
    project_directory = settings.project_dir(project["id"])
    other_directory = settings.project_dir(other["id"])
    other_directory.mkdir(parents=True, exist_ok=True)
    other_marker = other_directory / "keep.txt"
    other_marker.write_text("keep", encoding="utf-8")
    assert project_directory.is_dir()

    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    assert client.get(f"/api/projects/{other['id']}").status_code == 200
    assert not project_directory.exists()
    assert other_marker.read_text(encoding="utf-8") == "keep"

    with database.session() as session:
        assert session.get(Project, project["id"]) is None
        assert session.scalar(
            select(func.count()).select_from(Shot).where(Shot.project_id == project["id"])
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == project["id"])
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.project_id == project["id"])
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(Export).where(Export.project_id == project["id"])
        ) == 0
        assert session.get(GenerationJob, job_id) is None


def test_delete_project_rejects_missing_queued_and_running(
    client: TestClient,
    database: Database,
) -> None:
    assert client.delete("/api/projects/missing-project").status_code == 404

    queued_project = _create_project(client)
    queued_job_id = _queue_job(client, queued_project["id"])
    queued_response = client.delete(f"/api/projects/{queued_project['id']}")
    assert queued_response.status_code == 409
    assert queued_response.json()["detail"] == (
        "当前项目仍有任务正在等待或生成，请等待任务结束后再删除。"
    )
    assert client.get(f"/api/jobs/{queued_job_id}").json()["status"] == "QUEUED"

    running_project = client.post(
        "/api/projects",
        json={"title": "运行中项目", "story": "用于验证运行中任务禁止删除。"},
    ).json()
    running_job_id = _queue_job(client, running_project["id"])
    with database.session() as session:
        claimed = session.get(GenerationJob, running_job_id)
        assert claimed is not None
        claimed.status = JobStatus.RUNNING
        claimed.progress = 20
        session.commit()
    running_response = client.delete(f"/api/projects/{running_project['id']}")
    assert running_response.status_code == 409
    assert client.get(f"/api/jobs/{running_job_id}").json()["status"] == "RUNNING"


def test_project_delete_path_safety(settings: Settings) -> None:
    projects_root = (Path(settings.data_dir) / "projects").resolve()
    safe = _project_directory_for_delete(settings, "safe-project-id")
    assert safe.parent == projects_root

    for unsafe_id in ("", ".", "..", "../fixtures", "nested/project", "C:/outside"):
        try:
            _project_directory_for_delete(settings, unsafe_id)
        except ValueError:
            continue
        raise AssertionError(f"危险项目 ID 未被拒绝：{unsafe_id!r}")

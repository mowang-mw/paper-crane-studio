from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.media.ffmpeg import sha256_file
from backend.app.providers.mock import (
    MockAudioProvider,
    MockImageProvider,
    MockScriptProvider,
)
from backend.app.services.generation import GenerationService
from backend.app.worker import Worker


def _create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/projects",
        json={"title": "雨夜车站", "story": "少年跟随发光的猫走进机械花园。"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_provider_registry_and_explicit_offline_rejection(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline = {
        "provider_id": "llamacpp",
        "display_name": "本地 Qwen",
        "available": False,
        "configured": False,
        "model_id": "Qwen3-4B-Q4_K_M.gguf",
        "source_type": "LOCAL_MODEL",
        "server_version": None,
        "detail": "GGUF 模型文件不存在，请先完成 M3 模型准备。",
    }
    monkeypatch.setattr(
        "backend.app.providers.registry.check_llamacpp",
        lambda _settings: dict(offline),
    )
    monkeypatch.setattr(
        "backend.app.api.projects.check_llamacpp",
        lambda _settings: dict(offline),
    )
    image_ready = {
        "provider_id": "comfyui-animagine-xl-4",
        "display_name": "真实动漫视觉 · Animagine XL 4.0",
        "available": True,
        "configured": True,
        "model_id": "cagliostrolab/animagine-xl-4.0",
        "source_type": "LOCAL_MODEL",
        "detail": "M4-A 环境已就绪；ComfyUI 将按 Job 有界启动。",
        "requires_gpu_handoff": False,
    }
    monkeypatch.setattr(
        "backend.app.providers.registry.check_comfyui_image",
        lambda _settings: dict(image_ready),
    )
    registry = client.get("/api/providers")
    assert registry.status_code == 200, registry.text
    payload = registry.json()
    assert payload["default_script_provider"] == "mock"
    assert payload["checked_at"]
    by_id = {item["provider_id"]: item for item in payload["providers"]}
    assert by_id["mock"] == {
        "provider_id": "mock",
        "display_name": "Mock 离线",
        "available": True,
        "configured": True,
        "model_id": "mock-script.v1",
        "source_type": "MOCK",
        "server_version": None,
        "detail": "无需网络、API Key 或模型权重。",
    }
    assert by_id["llamacpp"]["available"] is False
    assert by_id["llamacpp"]["configured"] is False
    assert payload["default_image_provider"] == "mock"
    image_by_id = {
        item["provider_id"]: item for item in payload["image_providers"]
    }
    assert image_by_id["mock"]["source_type"] == "MOCK"
    assert image_by_id["comfyui-animagine-xl-4"] == image_ready

    project = _create_project(client)
    unavailable = client.post(
        f"/api/projects/{project['id']}/generate",
        json={"script_provider": "llamacpp"},
    )
    assert unavailable.status_code == 503
    generation_error = unavailable.json()["detail"]
    assert generation_error["code"] == "PROVIDER_UNAVAILABLE"
    assert generation_error["stage"] == "PROVIDER_UNAVAILABLE"
    assert "GGUF" in generation_error["summary"]
    assert generation_error["desired_shot_count"] == 4
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["recent_jobs"] == []

    queued = client.post(
        f"/api/projects/{project['id']}/generate",
        json={"script_provider": "mock"},
    )
    assert queued.status_code == 202, queued.text
    job = client.get(f"/api/jobs/{queued.json()['job_id']}").json()
    assert job["provider_id"] == "mock"
    assert job["request_json"]["script_provider"] == "mock"


def test_worker_uses_job_snapshot_instead_of_changed_default(tmp_path: Path) -> None:
    settings = Settings(
        root_dir=Path(__file__).resolve().parents[2],
        data_dir=tmp_path / "data",
        script_provider="llamacpp",
    )
    database = Database(str(settings.database_url))
    database.create_schema()
    app = create_app(settings, database=database)

    def fake_renderer(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / "provider-snapshot.mp4"
        manifest = output_dir / "manifest.json"
        video.write_bytes(b"provider-snapshot-test")
        manifest.write_text(json.dumps({"provider": "mock"}), encoding="utf-8")
        return {
            "output_path": str(video),
            "manifest_path": str(manifest),
            "sha256": sha256_file(video),
            "font_path": "test-font",
            "validation": {"duration_seconds": 28.0},
        }

    service = GenerationService(
        script_provider=MockScriptProvider(settings.root_dir),
        image_provider=MockImageProvider(),
        audio_provider=MockAudioProvider(),
    )
    try:
        with TestClient(app) as client:
            project = _create_project(client)
            queued = client.post(
                f"/api/projects/{project['id']}/generate",
                json={"script_provider": "mock"},
            )
            assert queued.status_code == 202, queued.text
            job_id = queued.json()["job_id"]

            worker = Worker(
                settings=settings,
                database=database,
                renderer=fake_renderer,
                generation_service=service,
            )
            assert worker.run_once() is True
            completed = client.get(f"/api/jobs/{job_id}").json()
            assert completed["status"] == "SUCCEEDED"
            assert completed["provider_id"] == "mock"
            assert completed["result_json"]["script_provider"] == "mock"
    finally:
        database.dispose()

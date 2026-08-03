from __future__ import annotations

import hashlib
import json
import struct
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import Asset, GenerationJob, JobStatus
from backend.app.providers.base import (
    GeneratedImageAsset,
    ImageGenerationRequest,
    ImageProvider,
    ScriptShot,
    VisualPlan,
)
from backend.app.providers.comfyui import ImageProviderError
from backend.app.worker import Worker


MODEL_ID = "cagliostrolab/animagine-xl-4.0:test"
MODEL_SHA256 = "a" * 64


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _write_png(path: Path, width: int, height: int, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scanline = b"\x00" + bytes(color) * width
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    payload += _png_chunk(b"IDAT", zlib.compress(scanline * height, level=1))
    payload += _png_chunk(b"IEND", b"")
    path.write_bytes(payload)


class _FakeGpuMonitor:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def summary(self) -> dict[str, Any]:
        return {
            "baseline_mib": 300,
            "peak_mib": 7600,
            "additional_mib": 7300,
            "sample_count": 3,
            "method": "test observation",
        }


class _FakeRealImageProvider(ImageProvider):
    provider_id = "comfyui-animagine-xl-4"
    model_id = MODEL_ID

    def __init__(self, *, fail_on_shot_index: int | None = None) -> None:
        self.fail_on_shot_index = fail_on_shot_index
        self.batch_calls = 0
        self.generated_shot_ids: list[str] = []
        self.reusable_seen: list[str] = []

    def plan(self, *, shot: ScriptShot) -> VisualPlan:
        return VisualPlan(
            provider_id=self.provider_id,
            source_type="REAL_LOCAL_MODEL",
            parameters={"seed_strategy": "base_seed_plus_shot_index"},
        )

    def generate_batch(
        self,
        *,
        requests: tuple[ImageGenerationRequest, ...],
        reusable_assets: tuple[GeneratedImageAsset, ...] = (),
        progress_callback=None,
    ) -> tuple[GeneratedImageAsset, ...]:
        self.batch_calls += 1
        reusable = {item.shot_id: item for item in reusable_assets}
        self.reusable_seen = sorted(reusable)
        completed: list[GeneratedImageAsset] = []
        session_needed = False
        for request in requests:
            candidate = reusable.get(request.shot.id)
            if candidate is not None:
                asset = replace(candidate, reused=True)
            else:
                session_needed = True
                if request.shot.index == self.fail_on_shot_index:
                    self._write_report(
                        request.output_dir,
                        completed,
                        session_started=session_needed,
                    )
                    raise ImageProviderError(
                        "IMAGE_GENERATION_FAILED",
                        "fake provider failed",
                        shot_id=request.shot.id,
                        completed_image_count=len(completed),
                    )
                asset = self._generate_one(request)
                self.generated_shot_ids.append(request.shot.id)
            completed.append(asset)
            if progress_callback is not None:
                progress_callback(len(completed), len(requests), asset)
        self._write_report(
            requests[0].output_dir,
            completed,
            session_started=session_needed,
        )
        return tuple(completed)

    def _generate_one(self, request: ImageGenerationRequest) -> GeneratedImageAsset:
        stem = f"shot-{request.shot.index:02d}"
        image = request.output_dir / f"{stem}.png"
        workflow = request.output_dir / f"{stem}.workflow.json"
        trace = request.output_dir / f"{stem}.result.json"
        positive = f"shared original character anchor, shot {request.shot.index}"
        negative = "text, watermark, low quality"
        _write_png(
            image,
            request.options.width,
            request.options.height,
            (20 * request.shot.index, 40, 90),
        )
        workflow.write_text("{}\n", encoding="utf-8")
        trace.write_text("{}\n", encoding="utf-8")
        return GeneratedImageAsset(
            provider_id=self.provider_id,
            model_id=self.model_id,
            shot_id=request.shot.id,
            image_path=image,
            width=request.options.width,
            height=request.options.height,
            seed=request.options.base_seed + request.shot.index,
            positive_prompt=positive,
            negative_prompt=negative,
            generation_seconds=float(request.shot.index),
            image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
            model_sha256=MODEL_SHA256,
            workflow_path=workflow,
            trace_path=trace,
            warnings=("test fake ComfyUI; no GPU",),
        )

    def _write_report(
        self,
        output_dir: Path,
        completed: list[GeneratedImageAsset],
        *,
        session_started: bool,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir.parent / "image_generation_report.json").write_text(
            json.dumps(
                {
                    "comfyui_start_count": 1 if session_started else 0,
                    "completed_count": len(completed),
                    "sequential_generation": True,
                    "mock_fallback": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )


def _fake_renderer(**kwargs: Any) -> dict[str, Any]:
    output_dir = Path(kwargs["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = str(kwargs.get("output_filename") or "short.mp4")
    output_path = output_dir / filename
    output_path.write_bytes(b"bounded-fake-mp4-for-worker-integration")
    planned = sum(float(item["duration_seconds"]) for item in kwargs["shots"])
    encoded = planned + 0.021333
    validation = {
        "planned_duration_seconds": planned,
        "encoded_duration_seconds": encoded,
        "duration_seconds": encoded,
        "duration_delta_seconds": 0.021333,
        "duration_tolerance_seconds": 0.051,
        "duration_validation": "passed_with_media_tolerance",
        "video_codec": "h264",
        "audio_codec": "aac",
        "frame_rate": 24.0,
        "width": 1280,
        "height": 720,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "m4.real-image-export.v1",
                "generation_context": kwargs.get("generation_context", {}),
                "script_provider": kwargs.get("generation_context", {})
                .get("providers", {})
                .get("script_provider", "mock"),
                "image_provider": kwargs["provider_id"],
                "audio_provider": "mock",
                "pipeline": {"subtitle_rendering": "burned_in"},
                "shots": kwargs.get("keyframes", []),
                "ffprobe_validation": validation,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "validation": validation,
    }


def _source_job(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> tuple[str, str]:
    project_response = client.post(
        "/api/projects",
        json={
            "title": "画册里的蓝鲸",
            "story": "少女在深夜旧书店翻开发光画册，蓝鲸游出书页；她跟随蓝鲸穿过城市，黎明时蓝鲸回到画册。",
        },
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    queue = client.post(
        f"/api/projects/{project_id}/generate",
        json={"script_provider": "mock", "desired_shot_count": 3},
    )
    assert queue.status_code == 202
    source_job_id = queue.json()["job_id"]
    worker = Worker(settings=settings, database=database, renderer=_fake_renderer)
    assert worker.run_once() is True
    assert client.get(f"/api/jobs/{source_job_id}").json()["status"] == "SUCCEEDED"
    return project_id, source_job_id


def _queue_real_job(
    client: TestClient,
    project_id: str,
    source_job_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setattr(
        "backend.app.api.projects.gpu_handoff_status",
        lambda **_kwargs: {
            "conflict": False,
            "llama_port_listening": False,
            "llama_process_detected": False,
        },
    )
    response = client.post(
        f"/api/projects/{project_id}/render-real-images",
        json={
            "source_script_job_id": source_job_id,
            "image_provider": "comfyui-animagine-xl-4",
            "base_seed": 9000,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _allow_worker_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.app.worker.gpu_handoff_status",
        lambda **_kwargs: {
            "conflict": False,
            "llama_port_listening": False,
            "llama_process_detected": False,
        },
    )
    monkeypatch.setattr(
        "backend.app.worker.GpuMemoryMonitor", lambda: _FakeGpuMonitor()
    )


def test_real_image_api_snapshot_worker_reuses_script_and_exposes_assets(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, source_job_id = _source_job(client, settings, database)
    real_job_id = _queue_real_job(client, project_id, source_job_id, monkeypatch)
    queued = client.get(f"/api/jobs/{real_job_id}").json()
    assert queued["provider_id"] == "comfyui-animagine-xl-4"
    assert queued["job_type"] == "GENERATE_REAL_IMAGE_VIDEO"
    assert queued["request_json"]["script_provider"] == "reused"
    assert queued["request_json"]["source_script_job_id"] == source_job_id
    assert queued["request_json"]["script_provider_calls_expected"] == 0
    assert queued["request_json"]["base_seed"] == 9000
    snapshot = Path(settings.data_dir) / queued["request_json"]["script_snapshot_path"]
    assert snapshot.is_file()

    provider = _FakeRealImageProvider()
    _allow_worker_gpu(monkeypatch)
    monkeypatch.setattr(
        Worker,
        "_generation_service_for",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("真实图片 Job 不得创建或调用 ScriptProvider")
        ),
    )
    worker = Worker(
        settings=settings,
        database=database,
        image_provider_factory=lambda _settings: provider,
        real_image_renderer=_fake_renderer,
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{real_job_id}").json()
    assert payload["status"] == "SUCCEEDED", payload
    result = payload["result_json"]
    assert result["script_provider"] == "reused"
    assert result["source_script_provider"] == "mock"
    assert result["script_provider_calls"] == 0
    assert result["image_provider"] == "comfyui-animagine-xl-4"
    assert result["audio_provider"] == "mock"
    assert result["mock_image_fallback"] is False
    assert result["comfyui_start_count"] == 1
    assert provider.batch_calls == 1
    assert provider.generated_shot_ids == ["shot_01", "shot_02", "shot_03"]
    assert [item["seed"] for item in result["image_shots"]] == [9001, 9002, 9003]
    assert {item["status"] for item in result["image_shots"]} == {"SUCCEEDED"}
    for item in result["image_shots"]:
        response = client.get(item["image_url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    manifest = client.get(result["manifest_url"]).json()
    assert manifest["generation_context"]["source_script_job_id"] == source_job_id
    assert manifest["generation_context"]["script"]["script_provider_calls"] == 0
    assert manifest["image_provider"] == "comfyui-animagine-xl-4"
    assert manifest["audio_provider"] == "mock"
    with database.session() as session:
        keyframes = list(
            session.scalars(
                select(Asset).where(
                    Asset.project_id == project_id,
                    Asset.asset_type == "KEYFRAME_IMAGE",
                )
            ).all()
        )
        assert len(keyframes) == 3
        assert {item.provider_id for item in keyframes} == {
            "comfyui-animagine-xl-4"
        }


def test_gpu_handoff_is_structured_and_does_not_queue_or_kill(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, source_job_id = _source_job(client, settings, database)
    process_kill_called = False

    def conflict(**_kwargs: Any) -> dict[str, bool]:
        return {
            "conflict": True,
            "llama_port_listening": True,
            "llama_process_detected": True,
        }

    monkeypatch.setattr("backend.app.api.projects.gpu_handoff_status", conflict)
    response = client.post(
        f"/api/projects/{project_id}/render-real-images",
        json={
            "source_script_job_id": source_job_id,
            "image_provider": "comfyui-animagine-xl-4",
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "GPU_HANDOFF_REQUIRED"
    assert detail["requires_qwen_shutdown"] is True
    assert process_kill_called is False
    with database.session() as session:
        real_jobs = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.project_id == project_id,
                    GenerationJob.job_type == "GENERATE_REAL_IMAGE_VIDEO",
                )
            ).all()
        )
        assert real_jobs == []


def test_failed_image_retry_reuses_completed_png_without_script_provider(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, source_job_id = _source_job(client, settings, database)
    failed_job_id = _queue_real_job(client, project_id, source_job_id, monkeypatch)
    _allow_worker_gpu(monkeypatch)
    failing = _FakeRealImageProvider(fail_on_shot_index=2)
    first_worker = Worker(
        settings=settings,
        database=database,
        image_provider_factory=lambda _settings: failing,
        real_image_renderer=_fake_renderer,
    )
    assert first_worker.run_once() is True
    failed = client.get(f"/api/jobs/{failed_job_id}").json()
    assert failed["status"] == "FAILED"
    assert failed["result_json"]["generation_error"]["failed_shot_id"] == "shot_02"
    assert failed["result_json"]["image_completed_count"] == 1
    assert failed["result_json"]["image_shots"][0]["status"] == "SUCCEEDED"
    assert failed["result_json"]["image_provider"] == "comfyui-animagine-xl-4"
    assert "export_id" not in failed["result_json"]

    retry = client.post(f"/api/jobs/{failed_job_id}/retry")
    assert retry.status_code == 202, retry.text
    retry_job_id = retry.json()["job_id"]
    retried_provider = _FakeRealImageProvider()
    monkeypatch.setattr(
        Worker,
        "_generation_service_for",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("真实图片重试不得调用 ScriptProvider")
        ),
    )
    retry_worker = Worker(
        settings=settings,
        database=database,
        image_provider_factory=lambda _settings: retried_provider,
        real_image_renderer=_fake_renderer,
    )
    assert retry_worker.run_once() is True
    retried = client.get(f"/api/jobs/{retry_job_id}").json()
    assert retried["status"] == "SUCCEEDED", retried
    assert retried_provider.reusable_seen == ["shot_01"]
    assert retried_provider.generated_shot_ids == ["shot_02", "shot_03"]
    assert retried["result_json"]["script_provider_calls"] == 0
    assert retried["result_json"]["image_shots"][0]["status"] == "REUSED"
    assert [item["seed"] for item in retried["result_json"]["image_shots"]] == [
        9001,
        9002,
        9003,
    ]

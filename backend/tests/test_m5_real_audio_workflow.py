from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any
import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import crud
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import Asset, JobStatus
from backend.app.providers.base import (
    AudioGenerationRequest,
    AudioPlan,
    AudioProvider,
    GeneratedAudioAsset,
    ScriptShot,
)
from backend.app.services.audio_jobs import (
    REAL_AUDIO_PROVIDER_ID,
    RealAudioJobError,
    inspect_pcm16_wav,
)
from backend.app.worker import Worker
from backend.tests.test_m4_real_image_workflow import (
    _FakeRealImageProvider,
    _allow_worker_gpu,
    _fake_renderer as _fake_real_image_renderer,
    _queue_real_job,
    _source_job,
)


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_SHA256 = "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb"


class _FakeGpuMonitor:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def summary(self) -> dict[str, Any]:
        return {
            "baseline_mib": 420,
            "peak_mib": 4380,
            "additional_mib": 3960,
            "sample_count": 4,
            "method": "test nvidia-smi global observation",
        }


def _write_pcm16_wav(path: Path, *, duration: float, sample_rate: int = 24_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(duration * sample_rate)
    samples = bytearray()
    for index in range(frame_count):
        value = round(6_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
        samples.extend(struct.pack("<h", value))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(bytes(samples))


class _FakeRealAudioProvider(AudioProvider):
    provider_id = REAL_AUDIO_PROVIDER_ID
    model_id = MODEL_ID
    model_revision = MODEL_REVISION
    model_license = "Apache-2.0"

    def __init__(self, *, fail_on_shot_index: int | None = None) -> None:
        self.fail_on_shot_index = fail_on_shot_index
        self.batch_calls = 0
        self.generated_shot_ids: list[str] = []
        self.reusable_seen: list[str] = []
        self.last_run_report: dict[str, Any] = {}

    def plan(self, *, shot: ScriptShot) -> AudioPlan:
        return AudioPlan(
            provider_id=self.provider_id,
            source_type="REAL_LOCAL_MODEL",
            parameters={"shot_id": shot.provider_shot_id},
        )

    def generate_batch(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...] = (),
        progress_callback=None,
    ) -> tuple[GeneratedAudioAsset, ...]:
        self.batch_calls += 1
        reusable = {item.shot_id: item for item in reusable_assets}
        self.reusable_seen = sorted(reusable)
        completed: list[GeneratedAudioAsset] = []
        generated_count = 0
        for request in requests:
            candidate = reusable.get(request.shot.id)
            if candidate is not None:
                asset = replace(candidate, reused=True)
            else:
                if request.shot.index == self.fail_on_shot_index:
                    self._write_report(request.output_dir, generated_count, len(completed))
                    raise RealAudioJobError(
                        code="TTS_GENERATION_FAILED",
                        stage="AUDIO_GENERATION",
                        summary="fake bounded TTS failure",
                        failed_shot_id=request.shot.id,
                        failed_shot_index=request.shot.index,
                        completed_audio_count=len(completed),
                        total_audio_count=len(requests),
                    )
                asset = self._generate_one(request)
                generated_count += 1
                self.generated_shot_ids.append(request.shot.id)
            completed.append(asset)
            if progress_callback is not None:
                progress_callback(len(completed), len(requests), asset)
        self._write_report(requests[0].output_dir, generated_count, len(completed))
        return tuple(completed)

    def _generate_one(self, request: AudioGenerationRequest) -> GeneratedAudioAsset:
        stem = f"shot-{request.shot.index:02d}"
        audio_path = request.output_dir / f"{stem}.wav"
        text_path = request.output_dir / f"{stem}.text.txt"
        trace_path = request.output_dir / f"{stem}.result.json"
        _write_pcm16_wav(audio_path, duration=1.0 + request.shot.index * 0.1)
        text_path.write_text(request.shot.narration, encoding="utf-8")
        technical = inspect_pcm16_wav(audio_path)
        seed = request.options.base_seed + request.shot.index
        trace = {
            "provider": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": MODEL_SHA256,
            "shot_id": request.shot.id,
            "text": request.shot.narration,
            "speaker": request.options.speaker,
            "language": request.options.language,
            "seed": seed,
            "audio_path": str(audio_path),
            "audio_sha256": technical["sha256"],
            "technical_validation": technical,
        }
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        duration = float(technical["duration_seconds"])
        return GeneratedAudioAsset(
            provider_id=self.provider_id,
            model_id=self.model_id,
            model_revision=self.model_revision,
            model_sha256=MODEL_SHA256,
            shot_id=request.shot.id,
            audio_path=audio_path,
            trace_path=trace_path,
            text=request.shot.narration,
            speaker=request.options.speaker,
            language=request.options.language,
            seed=seed,
            sample_rate=int(technical["sample_rate"]),
            channels=int(technical["channels"]),
            sample_width_bytes=int(technical["sample_width_bytes"]),
            duration_seconds=duration,
            generation_seconds=0.25,
            real_time_factor=round(0.25 / duration, 6),
            peak_amplitude=float(technical["peak_amplitude"]),
            rms=float(technical["rms"]),
            audio_sha256=str(technical["sha256"]),
            warnings=("test fake provider; no model process",),
        )

    def _write_report(
        self, output_dir: Path, generated_count: int, completed_count: int
    ) -> None:
        self.last_run_report = {
            "status": "SUCCEEDED" if self.fail_on_shot_index is None else "FAILED",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": MODEL_SHA256,
            "model_load_count": 1 if generated_count else 0,
            "generated_count": generated_count,
            "completed_count": completed_count,
            "sequential_generation": True,
            "max_audio_concurrency": 1,
            "mock_fallback": False,
            "cloud_api_used": False,
            "gpu_memory_observed": {
                "baseline_allocated_mib": 0,
                "peak_allocated_mib": 3000,
            },
        }
        report = output_dir.parent / "audio_generation_report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(self.last_run_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _fake_real_audio_renderer(**kwargs: Any) -> dict[str, Any]:
    assert kwargs["provider_id"] == REAL_AUDIO_PROVIDER_ID
    assert len(kwargs["keyframes"]) == len(kwargs["audio_assets"])
    assert all(Path(item["image_path"]).is_file() for item in kwargs["keyframes"])
    assert all(
        item["provider_id"] == REAL_AUDIO_PROVIDER_ID
        and Path(item["audio_path"]).is_file()
        for item in kwargs["audio_assets"]
    )
    assert Path(kwargs["timing_plan_path"]).is_file()
    context = kwargs["generation_context"]
    assert context["providers"]["script_provider"] == "reused"
    assert context["providers"]["image_provider_calls"] == 0
    assert context["providers"]["audio_provider"] == REAL_AUDIO_PROVIDER_ID
    assert context["mock_audio_fallback"] is False

    output_dir = Path(kwargs["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / str(kwargs["output_filename"])
    output_path.write_bytes(b"bounded-fake-m5-real-image-real-audio-mp4")
    timing = kwargs["timing_plan"]
    planned = float(timing["rendered_total_duration_seconds"])
    encoded = planned + 0.021333
    validation = {
        "planned_duration_seconds": planned,
        "expected_duration_seconds": planned,
        "encoded_duration_seconds": encoded,
        "duration_seconds": encoded,
        "duration_delta_seconds": 0.021333,
        "duration_tolerance_seconds": 0.051,
        "duration_validation": "passed_with_media_tolerance",
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 48_000,
        "frame_rate": 24.0,
        "width": 1280,
        "height": 720,
    }
    manifest_path = output_dir / "manifest.json"
    manifest = {
        "manifest_version": "m6.media-export.v1",
        "script_provider": "reused",
        "source_script_provider": context["providers"]["source_script_provider"],
        "image_provider": context["providers"]["image_provider"],
        "audio_provider": REAL_AUDIO_PROVIDER_ID,
        "audio_source_type": "REAL_LOCAL_MODEL",
        "mock_audio_fallback": False,
        "script_provider_calls": 0,
        "image_provider_calls": 0,
        "speaker": kwargs["audio_assets"][0]["speaker"],
        "language": kwargs["audio_assets"][0]["language"],
        "timing_plan": timing,
        "shots": [
            {
                **shot,
                "keyframe": keyframe,
                "audio": audio,
                "timing": timing_item,
            }
            for shot, keyframe, audio, timing_item in zip(
                kwargs["shots"],
                kwargs["keyframes"],
                kwargs["audio_assets"],
                timing["shots"],
                strict=True,
            )
        ],
        "ffprobe_validation": validation,
        "generation_context": context,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "validation": validation,
    }


def _prepare_real_image_source(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, str]:
    project_id, script_job_id = _source_job(client, settings, database)
    image_job_id = _queue_real_job(
        client, project_id, script_job_id, monkeypatch
    )
    _allow_worker_gpu(monkeypatch)
    image_provider = _FakeRealImageProvider()
    image_worker = Worker(
        settings=settings,
        database=database,
        image_provider_factory=lambda _settings: image_provider,
        real_image_renderer=_fake_real_image_renderer,
    )
    assert image_worker.run_once() is True
    image_job = client.get(f"/api/jobs/{image_job_id}").json()
    assert image_job["status"] == "SUCCEEDED", image_job
    return project_id, script_job_id, image_job_id


def _queue_real_audio_job(
    client: TestClient,
    *,
    project_id: str,
    image_job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    speaker: str = "Serena",
) -> str:
    monkeypatch.setattr(
        "backend.app.api.projects.audio_gpu_handoff_status",
        lambda _settings: {
            "conflict": False,
            "llama_port_listening": False,
            "comfyui_port_listening": False,
            "known_gpu_model_process_detected": False,
        },
    )
    response = client.post(
        f"/api/projects/{project_id}/render-real-audio",
        json={
            "source_image_job_id": image_job_id,
            "audio_provider": REAL_AUDIO_PROVIDER_ID,
            "speaker": speaker,
            "language": "Chinese",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _allow_audio_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.app.worker.audio_gpu_handoff_status",
        lambda _settings: {
            "conflict": False,
            "llama_port_listening": False,
            "comfyui_port_listening": False,
            "known_gpu_model_process_detected": False,
        },
    )
    monkeypatch.setattr("backend.app.worker.GpuMemoryMonitor", _FakeGpuMonitor)


def test_real_audio_without_animagine_job_generates_wav_and_timing_only(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, script_job_id = _source_job(client, settings, database)
    override = client.put(
        f"/api/projects/{project_id}/shots/shot_02/planning",
        json={
            "keyframe_description": "少女站在原地，列车已经停靠。",
            "motion_description": "车门打开，少女保持原地。",
        },
    )
    assert override.status_code == 200, override.text
    monkeypatch.setattr(
        "backend.app.api.projects.audio_gpu_handoff_status",
        lambda _settings: {"conflict": False},
    )
    response = client.post(
        f"/api/projects/{project_id}/render-real-audio",
        json={
            "source_script_job_id": script_job_id,
            "audio_provider": REAL_AUDIO_PROVIDER_ID,
            "speaker": "Serena",
            "language": "Chinese",
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    queued = client.get(f"/api/jobs/{job_id}").json()
    assert queued["request_json"]["source_image_job_id"] is None
    assert queued["request_json"]["audio_only"] is True

    _allow_audio_worker(monkeypatch)
    _forbid_upstream_provider_calls(monkeypatch)

    def forbid_media_render(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("audio-only generation must not render final media")

    worker = Worker(
        settings=settings,
        database=database,
        audio_provider_factory=lambda _settings: _FakeRealAudioProvider(),
        real_audio_renderer=forbid_media_render,
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["status"] == "SUCCEEDED", payload
    result = payload["result_json"]
    assert result["audio_only"] is True
    assert result["final_media_available"] is False
    assert result["source_image_job_id"] is None
    assert isinstance(result["timing_plan"], dict)
    assert len(result["audio_shots"]) == 3
    assert all(item["audio_url"] for item in result["audio_shots"])
    with database.session() as session:
        assert not any(
            job.job_type == "GENERATE_REAL_IMAGE_VIDEO"
            for job in crud.list_jobs(session, project_id)
        )


def test_matching_script_job_ignores_production_override_but_detects_script_change(
    client: TestClient,
    settings: Settings,
    database: Database,
) -> None:
    project_id, script_job_id = _source_job(client, settings, database)
    saved = client.put(
        f"/api/projects/{project_id}/shots/shot_02/planning",
        json={
            "keyframe_description": "制作层静态首帧校正",
            "motion_description": "制作层后续运动校正",
        },
    )
    assert saved.status_code == 200, saved.text
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        for index in range(12):
            job = crud.create_job(
                session,
                project=project,
                provider_id="test-runtime",
                job_type=f"RUNTIME_ONLY_{index}",
            )
            job.status = JobStatus.SUCCEEDED
            job.progress = 100
        session.commit()

    detail = client.get(f"/api/projects/{project_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["matching_script_job"]["id"] == script_job_id
    assert not any(
        item["id"] == script_job_id for item in detail.json()["recent_jobs"]
    )

    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        changed = json.loads(json.dumps(project.script_json, ensure_ascii=False))
        changed["shots"][1]["narration"] = "这是一段真正变化后的不同旁白。"
        project.script_json = changed
        session.commit()
    changed_detail = client.get(f"/api/projects/{project_id}")
    assert changed_detail.status_code == 200
    assert changed_detail.json()["matching_script_job"] is None


def test_audio_without_successful_script_job_remains_blocked(
    client: TestClient,
) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "无剧本来源", "story": "这是一个尚未生成结构化剧本的测试故事。"},
    ).json()
    response = client.post(
        f"/api/projects/{project['id']}/render-real-audio",
        json={"speaker": "Serena", "language": "Chinese"},
    )
    assert response.status_code == 409
    assert "成功且与当前剧本对应的 Script Job" in response.text


def _forbid_upstream_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("M5-B must not invoke ScriptProvider or ImageProvider")

    monkeypatch.setattr(Worker, "_generation_service_for", fail)
    monkeypatch.setattr(Worker, "_real_image_provider", fail)


def test_real_audio_worker_reuses_script_and_images_and_records_global_gpu(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, script_job_id, image_job_id = _prepare_real_image_source(
        client, settings, database, monkeypatch
    )
    audio_job_id = _queue_real_audio_job(
        client,
        project_id=project_id,
        image_job_id=image_job_id,
        monkeypatch=monkeypatch,
    )
    queued = client.get(f"/api/jobs/{audio_job_id}").json()
    assert queued["job_type"] == "GENERATE_REAL_AUDIO_VIDEO"
    assert queued["provider_id"] == REAL_AUDIO_PROVIDER_ID
    assert queued["request_json"]["source_script_job_id"] == script_job_id
    assert queued["request_json"]["source_image_job_id"] == image_job_id
    assert queued["request_json"]["script_provider_calls_expected"] == 0
    assert queued["request_json"]["image_provider_calls_expected"] == 0

    provider = _FakeRealAudioProvider()
    _allow_audio_worker(monkeypatch)
    _forbid_upstream_provider_calls(monkeypatch)
    worker = Worker(
        settings=settings,
        database=database,
        audio_provider_factory=lambda _settings: provider,
        real_audio_renderer=_fake_real_audio_renderer,
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{audio_job_id}").json()
    assert payload["status"] == "SUCCEEDED", payload
    result = payload["result_json"]
    assert result["script_provider_calls"] == 0
    assert result["image_provider_calls"] == 0
    assert result["source_image_provider"] == "comfyui-animagine-xl-4"
    assert result["audio_provider"] == REAL_AUDIO_PROVIDER_ID
    assert result["mock_audio_fallback"] is False
    assert result["model_load_count"] == 1
    assert result["sequential_generation"] is True
    assert result["max_audio_concurrency"] == 1
    assert result["audio_generated_count"] == 3
    assert result["audio_reused_count"] == 0
    assert provider.batch_calls == 1
    assert provider.generated_shot_ids == ["shot_01", "shot_02", "shot_03"]
    assert result["gpu_memory_observed"]["baseline_mib"] == 420
    assert result["gpu_memory_observed"]["peak_mib"] == 4380
    assert result["provider_gpu_allocator_observed"]["peak_allocated_mib"] == 3000
    assert result["source_planned_duration_seconds"] <= result[
        "rendered_planned_duration_seconds"
    ]
    assert all(item["status"] == "SUCCEEDED" for item in result["audio_shots"])
    manifest = client.get(result["manifest_url"]).json()
    assert manifest["audio_provider"] == REAL_AUDIO_PROVIDER_ID
    assert manifest["image_provider"] == "comfyui-animagine-xl-4"
    assert manifest["script_provider_calls"] == 0
    assert manifest["image_provider_calls"] == 0
    assert manifest["mock_audio_fallback"] is False

    with database.session() as session:
        audio_assets = list(
            session.scalars(
                select(Asset).where(
                    Asset.project_id == project_id,
                    Asset.asset_type == "NARRATION_AUDIO",
                    Asset.provider_id == REAL_AUDIO_PROVIDER_ID,
                )
            ).all()
        )
        assert len(audio_assets) == 3


def test_real_audio_retry_reuses_valid_wav_without_upstream_provider_calls(
    client: TestClient,
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _script_job_id, image_job_id = _prepare_real_image_source(
        client, settings, database, monkeypatch
    )
    failed_job_id = _queue_real_audio_job(
        client,
        project_id=project_id,
        image_job_id=image_job_id,
        monkeypatch=monkeypatch,
        speaker="Vivian",
    )
    _allow_audio_worker(monkeypatch)
    failing_provider = _FakeRealAudioProvider(fail_on_shot_index=2)
    first_worker = Worker(
        settings=settings,
        database=database,
        audio_provider_factory=lambda _settings: failing_provider,
        real_audio_renderer=_fake_real_audio_renderer,
    )
    assert first_worker.run_once() is True
    failed = client.get(f"/api/jobs/{failed_job_id}").json()
    assert failed["status"] == JobStatus.FAILED.value
    assert failed["result_json"]["generation_error"]["failed_shot_id"] == "shot_02"
    assert failed["result_json"]["audio_completed_count"] == 1
    assert failed["result_json"]["audio_shots"][0]["status"] == "SUCCEEDED"

    retry_response = client.post(f"/api/jobs/{failed_job_id}/retry")
    assert retry_response.status_code == 202, retry_response.text
    retry_job_id = retry_response.json()["job_id"]
    queued_retry = client.get(f"/api/jobs/{retry_job_id}").json()
    assert queued_retry["request_json"]["resume_audio_from_job_id"] == failed_job_id
    assert queued_retry["request_json"]["speaker"] == "Vivian"
    assert queued_retry["request_json"]["source_image_job_id"] == image_job_id

    retry_provider = _FakeRealAudioProvider()
    _forbid_upstream_provider_calls(monkeypatch)
    retry_worker = Worker(
        settings=settings,
        database=database,
        audio_provider_factory=lambda _settings: retry_provider,
        real_audio_renderer=_fake_real_audio_renderer,
    )
    assert retry_worker.run_once() is True
    retried = client.get(f"/api/jobs/{retry_job_id}").json()
    assert retried["status"] == JobStatus.SUCCEEDED.value, retried
    result = retried["result_json"]
    assert retry_provider.reusable_seen == ["shot_01"]
    assert retry_provider.generated_shot_ids == ["shot_02", "shot_03"]
    assert result["audio_shots"][0]["status"] == "REUSED"
    assert result["audio_reused_count"] == 1
    assert result["audio_generated_count"] == 2
    assert result["model_load_count"] == 1
    assert result["script_provider_calls"] == 0
    assert result["image_provider_calls"] == 0
    assert result["speaker"] == "Vivian"
    assert result["mock_audio_fallback"] is False

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.app import crud
from backend.app.media import resolve_media_tools
from backend.app.media.ffmpeg import run_command, sha256_file
from backend.app.media.mock_pipeline import generate_mock_wav
from backend.app.models import JobStatus
from backend.app.script_schema import Character, Scene, ScriptV1, Shot
from backend.app.services.audio_jobs import REAL_AUDIO_JOB_TYPE, REAL_AUDIO_PROVIDER_ID
from backend.app.services.image_jobs import REAL_IMAGE_JOB_TYPE, REAL_IMAGE_PROVIDER_ID
from backend.app.services.media_rerender import MEDIA_RERENDER_JOB_TYPE
from backend.app.worker import Worker


def _script() -> ScriptV1:
    durations = (7.0, 7.0, 6.0)
    return ScriptV1(
        schema_version="script.v1",
        title="Media rerender fixture",
        synopsis="A bounded fixture reuses three images and three narration tracks.",
        characters=[
            Character(
                id="hero",
                name="Hero",
                role="lead",
                appearance="short dark hair and a blue coat",
                personality="calm and observant",
                costume="blue coat",
                consistency_prompt="same original hero in a blue coat",
            )
        ],
        scenes=[
            Scene(
                id=f"scene{index}",
                name=f"Scene {index}",
                description="A quiet anime night scene.",
                time="night",
                lighting="soft blue light",
                consistency_prompt="same quiet blue night",
            )
            for index in range(1, 4)
        ],
        shots=[
            Shot(
                id=f"shot{index}",
                index=index,
                title=f"Shot {index}",
                scene_id=f"scene{index}",
                character_ids=["hero"],
                visual_description="The hero looks across the quiet city.",
                camera="gentle push in",
                image_prompt="anime film keyframe, no text",
                negative_prompt="text, watermark",
                narration=f"This is narration for shot {index}.",
                duration_seconds=duration,
            )
            for index, duration in enumerate(durations, start=1)
        ],
    )


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
            f"color=c={color}:s=320x180:d=0.1",
            "-frames:v",
            "1",
            "-update",
            "1",
            path,
        ],
        timeout_seconds=60,
    )


def _source_jobs(database, settings, project_id: str) -> dict[str, str]:
    script = _script()
    data_root = Path(settings.data_dir).resolve()
    project_root = settings.project_dir(project_id)
    image_items: list[dict[str, Any]] = []
    audio_items: list[dict[str, Any]] = []
    timing_items: list[dict[str, Any]] = []

    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        project.script_json = script.model_dump(mode="json")
        script_job = crud.create_job(
            session,
            project=project,
            provider_id="llamacpp",
            job_type="GENERATE_SHORT_VIDEO",
        )
        script_job.status = JobStatus.SUCCEEDED
        script_job.progress = 100
        script_job.result_json = {"script_json": project.script_json}
        image_job = crud.create_job(
            session,
            project=project,
            provider_id=REAL_IMAGE_PROVIDER_ID,
            job_type=REAL_IMAGE_JOB_TYPE,
        )
        audio_job = crud.create_job(
            session,
            project=project,
            provider_id=REAL_AUDIO_PROVIDER_ID,
            job_type=REAL_AUDIO_JOB_TYPE,
        )
        session.flush()

        for index, shot in enumerate(script.shots, start=1):
            image = project_root / "jobs" / image_job.id / "images" / f"shot-{index:02d}.png"
            image_trace = image.with_suffix(".result.json")
            image_workflow = image.with_suffix(".workflow.json")
            _make_png(image, ("0x173d78", "0x633a82", "0xb46762")[index - 1])
            image_trace.write_text("{}\n", encoding="utf-8")
            image_workflow.write_text("{}\n", encoding="utf-8")
            image_items.append(
                {
                    "provider_id": REAL_IMAGE_PROVIDER_ID,
                    "source_type": "REAL_LOCAL_MODEL",
                    "model_id": "animagine-xl-4.0-test",
                    "model_sha256": "6" * 64,
                    "shot_id": shot.id,
                    "shot_index": shot.index,
                    "status": "SUCCEEDED",
                    "image_path": image.relative_to(data_root).as_posix(),
                    "image_sha256": sha256_file(image),
                    "width": 320,
                    "height": 180,
                    "seed": 100 + index,
                    "generation_seconds": 0.1,
                    "workflow_path": image_workflow.relative_to(data_root).as_posix(),
                    "trace_path": image_trace.relative_to(data_root).as_posix(),
                    "warnings": [],
                }
            )

            audio = project_root / "jobs" / audio_job.id / "audio" / f"shot-{index:02d}.wav"
            audio_trace = audio.with_suffix(".result.json")
            generate_mock_wav(audio, 0.5 + index * 0.1, 220 + index * 30)
            audio_trace.write_text("{}\n", encoding="utf-8")
            audio_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="NARRATION_AUDIO",
                provider_id=REAL_AUDIO_PROVIDER_ID,
                source_type="REAL_LOCAL_MODEL",
                file_path=audio.relative_to(data_root).as_posix(),
                sha256=sha256_file(audio),
                metadata_json={"job_id": audio_job.id, "shot_id": shot.id},
            )
            audio_items.append(
                {
                    "provider_id": REAL_AUDIO_PROVIDER_ID,
                    "model_id": "qwen3-tts-test",
                    "model_revision": "revision-test",
                    "model_sha256": "b" * 64,
                    "shot_id": shot.id,
                    "shot_index": shot.index,
                    "status": "SUCCEEDED",
                    "text": shot.narration,
                    "speaker": "Serena",
                    "language": "Chinese",
                    "seed": 200 + index,
                    "audio_path": audio.relative_to(data_root).as_posix(),
                    "trace_path": audio_trace.relative_to(data_root).as_posix(),
                    "audio_sha256": sha256_file(audio),
                    "sample_rate": 48_000,
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "duration_seconds": 0.5 + index * 0.1,
                    "generation_seconds": 0.1,
                    "real_time_factor": 0.1,
                    "peak_amplitude": 0.2,
                    "rms": 0.1,
                    "warnings": [],
                    "audio_asset_id": audio_asset.id,
                }
            )
            timing_items.append(
                {
                    "shot_id": shot.id,
                    "shot_index": shot.index,
                    "source_shot_duration": float(shot.duration_seconds),
                    "source_duration_seconds": float(shot.duration_seconds),
                    "audio_duration": 0.5 + index * 0.1,
                    "audio_duration_seconds": 0.5 + index * 0.1,
                    "lead_in_seconds": 0.2,
                    "lead_out_seconds": 0.35,
                    "rendered_shot_duration": float(shot.duration_seconds),
                    "rendered_duration_seconds": float(shot.duration_seconds),
                    "extended_by_seconds": 0.0,
                    "extension_seconds": 0.0,
                    "extension_reason": "NO_EXTENSION",
                }
            )

        image_job.status = JobStatus.SUCCEEDED
        image_job.progress = 100
        image_job.result_json = {
            "source_script_job_id": script_job.id,
            "source_script_provider": "llamacpp",
            "image_provider": REAL_IMAGE_PROVIDER_ID,
            "mock_image_fallback": False,
            "image_shots": image_items,
        }
        audio_job.status = JobStatus.SUCCEEDED
        audio_job.progress = 100
        audio_job.result_json = {
            "source_script_job_id": script_job.id,
            "source_image_job_id": image_job.id,
            "source_script_provider": "llamacpp",
            "source_image_provider": REAL_IMAGE_PROVIDER_ID,
            "audio_provider": REAL_AUDIO_PROVIDER_ID,
            "mock_audio_fallback": False,
            "audio_shots": audio_items,
            "timing_plan": {
                "timing_plan_version": "m5.audio-timing.v1",
                "fps": 24,
                "source_total_duration_seconds": 20.0,
                "rendered_total_duration_seconds": 20.0,
                "max_total_duration_seconds": 60.0,
                "shots": timing_items,
            },
        }
        session.commit()
        return {
            "script": script_job.id,
            "image": image_job.id,
            "audio": audio_job.id,
            "first_audio_asset": audio_items[0]["audio_asset_id"],
        }


def test_real_audio_job_exposes_safe_public_wav_url(client, database, settings) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "Audio URL", "story": "A sufficiently long story for audio URL testing."},
    ).json()
    sources = _source_jobs(database, settings, project["id"])
    response = client.get(f"/api/jobs/{sources['audio']}")
    assert response.status_code == 200
    audio_shots = response.json()["result_json"]["audio_shots"]
    assert all(item["audio_url"].startswith("/api/projects/") for item in audio_shots)
    wav = client.get(audio_shots[0]["audio_url"])
    assert wav.status_code == 200
    assert wav.headers["content-type"].startswith("audio/wav")
    assert len(wav.content) > 44

    with database.session() as session:
        audio_job = crud.get_job(session, sources["audio"])
        assert audio_job is not None
        result = dict(audio_job.result_json or {})
        items = [dict(item) for item in result["audio_shots"]]
        items[0]["audio_asset_id"] = "missing-audio-asset"
        result["audio_shots"] = items
        audio_job.result_json = result
        session.commit()
    missing = client.get(f"/api/jobs/{sources['audio']}").json()["result_json"]["audio_shots"][0]
    assert "audio_url" not in missing
    assert missing["audio_url_error"]["code"] == "AUDIO_ASSET_URL_MISSING"

    with database.session() as session:
        project_row = crud.get_project(session, project["id"])
        assert project_row is not None
        invalid = crud.create_asset(
            session,
            project_id=project["id"],
            asset_type="NARRATION_AUDIO",
            provider_id=REAL_AUDIO_PROVIDER_ID,
            source_type="REAL_LOCAL_MODEL",
            file_path="../outside.wav",
            sha256="0" * 64,
        )
        session.commit()
        invalid_id = invalid.id
    assert client.get(
        f"/api/projects/{project['id']}/assets/{invalid_id}/content"
    ).status_code == 404


def test_media_only_rerender_reuses_sources_without_provider_calls(
    client, database, settings, tmp_path: Path
) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "Media only", "story": "A sufficiently long story for media-only rerender testing."},
    ).json()
    project_id = project["id"]
    sources = _source_jobs(database, settings, project_id)
    background = tmp_path / "background.wav"
    generate_mock_wav(background, 0.5, 180)
    assert client.post(
        f"/api/projects/{project_id}/background-audio",
        params={"filename": background.name},
        content=background.read_bytes(),
        headers={"content-type": "audio/wav"},
    ).status_code == 200

    queued = client.post(
        f"/api/projects/{project_id}/media-rerender",
        json={
            "source_audio_job_id": sources["audio"],
            "motion_preset": "cinematic_pan",
            "background_audio_enabled": True,
            "background_volume": 0.18,
        },
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    snapshot = client.get(f"/api/jobs/{job_id}").json()["request_json"]
    assert snapshot["media_only"] is True
    assert snapshot["parent_job_id"] == sources["audio"]
    assert snapshot["source_script_job_id"] == sources["script"]
    assert snapshot["source_image_job_id"] == sources["image"]
    assert snapshot["source_audio_job_id"] == sources["audio"]
    assert snapshot["motion_preset"] == "cinematic_pan"
    assert snapshot["background_audio"]["volume"] == 0.18
    assert snapshot["script_provider"] == "reused"
    assert snapshot["image_provider"] == "reused"
    assert snapshot["audio_provider"] == "reused"

    captured: dict[str, Any] = {}

    def forbidden_provider(_settings):
        raise AssertionError("media-only rerender must not construct a model provider")

    def renderer(**kwargs):
        captured.update(kwargs)
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / kwargs["output_filename"]
        poster = output_dir / "poster.jpg"
        manifest_path = output_dir / "manifest.json"
        video.write_bytes(b"media-only-mp4")
        poster.write_bytes(b"poster")
        data_root = Path(settings.data_dir).resolve()
        manifest = {
            "media_only": True,
            "reused_providers": kwargs["generation_context"]["reused_providers"],
            "output": {"poster_path": poster.relative_to(data_root).as_posix()},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {
            "output_path": str(video),
            "manifest_path": str(manifest_path),
            "poster_path": str(poster),
            "sha256": sha256_file(video),
            "validation": {
                "encoded_duration_seconds": 20.0,
                "duration_delta_seconds": 0.0,
                "duration_tolerance_seconds": 0.05,
                "duration_validation": "passed_exactly",
            },
            "warnings": [],
        }

    worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=renderer,
        image_provider_factory=forbidden_provider,
        audio_provider_factory=forbidden_provider,
    )
    assert worker.run_once() is True
    completed = client.get(f"/api/jobs/{job_id}").json()
    assert completed["status"] == "SUCCEEDED"
    assert completed["result_json"]["media_only"] is True
    assert completed["result_json"]["script_provider_calls"] == 0
    assert completed["result_json"]["image_provider_calls"] == 0
    assert completed["result_json"]["audio_provider_calls"] == 0
    assert completed["result_json"]["video_provider_calls"] == 0
    assert captured["motion_preset"] == "cinematic_pan"
    assert captured["background_audio"]["volume"] == 0.18
    assert all(Path(item["image_path"]).is_absolute() for item in captured["keyframes"])
    assert all(Path(item["audio_path"]).is_absolute() for item in captured["audio_assets"])
    assert all(
        item["selection_reason"] == "LEGACY_IMAGE_JOB_FALLBACK"
        and item["visual_source_type"] == "IMAGE"
        for item in captured["visual_sources"]
    )
    manifest_path = (
        Path(settings.data_dir) / completed["result_json"]["manifest_path"]
    ).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["media_only"] is True
    assert manifest["reused_providers"] == {
        "script_provider": "reused",
        "image_provider": "reused",
        "audio_provider": "reused",
    }
    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["latest_export"]["job_id"] == job_id
    assert client.get(detail["latest_export"]["video_url"]).status_code == 200
    assert client.get(detail["latest_export"]["manifest_url"]).status_code == 200
    assert client.get(detail["latest_export"]["poster_url"]).status_code == 200

    with database.session() as session:
        completed_job = crud.get_job(session, job_id)
        assert completed_job is not None
        completed_job.status = JobStatus.FAILED
        session.commit()
    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 202
    retry_snapshot = client.get(f"/api/jobs/{retried.json()['job_id']}").json()["request_json"]
    assert retry_snapshot["motion_preset"] == "cinematic_pan"
    assert retry_snapshot["background_audio"]["volume"] == 0.18
    assert retry_snapshot["source_audio_job_id"] == sources["audio"]


def test_media_only_rejects_cross_project_source(client, database, settings) -> None:
    source_project = client.post(
        "/api/projects",
        json={"title": "Source", "story": "A sufficiently long source project story."},
    ).json()
    sources = _source_jobs(database, settings, source_project["id"])
    other = client.post(
        "/api/projects",
        json={"title": "Other", "story": "A sufficiently long different project story."},
    ).json()
    response = client.post(
        f"/api/projects/{other['id']}/media-rerender",
        json={"source_audio_job_id": sources["audio"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SOURCE_JOB_PROJECT_MISMATCH"


def test_frontend_audio_contract_has_player_and_missing_state() -> None:
    root = Path(__file__).resolve().parents[2]
    types_source = (root / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    app_source = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "audio_url?: string" in types_source
    assert "function ShotAudioPlayer" in app_source
    assert 'preload="metadata"' in app_source
    assert "AUDIO_ASSET_URL_MISSING" in app_source
    assert "AUDIO_DECODE_FAILED" in app_source

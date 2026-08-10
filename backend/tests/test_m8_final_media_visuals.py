from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.app import crud
from backend.app.media import (
    MediaToolError,
    render_real_audio_project_short,
    resolve_media_tools,
    verify_media,
)
from backend.app.media.ffmpeg import run_command, sha256_file
from backend.app.models import JobStatus
from backend.app.providers.cloud_wan import (
    CLOUD_WAN_PROVIDER_ID,
    CLOUD_WAN_SOURCE_TYPE,
)
from backend.app.services.video_jobs import VIDEO_JOB_TYPE
from backend.app.worker import Worker
from backend.tests.test_m5_real_audio_media import AUDIO_PROVIDER_ID, _fixtures
from backend.tests.test_m6_media_blockers import _script, _source_jobs
from backend.tests.test_m5_real_audio_workflow import (
    _FakeRealAudioProvider,
    _allow_audio_worker,
    _forbid_upstream_provider_calls,
    _prepare_real_image_source,
    _queue_real_audio_job,
)


def _make_video(path: Path, *, duration: float, color: str, with_audio: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tools = resolve_media_tools()
    command: list[Any] = [
        tools.ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=240x136:r=12:d={duration}",
    ]
    if with_audio:
        command.extend(
            ["-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:d={duration}"]
        )
    command.extend(["-map", "0:v:0"])
    if with_audio:
        command.extend(["-map", "1:a:0", "-c:a", "aac"])
    else:
        command.append("-an")
    command.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            path,
        ]
    )
    run_command(command, timeout_seconds=120)


def _seed_database_shots(database, project_id: str) -> None:
    script = _script()
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        crud.replace_shots(
            session,
            project=project,
            script_json=script.model_dump(mode="json"),
            shots=[
                {
                    "shot_index": shot.index,
                    "title": shot.title,
                    "visual_description": shot.visual_description,
                    "narration": shot.narration,
                    "duration_seconds": shot.duration_seconds,
                    "provider_id": "llamacpp",
                    "parameters_json": {"provider_shot_id": shot.id},
                }
                for shot in script.shots
            ],
        )
        session.commit()


def _seed_video_job(
    database,
    settings,
    project_id: str,
    shot_ids: list[str],
    *,
    with_audio: bool = False,
    provider_id: str = "mock-video",
    source_type: str = "MOCK",
    source_image_asset_ids: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    data_root = Path(settings.data_dir).resolve()
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        job = crud.create_job(
            session,
            project=project,
            provider_id=provider_id,
            job_type=VIDEO_JOB_TYPE,
        )
        session.flush()
        items: list[dict[str, Any]] = []
        asset_ids: dict[str, str] = {}
        for index, shot_id in enumerate(shot_ids, start=1):
            video = settings.project_dir(project_id) / "jobs" / job.id / "video" / f"{shot_id}.mp4"
            _make_video(
                video,
                duration=1.0 + index * 0.25,
                color=("red", "green", "blue")[index - 1],
                with_audio=with_audio,
            )
            asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="VIDEO_SHOT",
                provider_id=provider_id,
                source_type=source_type,
                file_path=video.relative_to(data_root).as_posix(),
                sha256=sha256_file(video),
                metadata_json={
                    "job_id": job.id,
                    "shot_id": shot_id,
                    "source_image_asset_id": (source_image_asset_ids or {}).get(
                        shot_id
                    ),
                },
            )
            items.append(
                {
                    "shot_id": shot_id,
                    "shot_index": index,
                    "status": "SUCCEEDED",
                    "provider_id": provider_id,
                    "source_type": source_type,
                    "video_asset_id": asset.id,
                    "video_path": asset.file_path,
                    "video_sha256": asset.sha256,
                }
            )
            asset_ids[shot_id] = asset.id
        job.status = JobStatus.SUCCEEDED
        job.progress = 100
        job.result_json = {
            "video_provider": provider_id,
            "video_shots": items,
            "video_provider_calls": len(items),
        }
        session.commit()
        return job.id, asset_ids


def _fake_renderer(settings, captured: dict[str, Any]):
    def renderer(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        video = output_dir / kwargs["output_filename"]
        manifest_path = output_dir / "manifest.json"
        video.write_bytes(b"m8-a3-final-media")
        visual_sources = kwargs["visual_sources"]
        manifest = {
            "selection_mode": kwargs["generation_context"].get(
                "selection_mode", "MANUAL"
            ),
            "selection_plan": kwargs["generation_context"].get("selection_plan"),
            "video_source_type": (
                "VIDEO_SHOT_WITH_IMAGE_FALLBACK"
                if any(item["visual_source_type"] == "VIDEO_SHOT" for item in visual_sources)
                else "MEDIA_ONLY_RERENDER_FFMPEG"
            ),
            "visual_source_summary": {
                "video_shot_count": sum(
                    item["visual_source_type"] == "VIDEO_SHOT" for item in visual_sources
                ),
                "image_shot_count": sum(
                    item["visual_source_type"] == "IMAGE" for item in visual_sources
                ),
                "explicit_image_shot_count": sum(
                    item.get("selection_reason") == "EXPLICIT_IMAGE_ASSET"
                    for item in visual_sources
                ),
            },
            "shots": [{"visual_source": item} for item in visual_sources],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {
            "output_path": str(video),
            "manifest_path": str(manifest_path),
            "sha256": sha256_file(video),
            "validation": {
                "encoded_duration_seconds": 20.0,
                "duration_delta_seconds": 0.0,
                "duration_tolerance_seconds": 0.05,
                "duration_validation": "passed_exactly",
            },
            "manifest": manifest,
            "warnings": [],
        }

    return renderer


def _external_keyframes(keyframes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(keyframes, start=1):
        item = dict(raw)
        item.pop("model_id", None)
        item.pop("seed", None)
        item.update(
            {
                "provider_id": "external-import",
                "source_type": "EXTERNAL_IMPORT",
                "generation_mode": "HUMAN_IN_THE_LOOP",
                "external_source_type": "AI_GENERATED",
                "provider_hint": "ChatGPT Images",
                "original_filename": f"external-shot-{index}.png",
                "imported_at": "2026-08-10T00:00:00+00:00",
            }
        )
        normalized.append(item)
    return normalized


def test_complete_video_job_resolves_all_video_shots_without_provider_calls(
    client, database, settings
) -> None:
    project = client.post(
        "/api/projects", json={"title": "Complete video", "story": "A complete visual source test story."}
    ).json()
    sources = _source_jobs(database, settings, project["id"])
    _seed_database_shots(database, project["id"])
    video_job_id, _asset_ids = _seed_video_job(
        database, settings, project["id"], ["shot1", "shot2", "shot3"]
    )
    queued = client.post(
        f"/api/projects/{project['id']}/media-rerender",
        json={"source_audio_job_id": sources["audio"], "source_video_job_id": video_job_id},
    )
    assert queued.status_code == 202, queued.text
    captured: dict[str, Any] = {}
    worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, captured),
        image_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        audio_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
    )
    assert worker.run_once() is True
    assert [item["visual_source_type"] for item in captured["visual_sources"]] == [
        "VIDEO_SHOT",
        "VIDEO_SHOT",
        "VIDEO_SHOT",
    ]
    result = client.get(f"/api/jobs/{queued.json()['job_id']}").json()["result_json"]
    assert result["video_provider_calls"] == 0
    assert result["source_video_job_id"] == video_job_id
    assert result["visual_source_summary"] == {
        "video_shot_count": 3,
        "image_shot_count": 0,
        "explicit_image_shot_count": 0,
    }


def test_external_images_to_cloud_video_preferred_renders_final_media_without_model_id(
    client, database, settings, tmp_path: Path
) -> None:
    """覆盖 Showcase：External Image 是视频血缘，而非本地模型关键帧。"""

    from backend.tests.test_m6_media_blockers import _make_png

    project = client.post(
        "/api/projects",
        json={
            "title": "External to cloud video",
            "story": "Three external frames become three cloud video shots.",
        },
    ).json()
    _source_jobs(database, settings, project["id"])
    _seed_database_shots(database, project["id"])
    selected_images: dict[str, str] = {}
    for shot_id, color in zip(
        ("shot1", "shot2", "shot3"),
        ("yellow", "orange", "purple"),
        strict=True,
    ):
        source = tmp_path / f"{shot_id}.png"
        _make_png(source, color)
        imported = client.post(
            f"/api/projects/{project['id']}/shots/{shot_id}/external-images",
            params={
                "filename": source.name,
                "external_source_type": "AI_GENERATED",
                "provider_hint": "ChatGPT Images",
            },
            content=source.read_bytes(),
            headers={"content-type": "image/png"},
        )
        assert imported.status_code == 201, imported.text
        assert "model_id" not in imported.json()
        selected_images[shot_id] = imported.json()["asset_id"]

    video_job_id, _video_assets = _seed_video_job(
        database,
        settings,
        project["id"],
        ["shot1", "shot2", "shot3"],
        provider_id=CLOUD_WAN_PROVIDER_ID,
        source_type=CLOUD_WAN_SOURCE_TYPE,
        source_image_asset_ids=selected_images,
    )
    selection = client.put(
        f"/api/projects/{project['id']}/visual-selection",
        json={
            "source_image_asset_ids": selected_images,
            "source_video_job_id": video_job_id,
        },
    )
    assert selection.status_code == 200, selection.text
    plan = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "VIDEO_PREFERRED"},
    ).json()
    assert plan["status"] == "READY", plan
    assert all(item["selected_type"] == "VIDEO_SHOT" for item in plan["shots"])

    queued = client.post(
        f"/api/projects/{project['id']}/smart-media-render",
        json={"composition_mode": "VIDEO_PREFERRED"},
    )
    assert queued.status_code == 202, queued.text
    worker = Worker(
        settings=settings,
        database=database,
        image_provider_factory=lambda _settings: (_ for _ in ()).throw(
            AssertionError("Final Media must not call ImageProvider")
        ),
        audio_provider_factory=lambda _settings: (_ for _ in ()).throw(
            AssertionError("Final Media must not call AudioProvider")
        ),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(
            AssertionError("Final Media must not call VideoProvider")
        ),
    )
    assert worker.run_once() is True
    completed = client.get(f"/api/jobs/{queued.json()['job_id']}").json()
    assert completed["status"] == "SUCCEEDED", completed
    result = completed["result_json"]
    project_detail = client.get(f"/api/projects/{project['id']}").json()
    assert client.get(project_detail["latest_export"]["video_url"]).status_code == 200
    manifest = client.get(result["manifest_url"]).json()
    assert manifest["selection_mode"] == "VIDEO_PREFERRED"
    assert manifest["image_provider"] is None
    assert manifest["visual_source_summary"]["video_shot_count"] == 3
    for shot in manifest["shots"]:
        visual = shot["visual_source"]
        assert shot["visual_source_type"] == "VIDEO_SHOT"
        assert shot["source_provider"] == CLOUD_WAN_PROVIDER_ID
        assert shot["source_type"] == CLOUD_WAN_SOURCE_TYPE
        assert shot["source_image_asset_id"] == selected_images[shot["shot_id"]]
        assert visual["source_image_asset_id"] == selected_images[shot["shot_id"]]


def test_partial_video_uses_selected_external_then_legacy_image_fallback(
    client, database, settings, tmp_path: Path
) -> None:
    project = client.post(
        "/api/projects", json={"title": "Partial video", "story": "A partial visual source test story."}
    ).json()
    sources = _source_jobs(database, settings, project["id"])
    _seed_database_shots(database, project["id"])
    video_job_id, _asset_ids = _seed_video_job(
        database, settings, project["id"], ["shot1"]
    )
    external = tmp_path / "external.png"
    from backend.tests.test_m6_media_blockers import _make_png

    _make_png(external, "yellow")
    imported = client.post(
        f"/api/projects/{project['id']}/shots/shot2/external-images",
        params={
            "filename": "external.png",
            "external_source_type": "AI_GENERATED",
            "provider_hint": "ChatGPT Images",
        },
        content=external.read_bytes(),
        headers={"content-type": "image/png"},
    )
    assert imported.status_code == 201, imported.text
    queued = client.post(
        f"/api/projects/{project['id']}/media-rerender",
        json={
            "source_audio_job_id": sources["audio"],
            "source_video_job_id": video_job_id,
            "source_image_asset_ids": {"shot2": imported.json()["asset_id"]},
        },
    )
    assert queued.status_code == 202, queued.text
    captured: dict[str, Any] = {}
    worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, captured),
    )
    assert worker.run_once() is True
    assert [item["selection_reason"] for item in captured["visual_sources"]] == [
        "EXPLICIT_VIDEO_JOB",
        "EXPLICIT_IMAGE_ASSET",
        "LEGACY_IMAGE_JOB_FALLBACK",
    ]
    assert captured["visual_sources"][1]["source_provider"] == "external-import"


def test_media_rerender_rejects_cross_project_and_wrong_shot_video_binding(
    client, database, settings
) -> None:
    first = client.post(
        "/api/projects", json={"title": "First", "story": "A first project visual source story."}
    ).json()
    first_sources = _source_jobs(database, settings, first["id"])
    _seed_database_shots(database, first["id"])
    second = client.post(
        "/api/projects", json={"title": "Second", "story": "A second project visual source story."}
    ).json()
    second_sources = _source_jobs(database, settings, second["id"])
    _seed_database_shots(database, second["id"])
    video_job_id, asset_ids = _seed_video_job(database, settings, first["id"], ["shot1"])
    cross = client.post(
        f"/api/projects/{second['id']}/media-rerender",
        json={"source_audio_job_id": second_sources["audio"], "source_video_job_id": video_job_id},
    )
    assert cross.status_code == 409
    assert cross.json()["detail"]["code"] == "SOURCE_JOB_PROJECT_MISMATCH"

    with database.session() as session:
        asset = crud.get_asset(session, asset_ids["shot1"])
        assert asset is not None
        asset.metadata_json = {**asset.metadata_json, "shot_id": "shot2"}
        session.commit()
    wrong = client.post(
        f"/api/projects/{first['id']}/media-rerender",
        json={"source_audio_job_id": first_sources["audio"], "source_video_job_id": video_job_id},
    )
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["code"] == "SOURCE_VIDEO_MISSING"


def test_persisted_selection_drives_auto_audio_final_and_matches_rerender(
    client, database, settings, tmp_path: Path, monkeypatch
) -> None:
    project_id, _script_job_id, image_job_id = _prepare_real_image_source(
        client, settings, database, monkeypatch
    )
    external = tmp_path / "selected-external.png"
    from backend.tests.test_m6_media_blockers import _make_png

    _make_png(external, "yellow")
    imported = client.post(
        f"/api/projects/{project_id}/shots/shot_03/external-images",
        params={
            "filename": "selected-external.png",
            "external_source_type": "AI_GENERATED",
            "provider_hint": "ChatGPT Images",
        },
        content=external.read_bytes(),
        headers={"content-type": "image/png"},
    )
    assert imported.status_code == 201, imported.text
    external_asset_id = imported.json()["asset_id"]
    video_job_id, video_asset_ids = _seed_video_job(
        database, settings, project_id, ["shot_01", "shot_02"]
    )
    selected = client.put(
        f"/api/projects/{project_id}/visual-selection",
        json={
            "source_image_asset_ids": {"shot_03": external_asset_id},
            "source_video_job_id": video_job_id,
        },
    )
    assert selected.status_code == 200, selected.text
    refreshed = client.get(f"/api/projects/{project_id}").json()
    assert refreshed["visual_selection"] == selected.json()

    audio_job_id = _queue_real_audio_job(
        client,
        project_id=project_id,
        image_job_id=image_job_id,
        monkeypatch=monkeypatch,
    )
    queued = client.get(f"/api/jobs/{audio_job_id}").json()["request_json"]
    assert queued["source_video_job_id"] == video_job_id
    assert queued["source_video_asset_ids"] == video_asset_ids
    assert queued["source_image_asset_ids"] == {"shot_03": external_asset_id}
    assert queued["video_provider_calls_expected"] == 0

    auto_captured: dict[str, Any] = {}
    _allow_audio_worker(monkeypatch)
    _forbid_upstream_provider_calls(monkeypatch)
    worker = Worker(
        settings=settings,
        database=database,
        audio_provider_factory=lambda _settings: _FakeRealAudioProvider(),
        real_audio_renderer=_fake_renderer(settings, auto_captured),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(
            AssertionError("automatic Final Media must not call VideoProvider")
        ),
    )
    assert worker.run_once() is True
    audio_job = client.get(f"/api/jobs/{audio_job_id}").json()
    assert audio_job["status"] == "SUCCEEDED", audio_job
    expected_reasons = [
        "EXPLICIT_VIDEO_JOB",
        "EXPLICIT_VIDEO_JOB",
        "EXPLICIT_IMAGE_ASSET",
    ]
    assert [item["selection_reason"] for item in auto_captured["visual_sources"]] == expected_reasons
    assert auto_captured["generation_context"]["provider_calls"]["video"] == 0
    assert audio_job["result_json"]["video_provider_calls"] == 0
    assert audio_job["result_json"]["source_video_asset_ids"] == video_asset_ids
    assert [
        item["selection_reason"] for item in audio_job["result_json"]["visual_sources"]
    ] == expected_reasons

    rerender = client.post(
        f"/api/projects/{project_id}/media-rerender",
        json={
            "source_audio_job_id": audio_job_id,
            "source_video_job_id": video_job_id,
            "source_image_asset_ids": {"shot_03": external_asset_id},
        },
    )
    assert rerender.status_code == 202, rerender.text
    rerender_captured: dict[str, Any] = {}
    rerender_worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, rerender_captured),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(
            AssertionError("MEDIA_RERENDER must not call VideoProvider")
        ),
    )
    assert rerender_worker.run_once() is True
    assert [
        item["selection_reason"] for item in rerender_captured["visual_sources"]
    ] == expected_reasons


def test_smart_plan_freezes_exact_ids_and_worker_does_not_resolve_again(
    client, database, settings, tmp_path: Path
) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "Smart media", "story": "A deterministic best available media story."},
    ).json()
    sources = _source_jobs(database, settings, project["id"])
    _seed_database_shots(database, project["id"])
    with database.session() as session:
        image_job = crud.get_job(session, sources["image"])
        assert image_job is not None
        database_shots = crud.list_shots(session, project["id"])
        for raw, database_shot in zip(
            image_job.result_json["image_shots"], database_shots, strict=True
        ):
            asset = crud.create_asset(
                session,
                project_id=project["id"],
                shot_id=database_shot.id,
                asset_type="KEYFRAME_IMAGE",
                provider_id=raw["provider_id"],
                source_type=raw["source_type"],
                file_path=raw["image_path"],
                sha256=raw["image_sha256"],
                metadata_json={"job_id": image_job.id, "shot_id": raw["shot_id"]},
            )
            raw["image_asset_id"] = asset.id
        session.commit()
    from backend.tests.test_m6_media_blockers import _make_png

    first_external = tmp_path / "first-external.png"
    _make_png(first_external, "yellow")
    imported = client.post(
        f"/api/projects/{project['id']}/shots/shot3/external-images",
        params={
            "filename": "first-external.png",
            "external_source_type": "AI_GENERATED",
            "provider_hint": "ChatGPT Images",
        },
        content=first_external.read_bytes(),
        headers={"content-type": "image/png"},
    )
    assert imported.status_code == 201, imported.text
    selected_external_id = imported.json()["asset_id"]
    mock_video_job_id, _mock_video_assets = _seed_video_job(
        database, settings, project["id"], ["shot1", "shot2", "shot3"]
    )
    persisted = client.put(
        f"/api/projects/{project['id']}/visual-selection",
        json={
            "source_image_asset_ids": {"shot3": selected_external_id},
            "source_video_job_id": mock_video_job_id,
        },
    )
    assert persisted.status_code == 200, persisted.text

    plan_response = client.get(f"/api/projects/{project['id']}/best-media-plan")
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    assert plan["status"] == "READY", plan
    assert plan["audio"]["job_id"] == sources["audio"]
    shot3 = next(item for item in plan["shots"] if item["shot_id"] == "shot3")
    assert shot3["asset_id"] == selected_external_id
    assert shot3["priority_class"] == "NON_MOCK_IMAGE"
    assert all(item["selected_type"] != "VIDEO_SHOT" for item in plan["shots"])

    queued = client.post(
        f"/api/projects/{project['id']}/smart-media-render",
        json={"motion_preset": "gentle_zoom"},
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    frozen = client.get(f"/api/jobs/{job_id}").json()["request_json"]
    assert frozen["selection_mode"] == "BEST_AVAILABLE"
    assert frozen["resolver_runs_expected_in_worker"] == 0
    assert frozen["source_video_job_id"] is None
    assert frozen["source_image_asset_ids"]["shot3"] == selected_external_id

    second_external = tmp_path / "second-external.png"
    _make_png(second_external, "orange")
    changed = client.post(
        f"/api/projects/{project['id']}/shots/shot3/external-images",
        params={
            "filename": "second-external.png",
            "external_source_type": "AI_GENERATED",
            "provider_hint": "Another External Tool",
        },
        content=second_external.read_bytes(),
        headers={"content-type": "image/png"},
    )
    assert changed.status_code == 201, changed.text
    assert changed.json()["asset_id"] != selected_external_id

    captured: dict[str, Any] = {}
    worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, captured),
        image_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        audio_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
    )
    assert worker.run_once() is True
    assert captured["generation_context"]["selection_mode"] == "BEST_AVAILABLE"
    captured_shot3 = next(
        item for item in captured["visual_sources"] if item["shot_id"] == "shot3"
    )
    assert captured_shot3["source_asset_id"] == selected_external_id
    result = client.get(f"/api/jobs/{job_id}").json()["result_json"]
    assert result["selection_mode"] == "BEST_AVAILABLE"
    assert result["video_provider_calls"] == 0
    manifest = client.get(result["manifest_url"]).json()
    assert manifest["selection_mode"] == "BEST_AVAILABLE"
    assert manifest["selection_plan"]["shots"] == plan["shots"]

    outdated = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "BEST_AVAILABLE"},
    ).json()
    assert outdated["freshness"] == "OUTDATED"

    image_plan = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "IMAGE_ONLY"},
    ).json()
    assert image_plan["status"] == "READY"
    assert all(item["selected_type"] != "VIDEO_SHOT" for item in image_plan["shots"])
    image_queued = client.post(
        f"/api/projects/{project['id']}/smart-media-render",
        json={"composition_mode": "IMAGE_ONLY", "motion_preset": "gentle_zoom"},
    )
    assert image_queued.status_code == 202, image_queued.text
    image_capture: dict[str, Any] = {}
    image_worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, image_capture),
        image_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        audio_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
    )
    assert image_worker.run_once() is True
    assert image_capture["generation_context"]["selection_mode"] == "IMAGE_ONLY"
    current_image_plan = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "IMAGE_ONLY"},
    ).json()
    assert current_image_plan["freshness"] == "CURRENT"

    video_plan = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "VIDEO_PREFERRED"},
    ).json()
    assert video_plan["status"] == "READY"
    assert all(item["selected_type"] == "VIDEO_SHOT" for item in video_plan["shots"])
    assert video_plan["freshness"] == "OUTDATED"
    video_queued = client.post(
        f"/api/projects/{project['id']}/smart-media-render",
        json={"composition_mode": "VIDEO_PREFERRED", "motion_preset": "gentle_zoom"},
    )
    assert video_queued.status_code == 202, video_queued.text
    video_capture: dict[str, Any] = {}
    video_worker = Worker(
        settings=settings,
        database=database,
        real_audio_renderer=_fake_renderer(settings, video_capture),
        image_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        audio_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
        video_provider_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
    )
    assert video_worker.run_once() is True
    assert video_capture["generation_context"]["selection_mode"] == "VIDEO_PREFERRED"
    assert all(
        item["visual_source_type"] == "VIDEO_SHOT"
        for item in video_capture["visual_sources"]
    )
    current_video_plan = client.get(
        f"/api/projects/{project['id']}/best-media-plan",
        params={"mode": "VIDEO_PREFERRED"},
    ).json()
    assert current_video_plan["freshness"] == "CURRENT"


def test_renderer_freezes_short_video_trims_long_video_and_strips_source_audio(
    tmp_path: Path,
) -> None:
    shots, keyframes, audio_assets, timing_plan = _fixtures(tmp_path)
    keyframes = [dict(item) for item in keyframes]
    keyframes[1] = _external_keyframes([keyframes[1]])[0]
    short_video = tmp_path / "visuals" / "short.mp4"
    long_video = tmp_path / "visuals" / "long.mp4"
    _make_video(short_video, duration=1.0, color="red", with_audio=True)
    _make_video(long_video, duration=9.0, color="blue", with_audio=True)
    selected_image = Path(keyframes[1]["image_path"])
    visual_sources = [
        {
            "shot_id": "shot1",
            "visual_source_type": "VIDEO_SHOT",
            "selection_reason": "EXPLICIT_VIDEO_JOB",
            "source_asset_id": "video-asset-1",
            "source_video_job_id": "video-job",
            "source_provider": "mock-video",
            "source_type": "MOCK",
            "source_path": str(short_video),
            "source_sha256": sha256_file(short_video),
        },
        {
            "shot_id": "shot2",
            "visual_source_type": "IMAGE",
            "selection_reason": "EXPLICIT_IMAGE_ASSET",
            "source_asset_id": "external-image-2",
            "source_video_job_id": None,
            "source_provider": "external-import",
            "source_type": "EXTERNAL_IMPORT",
            "source_path": str(selected_image),
            "source_sha256": sha256_file(selected_image),
        },
        {
            "shot_id": "shot3",
            "visual_source_type": "VIDEO_SHOT",
            "selection_reason": "EXPLICIT_VIDEO_JOB",
            "source_asset_id": "video-asset-3",
            "source_video_job_id": "video-job",
            "source_provider": "mock-video",
            "source_type": "MOCK",
            "source_path": str(long_video),
            "source_sha256": sha256_file(long_video),
        },
    ]
    rendered = render_real_audio_project_short(
        root=tmp_path,
        project_id="m8-a3-render",
        project_title="M8-A3 visual normalization",
        shots=shots,
        keyframes=keyframes,
        audio_assets=audio_assets,
        timing_plan=timing_plan,
        visual_sources=visual_sources,
        output_dir=tmp_path / "export",
        output_filename="final.mp4",
        width=1280,
        height=720,
        fps=24,
        provider_id=AUDIO_PROVIDER_ID,
        generation_context={
            "media_only": True,
            "providers": {
                "script_provider": "reused",
                "image_provider": "selected-assets",
                "audio_provider": AUDIO_PROVIDER_ID,
            },
            "provider_calls": {"script": 0, "image": 0, "audio": 0, "video": 0},
        },
    )
    manifest = rendered["manifest"]
    assert [item["visual_source_type"] for item in manifest["shots"]] == [
        "VIDEO_SHOT",
        "IMAGE",
        "VIDEO_SHOT",
    ]
    assert manifest["shots"][0]["video_duration_normalization"] == "PLAY_THEN_FREEZE_LAST_FRAME"
    assert manifest["shots"][2]["video_duration_normalization"] == "TRIM_TO_TARGET"
    assert manifest["shots"][0]["source_video_audio_ignored"] is True
    assert manifest["shots"][2]["source_video_audio_ignored"] is True
    assert manifest["shots"][1]["source_provider"] == "external-import"
    expected_duration = sum(item["rendered_shot_duration"] for item in timing_plan["shots"])
    verification = verify_media(
        resolve_media_tools(),
        Path(rendered["output_path"]),
        expected_width=1280,
        expected_height=720,
        expected_fps=24.0,
        expected_duration_seconds=expected_duration,
    )
    assert verification["video_codec"] == "h264"
    assert verification["audio_codec"] == "aac"
    video_commands = [
        command
        for command in manifest["safe_command_log"]
        if "stop_mode=clone" in command
    ]
    assert len(video_commands) == 2
    assert all("-map 0:v:0 -map 1:a:0" in command for command in video_commands)


def test_image_only_external_assets_render_without_model_id(tmp_path: Path) -> None:
    shots, keyframes, audio_assets, timing_plan = _fixtures(tmp_path)
    external_keyframes = _external_keyframes([dict(item) for item in keyframes])
    rendered = render_real_audio_project_short(
        root=tmp_path,
        project_id="m8-external-image-only",
        project_title="External image-only Final Media",
        shots=shots,
        keyframes=external_keyframes,
        audio_assets=audio_assets,
        timing_plan=timing_plan,
        output_dir=tmp_path / "external-image-only-export",
        output_filename="final.mp4",
        width=1280,
        height=720,
        fps=24,
        provider_id=AUDIO_PROVIDER_ID,
        generation_context={
            "media_only": True,
            "providers": {
                "script_provider": "reused",
                "image_provider": "external-import",
                "audio_provider": AUDIO_PROVIDER_ID,
            },
            "provider_calls": {"script": 0, "image": 0, "audio": 0, "video": 0},
        },
    )
    assert Path(rendered["output_path"]).is_file()
    manifest = rendered["manifest"]
    assert manifest["image_provider"] == "external-import"
    assert all(item["visual_source_type"] == "IMAGE" for item in manifest["shots"])
    assert all(item["keyframe"].get("model_id") is None for item in manifest["shots"])


def test_external_and_local_image_contracts_keep_required_integrity_fields(
    tmp_path: Path,
) -> None:
    shots, keyframes, audio_assets, timing_plan = _fixtures(tmp_path)
    common = {
        "root": tmp_path,
        "project_id": "m8-image-contracts",
        "project_title": "Image provenance contracts",
        "shots": shots,
        "audio_assets": audio_assets,
        "timing_plan": timing_plan,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "provider_id": AUDIO_PROVIDER_ID,
    }

    local_missing_model = [dict(item) for item in keyframes]
    local_missing_model[0].pop("model_id")
    with pytest.raises(MediaToolError, match="model_id"):
        render_real_audio_project_short(
            **common,
            keyframes=local_missing_model,
            output_dir=tmp_path / "missing-local-model",
        )

    external = _external_keyframes([dict(item) for item in keyframes])
    invalid_variants: list[tuple[str, list[dict[str, Any]], str]] = []
    bad_sha = [dict(item) for item in external]
    bad_sha[0]["image_sha256"] = "0" * 64
    invalid_variants.append(("bad-sha", bad_sha, "SHA-256 不符"))
    bad_dimensions = [dict(item) for item in external]
    bad_dimensions[0]["width"] += 1
    invalid_variants.append(("bad-dimensions", bad_dimensions, "尺寸不符"))
    missing_file = [dict(item) for item in external]
    missing_file[0]["image_path"] = str(tmp_path / "missing.png")
    invalid_variants.append(("missing-file", missing_file, "不存在或为空"))
    for name, invalid, message in invalid_variants:
        with pytest.raises(MediaToolError, match=message):
            render_real_audio_project_short(
                **common,
                keyframes=invalid,
                output_dir=tmp_path / name,
            )


def test_frontend_composition_modes_do_not_treat_video_success_as_final_export() -> None:
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    api_source = (root / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    assert "使用关键帧合成成片" in app_source
    assert "使用动态镜头合成成片" in app_source
    assert 'pendingNavigationRef.current = "composition"' in app_source
    assert "动态镜头已准备完成，当前最终成片尚未包含这些新素材。" in app_source
    assert 'else if (job.job_type === "MEDIA_RERENDER" && jobHasFinalMedia(job))' in app_source
    assert 'job.job_type === "MEDIA_RERENDER" && jobHasFinalMedia(job)' in app_source
    assert "AI 旁白生成完成" in app_source
    assert "真实 AI 旁白已完成" not in app_source
    assert "composition_mode: compositionMode" in api_source

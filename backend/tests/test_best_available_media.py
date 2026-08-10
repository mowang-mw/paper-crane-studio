from __future__ import annotations

from typing import Any

from backend.app.models import Asset, GenerationJob, JobStatus, Shot as DatabaseShot
from backend.app.services.best_available_media import (
    IMAGE_ONLY,
    VIDEO_PREFERRED,
    is_mock_provenance,
    resolve_best_available_media,
)
from backend.app.services.audio_jobs import REAL_AUDIO_JOB_TYPE
from backend.app.services.image_jobs import REAL_IMAGE_JOB_TYPE
from backend.app.services.video_jobs import VIDEO_JOB_TYPE
from backend.tests.test_m6_media_blockers import _script


PROJECT_ID = "best-available-project"


def _database_shots() -> list[DatabaseShot]:
    return [
        DatabaseShot(
            id=f"db-shot-{index}",
            project_id=PROJECT_ID,
            shot_index=index,
            title=f"Shot {index}",
            visual_description=f"Visual {index}",
            narration=f"Narration {index}",
            duration_seconds=7.0,
            provider_id="llamacpp",
            parameters_json={"provider_shot_id": f"shot{index}"},
        )
        for index in range(1, 4)
    ]


def _asset(
    asset_id: str,
    *,
    shot_index: int,
    asset_type: str,
    source_type: str,
    provider: str,
    job_id: str | None = None,
) -> Asset:
    metadata: dict[str, Any] = {"shot_id": f"shot{shot_index}"}
    if job_id:
        metadata["job_id"] = job_id
    return Asset(
        id=asset_id,
        project_id=PROJECT_ID,
        shot_id=f"db-shot-{shot_index}" if asset_type == "KEYFRAME_IMAGE" else None,
        asset_type=asset_type,
        provider_id=provider,
        source_type=source_type,
        file_path=f"projects/{PROJECT_ID}/{asset_id}",
        sha256="a" * 64,
        metadata_json=metadata,
    )


def _legacy_job() -> GenerationJob:
    return GenerationJob(
        id="legacy-image-job",
        project_id=PROJECT_ID,
        job_type=REAL_IMAGE_JOB_TYPE,
        status=JobStatus.SUCCEEDED,
        progress=100,
        provider_id="comfyui-animagine-xl-4",
        request_json={},
        result_json={
            "mock_image_fallback": False,
            "image_shots": [
                {
                    "shot_id": f"shot{index}",
                    "provider_id": "comfyui-animagine-xl-4",
                    "source_type": "REAL_LOCAL_MODEL",
                    "image_path": f"legacy-{index}.png",
                    "image_sha256": "b" * 64,
                }
                for index in range(1, 4)
            ],
        },
    )


def _real_audio(job_id: str = "real-audio-job") -> GenerationJob:
    return GenerationJob(
        id=job_id,
        project_id=PROJECT_ID,
        job_type=REAL_AUDIO_JOB_TYPE,
        status=JobStatus.SUCCEEDED,
        progress=100,
        provider_id="qwen3-tts-0.6b-customvoice",
        request_json={},
        result_json={
            "mock_audio_fallback": False,
            "audio_source_type": "REAL_LOCAL_MODEL",
            "source_script_job_id": "script-job",
            "source_image_job_id": "legacy-image-job",
            "speaker": "Serena",
            "audio_shots": [{"shot_id": f"shot{index}"} for index in range(1, 4)],
            "timing_plan": {"shots": [{"shot_id": f"shot{index}"} for index in range(1, 4)]},
        },
    )


def _mock_audio(job_id: str = "mock-audio-job") -> GenerationJob:
    return GenerationJob(
        id=job_id,
        project_id=PROJECT_ID,
        job_type="GENERATE_SHORT_VIDEO",
        status=JobStatus.SUCCEEDED,
        progress=100,
        provider_id="mock",
        request_json={},
        result_json={
            "source_type": "DETERMINISTIC_FALLBACK",
            "audio_provider": "mock",
        },
    )


def _video_job(
    job_id: str, *, source_type: str, provider: str, shot_indices: tuple[int, ...] = (1,)
) -> tuple[GenerationJob, list[Asset]]:
    assets = [
        _asset(
            f"{job_id}-asset-{index}",
            shot_index=index,
            asset_type="VIDEO_SHOT",
            source_type=source_type,
            provider=provider,
            job_id=job_id,
        )
        for index in shot_indices
    ]
    job = GenerationJob(
        id=job_id,
        project_id=PROJECT_ID,
        job_type=VIDEO_JOB_TYPE,
        status=JobStatus.SUCCEEDED,
        progress=100,
        provider_id=provider,
        request_json={},
        result_json={
            "video_shots": [
                {
                    "shot_id": f"shot{index}",
                    "status": "SUCCEEDED",
                    "video_asset_id": asset.id,
                    "provider_id": provider,
                    "source_type": source_type,
                }
                for index, asset in zip(shot_indices, assets, strict=True)
            ]
        },
    )
    return job, assets


def _plan(
    *,
    assets: list[Asset] | None = None,
    jobs: list[GenerationJob] | None = None,
    explicit_images: dict[str, str] | None = None,
    explicit_video: str | None = None,
    preferred_audio: str | None = None,
    mode: str = "BEST_AVAILABLE",
) -> dict[str, Any]:
    return resolve_best_available_media(
        script=_script(),
        database_shots=_database_shots(),
        assets=assets or [],
        jobs=jobs or [_legacy_job(), _real_audio()],
        explicit_image_asset_ids=explicit_images or {},
        explicit_video_job_id=explicit_video,
        preferred_audio_job_id=preferred_audio,
        mode=mode,
    )


def _shot(plan: dict[str, Any], shot_id: str = "shot1") -> dict[str, Any]:
    return next(item for item in plan["shots"] if item["shot_id"] == shot_id)


def test_real_image_beats_explicit_mock_video() -> None:
    image = _asset(
        "real-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="EXTERNAL_IMPORT", provider="external-import",
    )
    video_job, video_assets = _video_job(
        "mock-video-job", source_type="MOCK", provider="mock-video"
    )
    plan = _plan(
        assets=[image, *video_assets],
        jobs=[_legacy_job(), _real_audio(), video_job],
        explicit_video=video_job.id,
    )
    assert _shot(plan)["asset_id"] == image.id
    assert _shot(plan)["priority_class"] == "NON_MOCK_IMAGE"


def test_explicit_video_version_is_authoritative_over_historical_video() -> None:
    image = _asset(
        "real-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="REAL_LOCAL_MODEL", provider="animagine",
    )
    real_job, real_assets = _video_job(
        "real-video-job", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    mock_job, mock_assets = _video_job(
        "mock-video-job", source_type="MOCK", provider="mock-video"
    )
    plan = _plan(
        mode=VIDEO_PREFERRED,
        assets=[image, *real_assets, *mock_assets],
        jobs=[_legacy_job(), _real_audio(), real_job, mock_job],
        explicit_video=mock_job.id,
    )
    assert _shot(plan)["asset_id"] == mock_assets[0].id
    assert _shot(plan)["priority_class"] == "MOCK_VIDEO_SHOT"


def test_mock_video_beats_mock_image_and_mock_image_is_fallback() -> None:
    image = _asset(
        "mock-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="MOCK", provider="mock-image-provider",
    )
    video_job, video_assets = _video_job(
        "mock-video-job", source_type="MOCK", provider="mock-video"
    )
    with_video = _plan(
        assets=[image, *video_assets],
        jobs=[_legacy_job(), _real_audio(), video_job],
    )
    assert _shot(with_video)["selected_type"] == "VIDEO_SHOT"
    without_video = _plan(assets=[image])
    assert _shot(without_video)["asset_id"] == image.id
    assert _shot(without_video)["priority_class"] == "MOCK_IMAGE"


def test_legacy_is_last_visual_fallback() -> None:
    plan = _plan()
    assert _shot(plan)["selected_type"] == "LEGACY_IMAGE"


def test_explicit_image_only_breaks_a_same_class_tie() -> None:
    first = _asset(
        "real-image-a", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="REAL_LOCAL_MODEL", provider="animagine",
    )
    second = _asset(
        "real-image-b", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="EXTERNAL_IMPORT", provider="external-import",
    )
    selected = _plan(assets=[first, second], explicit_images={"shot1": second.id})
    assert _shot(selected)["asset_id"] == second.id
    ambiguous = _plan(assets=[first, second])
    assert ambiguous["status"] == "AMBIGUOUS"
    assert ambiguous["problems"][0]["code"] == "AMBIGUOUS_VISUAL"


def test_two_real_video_jobs_are_ambiguous_without_explicit_selection() -> None:
    first_job, first_assets = _video_job(
        "wan-a", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    second_job, second_assets = _video_job(
        "wan-b", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    plan = _plan(
        assets=[*first_assets, *second_assets],
        jobs=[_legacy_job(), _real_audio(), first_job, second_job],
    )
    assert plan["status"] == "AMBIGUOUS"
    assert plan["problems"][0]["priority_class"] == "NON_MOCK_VIDEO_SHOT"


def test_external_import_is_non_mock_without_claiming_an_api_provider() -> None:
    assert is_mock_provenance(source_type="EXTERNAL_IMPORT") is False
    external = _asset(
        "external", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="EXTERNAL_IMPORT", provider="external-import",
    )
    external.metadata_json["provider_hint"] = "ChatGPT Images"
    plan = _plan(assets=[external], explicit_images={"shot1": external.id})
    selected = _shot(plan)
    assert selected["is_mock"] is False
    assert selected["provider"] == "external-import"
    assert selected["provider_hint"] == "ChatGPT Images"


def test_audio_real_then_mock_priority_ambiguity_and_missing_boundary() -> None:
    real = _real_audio()
    mock = _mock_audio()
    plan = _plan(jobs=[_legacy_job(), real, mock])
    assert plan["audio"]["job_id"] == real.id
    only_mock = _plan(jobs=[_legacy_job(), mock])
    assert only_mock["audio"]["job_id"] == mock.id
    assert only_mock["audio"]["is_mock"] is True
    ambiguous = _plan(jobs=[_legacy_job(), real, _real_audio("real-audio-2")])
    assert ambiguous["status"] == "AMBIGUOUS"
    assert any(item["code"] == "AMBIGUOUS_AUDIO" for item in ambiguous["problems"])
    missing = _plan(jobs=[_legacy_job()])
    assert missing["status"] == "BLOCKED"
    assert any(item["code"] == "NO_AUDIO_JOB" for item in missing["problems"])


def test_image_only_ignores_real_and_mock_video_assets() -> None:
    image = _asset(
        "selected-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="EXTERNAL_IMPORT", provider="external-import",
    )
    real_job, real_assets = _video_job(
        "real-video", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    mock_job, mock_assets = _video_job(
        "mock-video", source_type="MOCK", provider="mock-video"
    )
    plan = _plan(
        mode=IMAGE_ONLY,
        assets=[image, *real_assets, *mock_assets],
        jobs=[_legacy_job(), _real_audio(), real_job, mock_job],
        explicit_images={"shot1": image.id},
        explicit_video=real_job.id,
    )
    assert plan["status"] == "READY"
    assert _shot(plan)["asset_id"] == image.id
    assert all(item["selected_type"] != "VIDEO_SHOT" for item in plan["shots"])
    assert plan["warnings"] == []


def test_video_preferred_requires_video_and_mock_video_beats_real_image() -> None:
    image = _asset(
        "real-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="REAL_LOCAL_MODEL", provider="animagine",
    )
    blocked = _plan(mode=VIDEO_PREFERRED, assets=[image])
    assert blocked["status"] == "BLOCKED"
    assert any(item["code"] == "NO_VIDEO_SOURCE" for item in blocked["problems"])

    mock_job, mock_assets = _video_job(
        "mock-video", source_type="MOCK", provider="mock-video"
    )
    plan = _plan(
        mode=VIDEO_PREFERRED,
        assets=[image, *mock_assets],
        jobs=[_legacy_job(), _real_audio(), mock_job],
    )
    assert plan["status"] == "READY"
    assert _shot(plan)["asset_id"] == mock_assets[0].id
    assert _shot(plan)["priority_class"] == "MOCK_VIDEO_SHOT"


def test_video_preferred_partial_uses_image_image_video() -> None:
    images = [
        _asset(
            f"image-{index}", shot_index=index, asset_type="KEYFRAME_IMAGE",
            source_type="EXTERNAL_IMPORT", provider="external-import",
        )
        for index in (1, 2, 3)
    ]
    real_job, real_assets = _video_job(
        "wan-partial",
        source_type="REAL_CLOUD_MODEL",
        provider="cloud-wan-2.7",
        shot_indices=(3,),
    )
    plan = _plan(
        mode=VIDEO_PREFERRED,
        assets=[*images, *real_assets],
        jobs=[_legacy_job(), _real_audio(), real_job],
        explicit_images={f"shot{index}": image.id for index, image in enumerate(images, 1)},
    )
    assert [item["selected_type"] for item in plan["shots"]] == [
        "IMAGE", "IMAGE", "VIDEO_SHOT"
    ]


def test_video_preferred_same_class_selection_and_stale_lineage() -> None:
    current = _asset(
        "current-image", shot_index=1, asset_type="KEYFRAME_IMAGE",
        source_type="EXTERNAL_IMPORT", provider="external-import",
    )
    first_job, first_assets = _video_job(
        "wan-a", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    second_job, second_assets = _video_job(
        "wan-b", source_type="REAL_CLOUD_MODEL", provider="cloud-wan-2.7"
    )
    ambiguous = _plan(
        mode=VIDEO_PREFERRED,
        assets=[current, *first_assets, *second_assets],
        jobs=[_legacy_job(), _real_audio(), first_job, second_job],
    )
    assert ambiguous["status"] == "AMBIGUOUS"
    selected = _plan(
        mode=VIDEO_PREFERRED,
        assets=[current, *first_assets, *second_assets],
        jobs=[_legacy_job(), _real_audio(), first_job, second_job],
        explicit_video=second_job.id,
    )
    assert _shot(selected)["source_job_id"] == second_job.id

    first_assets[0].metadata_json["source_image_asset_id"] = "old-image"
    stale = _plan(
        mode=VIDEO_PREFERRED,
        assets=[current, *first_assets],
        jobs=[_legacy_job(), _real_audio(), first_job],
        explicit_images={"shot1": current.id},
        explicit_video=first_job.id,
    )
    assert _shot(stale)["selected_type"] == "IMAGE"
    assert any(item["code"] == "STALE_VIDEO_LINEAGE" for item in stale["warnings"])
    assert stale["status"] == "BLOCKED"
    assert all(item["asset_id"] != first_assets[0].id for item in stale["shots"])


def test_explicit_partial_video_version_ignores_other_historical_jobs() -> None:
    images = [
        _asset(
            f"image-{index}", shot_index=index, asset_type="KEYFRAME_IMAGE",
            source_type="EXTERNAL_IMPORT", provider="external-import",
        )
        for index in (1, 2, 3)
    ]
    selected_job, selected_assets = _video_job(
        "wan-shot3",
        source_type="REAL_CLOUD_MODEL",
        provider="cloud-wan-2.7",
        shot_indices=(3,),
    )
    history_job, history_assets = _video_job(
        "mock-history",
        source_type="MOCK",
        provider="mock-video",
        shot_indices=(1, 2, 3),
    )
    plan = _plan(
        mode=VIDEO_PREFERRED,
        assets=[*images, *selected_assets, *history_assets],
        jobs=[_legacy_job(), _real_audio(), selected_job, history_job],
        explicit_images={f"shot{index}": image.id for index, image in enumerate(images, 1)},
        explicit_video=selected_job.id,
    )
    assert plan["status"] == "READY"
    assert [item["selected_type"] for item in plan["shots"]] == [
        "IMAGE", "IMAGE", "VIDEO_SHOT"
    ]
    assert _shot(plan, "shot3")["source_job_id"] == selected_job.id

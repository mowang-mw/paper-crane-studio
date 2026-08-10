"""Deterministically select existing media by provenance and stable recency."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..models import Asset, GenerationJob, JobStatus, Shot as DatabaseShot
from ..script_schema import ScriptV1
from .audio_jobs import REAL_AUDIO_JOB_TYPE
from .image_jobs import REAL_IMAGE_JOB_TYPE
from .video_jobs import VIDEO_JOB_TYPE, VIDEO_PROVIDER_IDS


MOCK_SOURCE_TYPES = frozenset({"MOCK", "DETERMINISTIC_FALLBACK"})
BEST_AVAILABLE = "BEST_AVAILABLE"
IMAGE_ONLY = "IMAGE_ONLY"
VIDEO_PREFERRED = "VIDEO_PREFERRED"
COMPOSITION_MODES = frozenset({BEST_AVAILABLE, IMAGE_ONLY, VIDEO_PREFERRED})


def is_mock_provenance(*, source_type: str, mock_flag: bool | None = None) -> bool:
    """Use standard provenance fields; provider display names are intentionally ignored."""

    if mock_flag is not None:
        return mock_flag
    return source_type.strip().upper() in MOCK_SOURCE_TYPES


def _candidate(
    *,
    selected_type: str,
    asset_id: str | None,
    source_job_id: str | None,
    provider: str,
    source_type: str,
    provider_hint: str | None = None,
    source_image_asset_id: str | None = None,
) -> dict[str, Any]:
    return {
        "selected_type": selected_type,
        "asset_id": asset_id,
        "source_job_id": source_job_id,
        "provider": provider,
        "provider_hint": provider_hint,
        "source_type": source_type,
        "is_mock": is_mock_provenance(source_type=source_type),
        "source_image_asset_id": source_image_asset_id,
    }


def _pick_same_class(
    candidates: list[dict[str, Any]],
    *,
    explicit_id: str | None,
    explicit_field: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if explicit_id:
        explicit = [item for item in candidates if item.get(explicit_field) == explicit_id]
        if len(explicit) == 1:
            return explicit[0], []
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return None, sorted(candidates, key=lambda item: str(item.get(explicit_field) or ""))
    return None, []


def _audio_candidates(
    jobs: list[GenerationJob],
    shot_ids: set[str],
    compatible_script_job_ids: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in jobs:
        if job.status != JobStatus.SUCCEEDED:
            continue
        result = job.result_json if isinstance(job.result_json, dict) else {}
        if job.job_type == REAL_AUDIO_JOB_TYPE:
            raw = result.get("audio_shots")
            audio_by_shot = {
                str(item.get("shot_id")): item
                for item in raw
                if isinstance(item, dict) and item.get("shot_id")
            } if isinstance(raw, list) else {}
            timing = result.get("timing_plan")
            timing_shots = timing.get("shots") if isinstance(timing, dict) else None
            complete_audio = (
                set(audio_by_shot) == shot_ids
                and all(
                    item.get("status") in {"SUCCEEDED", "REUSED"}
                    and isinstance(item.get("audio_path"), str)
                    and bool(item["audio_path"])
                    and isinstance(item.get("audio_sha256"), str)
                    and len(item["audio_sha256"]) == 64
                    and isinstance(item.get("duration_seconds"), (int, float))
                    and float(item["duration_seconds"]) > 0
                    for item in audio_by_shot.values()
                )
                and isinstance(timing_shots, list)
                and {
                    str(item.get("shot_id"))
                    for item in timing_shots
                    if isinstance(item, dict)
                } == shot_ids
            )
            source_script_job_id = result.get("source_script_job_id") or (
                job.request_json.get("source_script_job_id")
                if isinstance(job.request_json, dict)
                else None
            )
            has_compatible_script = str(source_script_job_id) in compatible_script_job_ids
            if (
                result.get("mock_audio_fallback") is False
                and complete_audio
                and has_compatible_script
            ):
                candidates.append(
                    {
                        "job_id": job.id,
                        "provider": job.provider_id,
                        "source_type": str(result.get("audio_source_type") or "REAL_LOCAL_MODEL"),
                        "is_mock": False,
                        "source_script_job_id": source_script_job_id,
                        "source_image_job_id": result.get("source_image_job_id"),
                        "speaker": result.get("speaker"),
                        "reason": "默认使用最新成功且兼容当前剧本的真实旁白",
                    }
                )
            elif (
                result.get("mock_audio_fallback") is True
                and complete_audio
                and has_compatible_script
            ):
                candidates.append(
                    {
                        "job_id": job.id,
                        "provider": job.provider_id,
                        "source_type": str(
                            result.get("audio_source_type") or "DETERMINISTIC_FALLBACK"
                        ),
                        "is_mock": True,
                        "source_script_job_id": source_script_job_id,
                        "source_image_job_id": result.get("source_image_job_id"),
                        "speaker": result.get("speaker"),
                        "reason": "默认使用最新成功且兼容当前剧本的 Mock 旁白",
                    }
                )
    return candidates


def _resolve_audio(
    jobs: list[GenerationJob],
    shot_ids: set[str],
    preferred_audio_job_id: str | None,
    compatible_script_job_ids: set[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = _audio_candidates(jobs, shot_ids, compatible_script_job_ids)
    if preferred_audio_job_id:
        explicit = [
            item for item in candidates if item["job_id"] == preferred_audio_job_id
        ]
        if explicit:
            return explicit[0], None

    def recency(job: GenerationJob) -> tuple[float, str]:
        value = job.finished_at or job.created_at
        if not isinstance(value, datetime):
            return float("-inf"), job.id
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp(), job.id

    for is_mock in (False, True):
        same_class = [item for item in candidates if item["is_mock"] is is_mock]
        if same_class:
            by_id = {item["job_id"]: item for item in same_class}
            ordered = sorted(
                (job for job in jobs if job.id in by_id),
                key=recency,
                reverse=True,
            )
            return by_id[ordered[0].id], None
    return None, {
        "code": "NO_AUDIO_JOB",
        "message": "当前没有可用于成片的 Audio Job，请先生成配音。",
        "candidates": [],
    }


def resolve_best_available_media(
    *,
    script: ScriptV1,
    database_shots: Iterable[DatabaseShot],
    assets: Iterable[Asset],
    jobs: Iterable[GenerationJob],
    compatible_script_job_ids: set[str],
    explicit_image_asset_ids: dict[str, str],
    explicit_video_job_id: str | None,
    preferred_audio_job_id: str | None = None,
    mode: str = BEST_AVAILABLE,
) -> dict[str, Any]:
    """Return an explainable plan; ambiguity is reported instead of guessed."""

    if mode not in COMPOSITION_MODES:
        raise ValueError(f"不支持的成片模式：{mode}")

    job_list = list(jobs)
    asset_list = list(assets)
    shot_ids = {shot.id for shot in script.shots}
    audio, audio_problem = _resolve_audio(
        job_list, shot_ids, preferred_audio_job_id, compatible_script_job_ids
    )
    preferred_legacy_job_id = (
        str(audio.get("source_image_job_id") or "") if audio is not None else ""
    )
    database_by_script: dict[str, DatabaseShot] = {}
    for database_shot in database_shots:
        parameters = database_shot.parameters_json if isinstance(database_shot.parameters_json, dict) else {}
        provider_shot_id = str(parameters.get("provider_shot_id") or "")
        if provider_shot_id in shot_ids:
            database_by_script[provider_shot_id] = database_shot

    images_by_shot: dict[str, list[dict[str, Any]]] = {shot_id: [] for shot_id in shot_ids}
    assets_by_id = {asset.id: asset for asset in asset_list}
    for shot_id, database_shot in database_by_script.items():
        for asset in asset_list:
            if asset.shot_id != database_shot.id or asset.asset_type != "KEYFRAME_IMAGE":
                continue
            metadata = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
            images_by_shot[shot_id].append(
                _candidate(
                    selected_type="IMAGE",
                    asset_id=asset.id,
                    source_job_id=str(metadata.get("job_id") or "") or None,
                    provider=asset.provider_id,
                    source_type=asset.source_type,
                    provider_hint=(
                        str(metadata.get("provider_hint"))
                        if metadata.get("provider_hint")
                        else None
                    ),
                )
            )

    videos_by_shot: dict[str, list[dict[str, Any]]] = {shot_id: [] for shot_id in shot_ids}
    warnings: list[dict[str, Any]] = []
    for job in job_list:
        if (
            job.status != JobStatus.SUCCEEDED
            or job.job_type != VIDEO_JOB_TYPE
            or job.provider_id not in VIDEO_PROVIDER_IDS
        ):
            continue
        result = job.result_json if isinstance(job.result_json, dict) else {}
        raw_videos = result.get("video_shots")
        if not isinstance(raw_videos, list):
            continue
        for raw in raw_videos:
            if not isinstance(raw, dict) or raw.get("status") not in {"SUCCEEDED", "REUSED"}:
                continue
            shot_id = str(raw.get("shot_id") or "")
            asset = assets_by_id.get(str(raw.get("video_asset_id") or ""))
            metadata = asset.metadata_json if asset is not None and isinstance(asset.metadata_json, dict) else {}
            if (
                shot_id not in shot_ids
                or asset is None
                or asset.asset_type != "VIDEO_SHOT"
                or asset.project_id != job.project_id
                or metadata.get("job_id") != job.id
                or metadata.get("shot_id") != shot_id
                or raw.get("source_type") != asset.source_type
            ):
                continue
            videos_by_shot[shot_id].append(
                _candidate(
                    selected_type="VIDEO_SHOT",
                    asset_id=asset.id,
                    source_job_id=job.id,
                    provider=asset.provider_id,
                    source_type=asset.source_type,
                    source_image_asset_id=(
                        str(metadata.get("source_image_asset_id"))
                        if metadata.get("source_image_asset_id")
                        else None
                    ),
                )
            )

    # A persisted Video Job is the explicit current version. Its partial shot set is
    # intentional: missing shots must fall back to images, not other historical jobs.
    if explicit_video_job_id:
        videos_by_shot = {
            shot_id: [
                candidate
                for candidate in candidates
                if candidate.get("source_job_id") == explicit_video_job_id
            ]
            for shot_id, candidates in videos_by_shot.items()
        }

    # A video can only be declared stale when the current keyframe is known without
    # guessing. Persisted selection is authoritative; a sole image candidate is the
    # only safe implicit current image. Missing lineage is reported but not invented.
    for shot_id, candidates in videos_by_shot.items():
        current_image_asset_id = explicit_image_asset_ids.get(shot_id)
        if current_image_asset_id not in {
            str(item.get("asset_id") or "") for item in images_by_shot[shot_id]
        }:
            current_image_asset_id = None
        if current_image_asset_id is None and len(images_by_shot[shot_id]) == 1:
            current_image_asset_id = str(images_by_shot[shot_id][0].get("asset_id") or "") or None
        eligible: list[dict[str, Any]] = []
        for candidate in candidates:
            lineage_id = candidate.get("source_image_asset_id")
            if current_image_asset_id and lineage_id and lineage_id != current_image_asset_id:
                if mode != IMAGE_ONLY:
                    warnings.append(
                        {
                            "code": "STALE_VIDEO_LINEAGE",
                            "shot_id": shot_id,
                            "video_asset_id": candidate.get("asset_id"),
                            "source_video_job_id": candidate.get("source_job_id"),
                            "source_image_asset_id": lineage_id,
                            "current_image_asset_id": current_image_asset_id,
                            "message": "该动态镜头基于旧关键帧生成，需要重新生成。",
                        }
                    )
                continue
            if current_image_asset_id and not lineage_id:
                if mode != IMAGE_ONLY:
                    warnings.append(
                        {
                            "code": "VIDEO_LINEAGE_UNKNOWN",
                            "shot_id": shot_id,
                            "video_asset_id": candidate.get("asset_id"),
                            "message": "该动态镜头缺少来源关键帧绑定，无法确认是否对应当前关键帧。",
                        }
                    )
            eligible.append(candidate)
        videos_by_shot[shot_id] = eligible

    legacy_by_shot: dict[str, list[dict[str, Any]]] = {shot_id: [] for shot_id in shot_ids}
    for job in job_list:
        if job.status != JobStatus.SUCCEEDED or job.job_type != REAL_IMAGE_JOB_TYPE:
            continue
        result = job.result_json if isinstance(job.result_json, dict) else {}
        if result.get("mock_image_fallback") is not False:
            continue
        raw_images = result.get("image_shots")
        if not isinstance(raw_images, list):
            continue
        for raw in raw_images:
            if not isinstance(raw, dict):
                continue
            shot_id = str(raw.get("shot_id") or "")
            if shot_id not in shot_ids:
                continue
            legacy_by_shot[shot_id].append(
                _candidate(
                    selected_type="LEGACY_IMAGE",
                    asset_id=str(raw.get("image_asset_id") or "") or None,
                    source_job_id=job.id,
                    provider=str(raw.get("provider_id") or job.provider_id),
                    source_type=str(raw.get("source_type") or "REAL_LOCAL_MODEL"),
                )
            )

    shot_plans: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    if mode == IMAGE_ONLY:
        priority_names = ["CURRENT_IMAGE_ASSET", "LEGACY_IMAGE_FALLBACK"]
    elif mode == VIDEO_PREFERRED:
        priority_names = [
            "NON_MOCK_VIDEO_SHOT",
            "MOCK_VIDEO_SHOT",
            "CURRENT_IMAGE_ASSET",
            "LEGACY_IMAGE_FALLBACK",
        ]
    else:
        priority_names = [
            "NON_MOCK_VIDEO_SHOT",
            "NON_MOCK_IMAGE",
            "MOCK_VIDEO_SHOT",
            "MOCK_IMAGE",
            "LEGACY_IMAGE_FALLBACK",
        ]

    available_video_shot_count = sum(bool(items) for items in videos_by_shot.values())
    if mode == VIDEO_PREFERRED and available_video_shot_count == 0:
        problems.append(
            {
                "code": "NO_VIDEO_SOURCE",
                "message": "当前项目还没有可用动态镜头，请先生成视频。",
                "candidates": [],
            }
        )

    def resolve_current_image(shot_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        candidates = images_by_shot[shot_id]
        explicit_id = explicit_image_asset_ids.get(shot_id)
        if explicit_id:
            explicit = [item for item in candidates if item.get("asset_id") == explicit_id]
            if len(explicit) == 1:
                return explicit[0], []
        return _pick_same_class(candidates, explicit_id=None, explicit_field="asset_id")

    for shot in script.shots:
        selected: dict[str, Any] | None = None
        for class_name in priority_names:
            if class_name == "CURRENT_IMAGE_ASSET":
                selected, ambiguous = resolve_current_image(shot.id)
            elif class_name == "LEGACY_IMAGE_FALLBACK":
                selected, ambiguous = _pick_same_class(
                    legacy_by_shot[shot.id],
                    explicit_id=preferred_legacy_job_id,
                    explicit_field="source_job_id",
                )
            else:
                is_video = "VIDEO_SHOT" in class_name
                source_map = videos_by_shot if is_video else images_by_shot
                is_mock = class_name.startswith("MOCK_")
                explicit_field = "source_job_id" if is_video else "asset_id"
                explicit_id = (
                    explicit_video_job_id
                    if is_video
                    else explicit_image_asset_ids.get(shot.id)
                )
                candidates = [
                    item for item in source_map[shot.id] if item["is_mock"] is is_mock
                ]
                selected, ambiguous = _pick_same_class(
                    candidates,
                    explicit_id=explicit_id,
                    explicit_field=explicit_field,
                )
            if ambiguous:
                problems.append(
                    {
                        "code": "AMBIGUOUS_VISUAL",
                        "shot_id": shot.id,
                        "message": "存在多个同等级素材，请在高级来源设置中选择。",
                        "priority_class": class_name,
                        "candidates": ambiguous,
                    }
                )
                break
            if selected is not None:
                selected = {
                    "shot_id": shot.id,
                    **selected,
                    "priority_class": class_name,
                    "selection_reason": (
                        f"{class_name} selected by {mode} composition priority; persisted "
                        "selection is only used where the mode contract permits it"
                    ),
                }
                shot_plans.append(selected)
                break
        else:
            problems.append(
                {
                    "code": "NO_VISUAL_SOURCE",
                    "shot_id": shot.id,
                    "message": "当前镜头没有可用于成片的现有视觉素材。",
                    "candidates": [],
                }
            )

    selected_video_jobs = {
        str(item["source_job_id"])
        for item in shot_plans
        if item["selected_type"] == "VIDEO_SHOT" and item.get("source_job_id")
    }
    if len(selected_video_jobs) > 1:
        problems.append(
            {
                "code": "MULTIPLE_VIDEO_JOBS_REQUIRED",
                "message": "当前最佳逐镜头方案涉及多个 Video Job，请在高级来源设置中消歧。",
                "candidates": sorted(selected_video_jobs),
            }
        )
    if audio_problem is not None:
        problems.append(audio_problem)
    status = "READY"
    if any(problem["code"].startswith("AMBIGUOUS") or problem["code"] == "MULTIPLE_VIDEO_JOBS_REQUIRED" for problem in problems):
        status = "AMBIGUOUS"
    elif problems:
        status = "BLOCKED"
    return {
        "mode": mode,
        "status": status,
        "priority": priority_names,
        "shots": shot_plans,
        "audio": audio,
        "problems": problems,
        "warnings": warnings,
        "available_image_shot_count": sum(
            bool(images_by_shot[shot.id] or legacy_by_shot[shot.id]) for shot in script.shots
        ),
        "available_video_shot_count": available_video_shot_count,
    }


__all__ = [
    "BEST_AVAILABLE",
    "IMAGE_ONLY",
    "VIDEO_PREFERRED",
    "is_mock_provenance",
    "resolve_best_available_media",
]

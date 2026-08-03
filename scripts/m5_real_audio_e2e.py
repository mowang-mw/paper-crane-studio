"""有界执行一次 M5-B 三镜头真实中文旁白纵向链路。

该脚本固定复用已经成功的 M4-B ScriptV1 与真实关键帧，通过正式 API
创建 Serena 旁白 Job，再由本地 Worker 同步处理。它不会启动或调用
ScriptProvider、ImageProvider、llama-server 或 ComfyUI。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.media.ffmpeg import (
    decode_media_fully,
    extract_shot_midpoint_frames,
    resolve_media_tools,
    sha256_file,
    verify_media,
)
from backend.app.models import GenerationJob, JobStatus, Project
from backend.app.script_schema import ScriptV1
from backend.app.services.audio_jobs import (
    REAL_AUDIO_JOB_TYPE,
    REAL_AUDIO_PROVIDER_ID,
    REAL_AUDIO_SOURCE_TYPE,
    audio_gpu_handoff_status,
    inspect_pcm16_wav,
)
from backend.app.services.image_jobs import (
    REAL_IMAGE_JOB_TYPE,
    REAL_IMAGE_PROVIDER_ID,
    script_from_source_job,
)
from backend.app.worker import Worker


DEFAULT_SOURCE_IMAGE_JOB_ID = "11c1b83a-f5b7-4511-b7db-2e1056ef2160"
GPU_RELEASE_ALLOWANCE_MIB = 512
PORTS = (8000, 8081, 8188)


class E2EError(RuntimeError):
    """M5-B 真实纵向链路未满足验收契约。"""


class _NoUpstreamWorker(Worker):
    """把任何意外 Script/Image Provider 调用变成可见失败。"""

    def _generation_service_for(self, *args: Any, **kwargs: Any) -> Any:
        raise E2EError("M5-B E2E 禁止调用 ScriptProvider")

    def _real_image_provider(self, *args: Any, **kwargs: Any) -> Any:
        raise E2EError("M5-B E2E 禁止调用 ImageProvider 或启动 ComfyUI")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EError(f"{label} 无法读取为 UTF-8 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise E2EError(f"{label} 顶层必须是对象：{path}")
    return payload


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _port_snapshot() -> dict[str, bool]:
    return {f"{port}_free": _port_is_free("127.0.0.1", port) for port in PORTS}


def _gpu_memory_used_mib() -> int | None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            shell=False,
        )
        if completed.returncode != 0:
            return None
        return int(completed.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _gpu_compute_processes() -> list[dict[str, Any]] | None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    processes: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", maxsplit=2)]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        try:
            used_mib: int | None = int(parts[2])
        except ValueError:
            used_mib = None
        processes.append(
            {"pid": int(parts[0]), "process_name": parts[1], "used_gpu_memory_mib": used_mib}
        )
    return processes


def _tts_runner_processes() -> list[dict[str, Any]] | None:
    """只读查询仍以 qwen3_tts_job_runner.py 启动的 Python 进程。"""

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        return None
    command = (
        "@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -like 'python*' -and "
        "$_.CommandLine -like '*qwen3_tts_job_runner.py*' "
        "} | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return None
    result: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "pid": item.get("ProcessId"),
                "name": item.get("Name"),
                "command_line": item.get("CommandLine"),
            }
        )
    return result


def _wait_for_gpu_release(
    baseline_mib: int,
    *,
    timeout_seconds: float,
) -> tuple[int | None, bool, float]:
    started = time.monotonic()
    deadline = started + max(0.0, timeout_seconds)
    while True:
        current = _gpu_memory_used_mib()
        if current is not None and current <= baseline_mib + GPU_RELEASE_ALLOWANCE_MIB:
            return current, True, round(time.monotonic() - started, 3)
        if time.monotonic() >= deadline:
            return current, False, round(time.monotonic() - started, 3)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _resolve_data_path(
    settings: Settings,
    value: Any,
    *,
    project_id: str,
    label: str,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise E2EError(f"{label} 缺少文件路径")
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (Path(settings.data_dir) / raw).resolve()
    project_root = settings.project_dir(project_id).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise E2EError(f"{label} 路径越过当前项目目录：{path}") from exc
    return path


def _source_image_trace(
    settings: Settings,
    *,
    project_id: str,
    script: ScriptV1,
    source_result: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_images = source_result.get("image_shots")
    if not isinstance(raw_images, list) or len(raw_images) != 3:
        raise E2EError("固定 M4-B 来源 Job 必须恰好包含 3 张真实关键帧")
    by_id = {
        str(item.get("shot_id")): item
        for item in raw_images
        if isinstance(item, dict) and item.get("shot_id")
    }
    if set(by_id) != {shot.id for shot in script.shots}:
        raise E2EError("来源关键帧 shot_id 集合与 ScriptV1 不一致")
    trace: list[dict[str, Any]] = []
    for shot in script.shots:
        item = by_id[shot.id]
        if item.get("provider_id") != REAL_IMAGE_PROVIDER_ID:
            raise E2EError(f"来源关键帧不是正式真实 ImageProvider：{shot.id}")
        image_path = _resolve_data_path(
            settings,
            value=item.get("image_path"),
            project_id=project_id,
            label=f"镜头 {shot.index} 关键帧",
        )
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            raise E2EError(f"来源关键帧不存在或为空：{image_path}")
        actual_sha256 = sha256_file(image_path)
        if actual_sha256 != str(item.get("image_sha256", "")).lower():
            raise E2EError(f"来源关键帧 SHA256 不一致：{image_path}")
        trace.append(
            {
                "shot_id": shot.id,
                "shot_index": shot.index,
                "image_path": str(image_path),
                "image_sha256": actual_sha256,
                "file_size_bytes": image_path.stat().st_size,
            }
        )
    return trace


def _select_source(
    settings: Settings,
    database: Database,
    *,
    source_image_job_id: str,
) -> dict[str, Any]:
    with database.session() as session:
        active = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING))
                )
            ).all()
        )
        if active:
            raise E2EError(
                "数据库已有 QUEUED/RUNNING Job；为避免 run_once 误领任务，请先处理："
                + ", ".join(job.id for job in active)
            )
        source_job = session.get(GenerationJob, source_image_job_id)
        if source_job is None:
            raise E2EError(f"固定 M4-B 来源 Job 不存在：{source_image_job_id}")
        if (
            source_job.status != JobStatus.SUCCEEDED
            or source_job.job_type != REAL_IMAGE_JOB_TYPE
            or source_job.provider_id != REAL_IMAGE_PROVIDER_ID
        ):
            raise E2EError("固定来源 Job 不是成功的 M4-B 真实图像 Job")
        project = session.get(Project, source_job.project_id)
        if project is None:
            raise E2EError("固定来源 Job 的项目不存在")
        source_result = dict(source_job.result_json or {})
        if (
            source_result.get("image_provider") != REAL_IMAGE_PROVIDER_ID
            or source_result.get("mock_image_fallback") is not False
        ):
            raise E2EError("固定来源 Job 无法证明关键帧来自真实模型")
        try:
            script, source_trace = script_from_source_job(
                settings,
                project=project,
                source_job=source_job,
            )
        except RuntimeError as exc:
            raise E2EError(f"固定来源 Job 的 ScriptV1 追溯无效：{exc}") from exc
        if len(script.shots) != 3:
            raise E2EError(f"固定来源 ScriptV1 必须为 3 镜头，实际 {len(script.shots)}")
        if project.script_json != script.model_dump(mode="json"):
            raise E2EError("固定来源 ScriptV1 已不是项目当前剧本")
        images = _source_image_trace(
            settings,
            project_id=project.id,
            script=script,
            source_result=source_result,
        )
        source_script_job_id = str(
            source_result.get("source_script_job_id")
            or (source_job.request_json or {}).get("source_script_job_id")
            or ""
        )
        if not source_script_job_id:
            raise E2EError("固定来源 Job 缺少 source_script_job_id")
        return {
            "project_id": project.id,
            "project_title": project.title,
            "source_image_job_id": source_job.id,
            "source_script_job_id": source_script_job_id,
            "source_script_provider": source_result.get("source_script_provider"),
            "source_image_provider": source_result.get("image_provider"),
            "source_image_model_id": source_result.get("image_model_id"),
            "script": script,
            "source_trace": source_trace,
            "source_images": images,
        }


def _validate_audio_shots(
    settings: Settings,
    *,
    project_id: str,
    script: ScriptV1,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = result.get("audio_shots")
    if not isinstance(raw, list) or len(raw) != len(script.shots):
        raise E2EError("Job result_json 必须包含逐镜头完整真实旁白")
    by_id = {
        str(item.get("shot_id")): item
        for item in raw
        if isinstance(item, dict) and item.get("shot_id")
    }
    if set(by_id) != {shot.id for shot in script.shots}:
        raise E2EError("真实旁白 shot_id 集合与 ScriptV1 不一致")
    verified: list[dict[str, Any]] = []
    hashes: set[str] = set()
    for shot in script.shots:
        item = by_id[shot.id]
        if item.get("status") != "SUCCEEDED":
            raise E2EError(f"镜头 {shot.index} 真实旁白未成功生成")
        if item.get("provider_id") != REAL_AUDIO_PROVIDER_ID:
            raise E2EError(f"镜头 {shot.index} 旁白不是正式 Qwen3-TTS Provider")
        if item.get("source_type") != REAL_AUDIO_SOURCE_TYPE:
            raise E2EError(f"镜头 {shot.index} 旁白来源类型不是本地真实模型")
        if item.get("speaker") != "Serena" or item.get("language") != "Chinese":
            raise E2EError(f"镜头 {shot.index} 音色或语言与 Serena/Chinese 不一致")
        if item.get("text") != shot.narration:
            raise E2EError(f"镜头 {shot.index} 旁白文本被截断或改写")
        if item.get("reused") is not False:
            raise E2EError(f"首次 Serena E2E 不应复用既有旁白：{shot.id}")
        audio_path = _resolve_data_path(
            settings,
            value=item.get("audio_path"),
            project_id=project_id,
            label=f"镜头 {shot.index} WAV",
        )
        trace_path = _resolve_data_path(
            settings,
            value=item.get("trace_path"),
            project_id=project_id,
            label=f"镜头 {shot.index} 音频追溯",
        )
        technical = inspect_pcm16_wav(audio_path)
        actual_sha256 = str(technical["sha256"])
        if actual_sha256 != str(item.get("audio_sha256", "")).lower():
            raise E2EError(f"镜头 {shot.index} WAV SHA256 与 Job 追溯不一致")
        trace = _read_json(trace_path, label=f"镜头 {shot.index} 音频追溯")
        expected_trace = {
            "provider_id": REAL_AUDIO_PROVIDER_ID,
            "model_id": item.get("model_id"),
            "model_revision": item.get("model_revision"),
            "model_sha256": item.get("model_sha256"),
            "shot_id": shot.id,
            "text": shot.narration,
            "speaker": "Serena",
            "language": "Chinese",
            "audio_sha256": actual_sha256,
        }
        for key, expected in expected_trace.items():
            if trace.get(key) != expected:
                raise E2EError(f"镜头 {shot.index} 音频追溯字段不一致：{key}")
        duration = float(technical["duration_seconds"])
        if abs(float(item.get("duration_seconds", -1)) - duration) > 1e-6:
            raise E2EError(f"镜头 {shot.index} WAV 时长与 Job 追溯不一致")
        generation_seconds = float(item.get("generation_seconds", 0))
        real_time_factor = float(item.get("real_time_factor", 0))
        if generation_seconds <= 0 or real_time_factor <= 0:
            raise E2EError(f"镜头 {shot.index} 缺少有效生成耗时或 RTF")
        if abs(real_time_factor - generation_seconds / duration) > 2e-5:
            raise E2EError(f"镜头 {shot.index} RTF 与耗时/音频时长不一致")
        hashes.add(actual_sha256)
        verified.append(
            {
                "shot_id": shot.id,
                "shot_index": shot.index,
                "title": shot.title,
                "narration": shot.narration,
                "audio_path": str(audio_path),
                "trace_path": str(trace_path),
                "duration_seconds": duration,
                "generation_seconds": generation_seconds,
                "real_time_factor": real_time_factor,
                "sample_rate": technical["sample_rate"],
                "channels": technical["channels"],
                "sample_width_bytes": technical["sample_width_bytes"],
                "peak_amplitude": technical["peak_amplitude"],
                "rms": technical["rms"],
                "full_scale_ratio": technical["full_scale_ratio"],
                "full_decode_ok": technical["full_decode_ok"],
                "all_samples_finite": technical["all_samples_finite"],
                "audio_sha256": actual_sha256,
                "model_id": item.get("model_id"),
                "model_revision": item.get("model_revision"),
                "model_sha256": item.get("model_sha256"),
                "speaker": item.get("speaker"),
                "language": item.get("language"),
                "seed": item.get("seed"),
                "reused": False,
            }
        )
    if len(hashes) != len(script.shots):
        raise E2EError("三段旁白 WAV 内容必须互不相同")
    return verified


def _validate_timing_plan(
    *,
    script: ScriptV1,
    audio_shots: list[dict[str, Any]],
    result: dict[str, Any],
    max_total_duration_seconds: float,
) -> dict[str, Any]:
    plan = result.get("timing_plan")
    if not isinstance(plan, dict):
        raise E2EError("Job result_json 缺少媒体 timing_plan")
    plan_shots = plan.get("shots")
    if not isinstance(plan_shots, list) or len(plan_shots) != len(script.shots):
        raise E2EError("timing_plan 镜头数量与 ScriptV1 不一致")
    source_total = round(sum(float(shot.duration_seconds) for shot in script.shots), 6)
    if not 20.0 <= source_total <= 40.0:
        raise E2EError(f"源 ScriptV1 总时长越界：{source_total:.6f} 秒")
    audio_by_id = {item["shot_id"]: item for item in audio_shots}
    source_sum = 0.0
    rendered_sum = 0.0
    verified_shots: list[dict[str, Any]] = []
    for script_shot, timing in zip(script.shots, plan_shots, strict=True):
        if not isinstance(timing, dict) or timing.get("shot_id") != script_shot.id:
            raise E2EError("timing_plan 镜头顺序或 shot_id 与 ScriptV1 不一致")
        source_duration = float(timing["source_shot_duration"])
        audio_duration = float(timing["audio_duration"])
        lead_in = float(timing["lead_in_seconds"])
        lead_out = float(timing["lead_out_seconds"])
        rendered_duration = float(timing["rendered_shot_duration"])
        if abs(source_duration - float(script_shot.duration_seconds)) > 1e-6:
            raise E2EError(f"镜头 {script_shot.index} 源时长被修改")
        if abs(audio_duration - audio_by_id[script_shot.id]["duration_seconds"]) > 1e-6:
            raise E2EError(f"镜头 {script_shot.index} 旁白时长未进入 timing_plan")
        required = max(source_duration, audio_duration + lead_in + lead_out)
        if rendered_duration + 1e-6 < required:
            raise E2EError(f"镜头 {script_shot.index} 渲染时长会截断真实旁白")
        if abs(rendered_duration * 24 - round(rendered_duration * 24)) > 2e-5:
            raise E2EError(f"镜头 {script_shot.index} 渲染时长未对齐 24fps")
        source_sum += source_duration
        rendered_sum += rendered_duration
        verified_shots.append(dict(timing))
    rendered_sum = round(rendered_sum, 6)
    if abs(float(plan.get("source_total_duration_seconds", -1)) - source_sum) > 1e-6:
        raise E2EError("timing_plan 源总时长与逐镜头求和不一致")
    if abs(float(plan.get("rendered_total_duration_seconds", -1)) - rendered_sum) > 1e-6:
        raise E2EError("timing_plan 渲染总时长与逐镜头求和不一致")
    if rendered_sum > max_total_duration_seconds + 1e-6:
        raise E2EError(
            f"真实旁白渲染总时长 {rendered_sum:.3f} 秒超过上限 "
            f"{max_total_duration_seconds:.3f} 秒"
        )
    return {
        "plan_version": plan.get("timing_plan_version"),
        "source_total_duration_seconds": round(source_sum, 6),
        "rendered_total_duration_seconds": rendered_sum,
        "extended_by_seconds": round(rendered_sum - source_sum, 6),
        "max_total_duration_seconds": max_total_duration_seconds,
        "shots": verified_shots,
    }


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    source: dict[str, Any],
    script: ScriptV1,
    audio_shots: list[dict[str, Any]],
    timing: dict[str, Any],
    video_sha256: str,
) -> None:
    expected = {
        "manifest_version": "m5.real-audio-export.v1",
        "script_provider": "reused",
        "image_provider": REAL_IMAGE_PROVIDER_ID,
        "audio_provider": REAL_AUDIO_PROVIDER_ID,
        "speaker": "Serena",
        "language": "Chinese",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise E2EError(f"Manifest 字段不一致：{key}")
    pipeline = manifest.get("pipeline")
    if not isinstance(pipeline, dict):
        raise E2EError("Manifest 缺少 pipeline 追溯")
    if (
        pipeline.get("mock_audio_used") is not False
        or pipeline.get("audio_speed_changed") is not False
        or pipeline.get("audio_truncated") is not False
        or pipeline.get("cloud_api_used") is not False
    ):
        raise E2EError("Manifest 未证明真实旁白未 Mock、未变速、未截断且未使用云 API")
    context = manifest.get("generation_context")
    providers = context.get("providers") if isinstance(context, dict) else None
    script_trace = context.get("script") if isinstance(context, dict) else None
    if not isinstance(providers, dict) or not isinstance(script_trace, dict):
        raise E2EError("Manifest 缺少上游复用追溯")
    if script_trace.get("script_provider_calls") != 0:
        raise E2EError("Manifest 显示 ScriptProvider 被调用")
    if providers.get("image_provider_calls") != 0:
        raise E2EError("Manifest 显示 ImageProvider 被调用")
    if context.get("source_image_job_id") != source["source_image_job_id"]:
        raise E2EError("Manifest source_image_job_id 与固定来源不一致")
    manifest_shots = manifest.get("shots")
    if not isinstance(manifest_shots, list) or len(manifest_shots) != len(script.shots):
        raise E2EError("Manifest 必须包含三个完整镜头追溯")
    audio_by_id = {item["shot_id"]: item for item in audio_shots}
    for script_shot, manifest_shot in zip(script.shots, manifest_shots, strict=True):
        if (
            not isinstance(manifest_shot, dict)
            or manifest_shot.get("shot_id") != script_shot.id
        ):
            raise E2EError("Manifest 镜头顺序或 ID 与 ScriptV1 不一致")
        if manifest_shot.get("narration") != script_shot.narration:
            raise E2EError(f"Manifest 镜头 {script_shot.index} 旁白文本不完整")
        if manifest_shot.get("audio_text") != script_shot.narration:
            raise E2EError(f"Manifest 镜头 {script_shot.index} 音频文本不完整")
        if manifest_shot.get("audio_sha256") != audio_by_id[script_shot.id]["audio_sha256"]:
            raise E2EError(f"Manifest 镜头 {script_shot.index} WAV SHA256 不一致")
        if manifest_shot.get("subtitle_rendering") != "burned_in":
            raise E2EError(f"Manifest 镜头 {script_shot.index} 未记录烧录字幕")
    manifest_timing = manifest.get("timing_plan")
    if not isinstance(manifest_timing, dict):
        raise E2EError("Manifest 缺少 timing_plan")
    if abs(
        float(manifest_timing.get("rendered_planned_duration_seconds", -1))
        - timing["rendered_total_duration_seconds"]
    ) > 1e-6:
        raise E2EError("Manifest 渲染总时长与 Job timing_plan 不一致")
    output = manifest.get("output")
    if not isinstance(output, dict) or output.get("sha256") != video_sha256:
        raise E2EError("Manifest MP4 SHA256 与文件实算值不一致")


def _source_images_unchanged(source_images: list[dict[str, Any]]) -> bool:
    for item in source_images:
        path = Path(item["image_path"])
        if (
            not path.is_file()
            or path.stat().st_size != item["file_size_bytes"]
            or sha256_file(path) != item["image_sha256"]
        ):
            return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-image-job-id",
        default=DEFAULT_SOURCE_IMAGE_JOB_ID,
        help="固定复用的成功 M4-B 三镜头 Job",
    )
    parser.add_argument(
        "--speaker",
        choices=("Serena",),
        default="Serena",
        help="M5-B 首次真实 E2E 固定使用 Serena；Vivian 仅做单元测试",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(str(settings.database_url))
    database.create_schema()
    started = time.monotonic()
    source: dict[str, Any] | None = None
    created_job_id: str | None = None
    summary_path: Path | None = None
    baseline_gpu_mib: int | None = None
    cleanup: dict[str, Any] | None = None
    try:
        ports_before = _port_snapshot()
        if not all(ports_before.values()):
            occupied = [name.removesuffix("_free") for name, free in ports_before.items() if not free]
            raise E2EError("M5-B E2E 要求端口空闲：" + ", ".join(occupied))
        handoff_before = audio_gpu_handoff_status(settings)
        if handoff_before.get("conflict"):
            raise E2EError(
                "检测到 llama-server、ComfyUI 或其他已知高显存模型进程；"
                "拒绝与 Qwen3-TTS 同时驻留 GPU"
            )
        tts_processes_before = _tts_runner_processes()
        if tts_processes_before:
            raise E2EError("检测到遗留 qwen3_tts_job_runner.py 进程")
        baseline_gpu_mib = _gpu_memory_used_mib()
        if baseline_gpu_mib is None:
            raise E2EError("nvidia-smi 未返回可解析的 GPU 显存基线")

        source = _select_source(
            settings,
            database,
            source_image_job_id=args.source_image_job_id,
        )
        script: ScriptV1 = source["script"]
        print(
            "[m5-e2e] source_image_job="
            f"{source['source_image_job_id']} project={source['project_id']} "
            f"shots={len(script.shots)} speaker={args.speaker} "
            "script_provider_calls_expected=0 image_provider_calls_expected=0",
            flush=True,
        )

        app = create_app(settings, database=database)
        with TestClient(app) as client:
            response = client.post(
                f"{settings.api_prefix}/projects/{source['project_id']}/render-real-audio",
                json={
                    "source_image_job_id": source["source_image_job_id"],
                    "audio_provider": REAL_AUDIO_PROVIDER_ID,
                    "speaker": args.speaker,
                    "language": "Chinese",
                },
            )
        if response.status_code != 202:
            raise E2EError(
                f"真实旁白 Job 入队失败：HTTP {response.status_code} {response.text}"
            )
        created_job_id = str(response.json()["job_id"])
        print(f"[m5-e2e] queued job={created_job_id}", flush=True)

        worker = _NoUpstreamWorker(settings=settings, database=database)
        if not worker.run_once():
            raise E2EError("Worker 未领取刚创建的真实旁白 Job")
        with database.session() as session:
            job = session.get(GenerationJob, created_job_id)
            if job is None:
                raise E2EError("真实旁白 Job 处理后不存在")
            result = dict(job.result_json or {})
            if job.status != JobStatus.SUCCEEDED:
                raise E2EError(
                    "真实旁白 Job 未成功："
                    + json.dumps(
                        result.get("generation_error") or job.error_message,
                        ensure_ascii=False,
                    )
                )
            if job.job_type != REAL_AUDIO_JOB_TYPE or job.provider_id != REAL_AUDIO_PROVIDER_ID:
                raise E2EError("成功 Job 的类型或 Provider 不符合 M5-B 契约")

        if result.get("script_provider") != "reused" or result.get("script_provider_calls") != 0:
            raise E2EError("真实旁白 Job 错误调用或错误记录了 ScriptProvider")
        if result.get("image_provider") != "reused" or result.get("image_provider_calls") != 0:
            raise E2EError("真实旁白 Job 错误调用或错误记录了 ImageProvider")
        if result.get("source_image_job_id") != source["source_image_job_id"]:
            raise E2EError("真实旁白 Job 未复用指定 M4-B 来源")
        if result.get("source_image_provider") != REAL_IMAGE_PROVIDER_ID:
            raise E2EError("真实旁白 Job 未记录原真实 ImageProvider")
        if result.get("audio_provider") != REAL_AUDIO_PROVIDER_ID:
            raise E2EError("真实旁白 Job 未记录正式 Qwen3-TTS AudioProvider")
        if result.get("speaker") != "Serena" or result.get("language") != "Chinese":
            raise E2EError("真实旁白 Job 的音色或语言不符合 E2E 请求")
        if (
            result.get("mock_audio_fallback") is not False
            or result.get("mock_audio_used") is not False
            or result.get("cloud_api_used") is not False
            or result.get("voice_cloning_used") is not False
        ):
            raise E2EError("Job 未证明无 Mock、无云 API 且无声音克隆")
        if result.get("model_load_count") != 1:
            raise E2EError("三镜头真实旁白必须只加载一次模型")
        if result.get("sequential_generation") is not True:
            raise E2EError("三镜头真实旁白必须顺序生成")
        if result.get("max_audio_concurrency") != 1:
            raise E2EError("真实旁白最大并发必须为 1")
        if result.get("audio_generated_count") != 3 or result.get("audio_reused_count") != 0:
            raise E2EError("首次 Serena E2E 必须真实新生成 3 段 WAV")

        audio_shots = _validate_audio_shots(
            settings,
            project_id=source["project_id"],
            script=script,
            result=result,
        )
        timing = _validate_timing_plan(
            script=script,
            audio_shots=audio_shots,
            result=result,
            max_total_duration_seconds=float(settings.audio_rendered_max_seconds),
        )
        timing_path = _resolve_data_path(
            settings,
            value=result.get("timing_plan_path"),
            project_id=source["project_id"],
            label="媒体 timing_plan",
        )
        stored_timing = _read_json(timing_path, label="媒体 timing_plan")
        if stored_timing != result["timing_plan"]:
            raise E2EError("timing_plan 文件与 Job result_json 不一致")

        report_path = _resolve_data_path(
            settings,
            value=result.get("audio_generation_report_path"),
            project_id=source["project_id"],
            label="AudioProvider 生成报告",
        )
        provider_report = _read_json(report_path, label="AudioProvider 生成报告")
        runner_summary = provider_report.get("runner_summary")
        if not isinstance(runner_summary, dict):
            raise E2EError("AudioProvider 报告缺少 runner_summary")
        if (
            provider_report.get("status") != "SUCCEEDED"
            or provider_report.get("model_load_count") != 1
            or provider_report.get("generated_count") != 3
            or provider_report.get("reused_count") != 0
            or provider_report.get("child_process_exited") is not True
            or provider_report.get("mock_fallback") is not False
            or provider_report.get("cloud_api_used") is not False
            or provider_report.get("voice_cloning_used") is not False
            or runner_summary.get("oom") is not False
            or runner_summary.get("cpu_offload_used") is not False
        ):
            raise E2EError("AudioProvider 报告未满足一次加载、三段真实生成与清理契约")

        global_gpu = result.get("gpu_memory_observed")
        if not isinstance(global_gpu, dict):
            raise E2EError("Job 缺少 GPU 全卡显存观测")
        if (
            global_gpu.get("baseline_mib") is None
            or global_gpu.get("peak_mib") is None
            or int(global_gpu.get("sample_count", 0)) <= 0
            or int(global_gpu["peak_mib"]) <= int(global_gpu["baseline_mib"])
        ):
            raise E2EError("GPU 全卡显存观测没有捕获到真实模型加载峰值")

        video_path = _resolve_data_path(
            settings,
            value=result.get("video_path"),
            project_id=source["project_id"],
            label="M5-B MP4",
        )
        manifest_path = _resolve_data_path(
            settings,
            value=result.get("manifest_path"),
            project_id=source["project_id"],
            label="M5-B Manifest",
        )
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise E2EError(f"M5-B MP4 不存在或为空：{video_path}")
        video_sha256 = sha256_file(video_path)
        if video_sha256 != str(result.get("sha256", "")).lower():
            raise E2EError("MP4 SHA256 与 Job result_json 不一致")
        manifest = _read_json(manifest_path, label="M5-B Export Manifest")
        _validate_manifest(
            manifest,
            source=source,
            script=script,
            audio_shots=audio_shots,
            timing=timing,
            video_sha256=video_sha256,
        )

        tools = resolve_media_tools()
        verification = verify_media(
            tools,
            video_path,
            expected_width=1280,
            expected_height=720,
            expected_fps=24.0,
            expected_duration_seconds=timing["rendered_total_duration_seconds"],
        )
        full_decode = decode_media_fully(tools, video_path, timeout_seconds=600)
        midpoint_frames = extract_shot_midpoint_frames(
            tools,
            video_path,
            shot_durations=[
                float(item["rendered_shot_duration"]) for item in timing["shots"]
            ],
            output_dir=video_path.parent / "e2e-frames",
            filename_prefix="m5b-subtitle",
        )
        if len(midpoint_frames) != 3:
            raise E2EError("必须抽取三个镜头的中点字幕帧")

        source_images_unchanged = _source_images_unchanged(source["source_images"])
        if not source_images_unchanged:
            raise E2EError("M5-B 处理修改了来源 M4-B 真实 PNG")

        post_gpu_mib, gpu_released, release_wait_seconds = _wait_for_gpu_release(
            baseline_gpu_mib,
            timeout_seconds=float(settings.qwen_tts_gpu_release_timeout_seconds),
        )
        ports_after = _port_snapshot()
        tts_processes_after = _tts_runner_processes()
        handoff_after = audio_gpu_handoff_status(settings)
        cleanup = {
            "ports_before": ports_before,
            "ports_after": ports_after,
            "all_ports_released": all(ports_after.values()),
            "tts_runner_processes_before": tts_processes_before,
            "tts_runner_processes_after": tts_processes_after,
            "provider_owned_child_exited": provider_report.get("child_process_exited"),
            "known_gpu_model_conflict_after": handoff_after.get("conflict"),
            "gpu_baseline_mib": baseline_gpu_mib,
            "gpu_post_cleanup_mib": post_gpu_mib,
            "gpu_release_allowance_mib": GPU_RELEASE_ALLOWANCE_MIB,
            "gpu_released_to_baseline_allowance": gpu_released,
            "gpu_release_wait_seconds": release_wait_seconds,
            "gpu_compute_processes_after": _gpu_compute_processes(),
        }
        if not all(ports_after.values()):
            raise E2EError("M5-B 结束后 8000/8081/8188 未全部释放")
        if tts_processes_after:
            raise E2EError("M5-B 结束后仍有 qwen3_tts_job_runner.py 进程")
        if handoff_after.get("conflict"):
            raise E2EError("M5-B 结束后检测到已知高显存模型进程或端口")
        if not gpu_released:
            raise E2EError(
                "M5-B 结束后 GPU 显存未回落到基线 + "
                f"{GPU_RELEASE_ALLOWANCE_MIB} MiB"
            )

        summary = {
            "e2e_version": "m5b.real-audio-e2e.v1",
            "success": True,
            "finished_at": _utc_now(),
            "project_id": source["project_id"],
            "project_title": source["project_title"],
            "source_script_job_id": source["source_script_job_id"],
            "source_script_provider": source["source_script_provider"],
            "source_image_job_id": source["source_image_job_id"],
            "source_image_provider": source["source_image_provider"],
            "source_image_model_id": source["source_image_model_id"],
            "source_images": source["source_images"],
            "source_images_unchanged": source_images_unchanged,
            "job_id": created_job_id,
            "job_type": REAL_AUDIO_JOB_TYPE,
            "script_provider": "reused",
            "script_provider_calls": result.get("script_provider_calls"),
            "image_provider": "reused",
            "image_provider_calls": result.get("image_provider_calls"),
            "audio_provider": result.get("audio_provider"),
            "audio_model_id": result.get("audio_model_id"),
            "audio_model_revision": result.get("audio_model_revision"),
            "audio_model_sha256": result.get("audio_model_sha256"),
            "audio_model_license": result.get("audio_model_license"),
            "speaker": result.get("speaker"),
            "language": result.get("language"),
            "audio_shots": audio_shots,
            "audio_generation_total_seconds": result.get("audio_generation_total_seconds"),
            "model_load_count": result.get("model_load_count"),
            "model_load_seconds": runner_summary.get("model_load_seconds"),
            "sequential_generation": result.get("sequential_generation"),
            "max_audio_concurrency": result.get("max_audio_concurrency"),
            "mock_audio_used": False,
            "cloud_api_used": False,
            "voice_cloning_used": False,
            "cpu_offload_used": runner_summary.get("cpu_offload_used"),
            "oom": runner_summary.get("oom"),
            "timing_plan_path": str(timing_path),
            "timing": timing,
            "video_path": str(video_path),
            "video_sha256": video_sha256,
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "ffprobe_validation": verification,
            "full_decode": full_decode,
            "subtitle_midpoint_frames": midpoint_frames,
            "subtitles_burned_in_recorded": True,
            "gpu_memory_observed": global_gpu,
            "provider_gpu_allocator_observed": result.get(
                "provider_gpu_allocator_observed"
            ),
            "cleanup": cleanup,
            "tts_logs": provider_report.get("log_paths"),
            "complete_narration_without_truncation": True,
            "manual_listening_required": True,
            "manual_subtitle_frame_review_required": True,
            "total_wall_seconds": round(time.monotonic() - started, 3),
        }
        summary_path = video_path.parent / "m5b-e2e-summary.json"
        _atomic_json(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        print(f"[m5-e2e] summary={summary_path}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - 顶层必须给出明确非零退出码
        if baseline_gpu_mib is not None and cleanup is None:
            post_gpu_mib, gpu_released, release_wait_seconds = _wait_for_gpu_release(
                baseline_gpu_mib,
                timeout_seconds=float(settings.qwen_tts_gpu_release_timeout_seconds),
            )
            cleanup = {
                "ports_after": _port_snapshot(),
                "tts_runner_processes_after": _tts_runner_processes(),
                "gpu_baseline_mib": baseline_gpu_mib,
                "gpu_post_cleanup_mib": post_gpu_mib,
                "gpu_release_allowance_mib": GPU_RELEASE_ALLOWANCE_MIB,
                "gpu_released_to_baseline_allowance": gpu_released,
                "gpu_release_wait_seconds": release_wait_seconds,
                "gpu_compute_processes_after": _gpu_compute_processes(),
            }
        failure = {
            "e2e_version": "m5b.real-audio-e2e.v1",
            "success": False,
            "failed_at": _utc_now(),
            "job_id": created_job_id,
            "source_image_job_id": args.source_image_job_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "cleanup": cleanup,
            "total_wall_seconds": round(time.monotonic() - started, 3),
        }
        if source is not None:
            failure_dir = (
                settings.project_dir(source["project_id"])
                / "exports"
                / (created_job_id or "m5b-e2e-preflight")
            )
            summary_path = failure_dir / "m5b-e2e-summary.json"
            try:
                _atomic_json(summary_path, failure)
            except OSError:
                pass
        print(f"[m5-e2e] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        if created_job_id:
            print(f"[m5-e2e] job={created_job_id}", file=sys.stderr)
        if summary_path is not None:
            print(f"[m5-e2e] summary={summary_path}", file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""M5-B 真实旁白 Job 的轻量校验、错误与 GPU 交接工具。"""

from __future__ import annotations

from array import array
import csv
from dataclasses import replace
import hashlib
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from ..providers.base import (
    AudioGenerationRequest,
    GeneratedAudioAsset,
)
from ..script_schema import ScriptV1


REAL_AUDIO_JOB_TYPE = "GENERATE_REAL_AUDIO_VIDEO"
REAL_AUDIO_PROVIDER_ID = "qwen3-tts-0.6b-customvoice"
REAL_AUDIO_SOURCE_TYPE = "REAL_LOCAL_MODEL"

MIN_AUDIBLE_PEAK = 1e-3
MIN_AUDIBLE_RMS = 1e-4
MAX_FULL_SCALE_RATIO = 1e-3


class AudioValidationError(ValueError):
    """WAV 技术验收失败，并保留稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RealAudioJobError(RuntimeError):
    """携带可以直接落入 Job result_json 的结构化失败。"""

    def __init__(
        self,
        *,
        code: str,
        stage: str,
        summary: str,
        failed_shot_id: str | None = None,
        failed_shot_index: int | None = None,
        completed_audio_count: int = 0,
        total_audio_count: int | None = None,
        retryable: bool = True,
        oom: bool = False,
        requires_qwen_shutdown: bool = False,
        requires_comfyui_shutdown: bool = False,
        log_paths: dict[str, str] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(summary)
        self.generation_error = {
            "code": code,
            "stage": stage,
            "summary": summary,
            "failed_shot_id": failed_shot_id,
            "failed_shot_index": failed_shot_index,
            "completed_audio_count": completed_audio_count,
            "total_audio_count": total_audio_count,
            "retryable": retryable,
            "oom": oom,
            "requires_qwen_shutdown": requires_qwen_shutdown,
            "requires_comfyui_shutdown": requires_comfyui_shutdown,
            "log_paths": log_paths or {},
            "suggestions": suggestions or [],
            "provider_id": REAL_AUDIO_PROVIDER_ID,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
        }


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pcm16_wav(path: Path) -> dict[str, Any]:
    """完整读取单个 PCM16 WAV，并执行确定性的技术验收。"""

    if not path.is_file() or path.stat().st_size <= 44:
        raise AudioValidationError("AUDIO_OUTPUT_MISSING", f"WAV 不存在或为空：{path}")
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            raw = source.readframes(frame_count)
            trailing = source.readframes(1)
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED", f"WAV 无法完整解码：{path}: {exc}"
        ) from exc
    if compression != "NONE":
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED", f"WAV 不是未压缩 PCM：{compression}"
        )
    if sample_width != 2:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED",
            f"WAV 必须为 PCM16，实际 sample_width={sample_width}",
        )
    if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED", "WAV 声道、采样率和帧数必须大于 0"
        )
    expected_bytes = frame_count * channels * sample_width
    if len(raw) != expected_bytes or trailing:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED",
            f"WAV PCM 数据长度不符：要求 {expected_bytes}，实际 {len(raw)}"
        )

    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) != frame_count * channels:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED", "WAV 解码后的样本数量不符"
        )
    peak_raw = max(abs(int(value)) for value in samples)
    sum_squares = sum(int(value) * int(value) for value in samples)
    peak = peak_raw / 32768.0
    rms = math.sqrt(sum_squares / len(samples)) / 32768.0
    full_scale_count = sum(value in (-32768, 32767) for value in samples)
    full_scale_ratio = full_scale_count / len(samples)
    duration = frame_count / sample_rate
    if not math.isfinite(duration) or duration <= 0:
        raise AudioValidationError("AUDIO_DECODE_FAILED", "WAV 时长无效")
    if peak <= MIN_AUDIBLE_PEAK or rms <= MIN_AUDIBLE_RMS:
        raise AudioValidationError(
            "AUDIO_SILENT",
            f"WAV 近似静音：peak={peak:.9f}, rms={rms:.9f}"
        )
    if full_scale_ratio > MAX_FULL_SCALE_RATIO:
        raise AudioValidationError(
            "AUDIO_DECODE_FAILED",
            "WAV 存在明显数字削波："
            f"full_scale_ratio={full_scale_ratio:.9f}"
        )
    return {
        "format": "WAV PCM_16",
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frame_count": frame_count,
        "duration_seconds": round(duration, 6),
        "peak_amplitude": round(peak, 9),
        "rms": round(rms, 9),
        "full_scale_sample_count": full_scale_count,
        "full_scale_ratio": round(full_scale_ratio, 9),
        "file_size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "full_decode_ok": True,
        "all_samples_finite": True,
    }


def validate_reusable_audio_asset(
    *,
    asset: GeneratedAudioAsset,
    request: AudioGenerationRequest,
    provider_id: str,
    model_id: str,
    model_revision: str,
    model_sha256: str,
) -> tuple[GeneratedAudioAsset | None, str | None]:
    """只复用输入、模型和文件完整性均完全一致的旁白。"""

    expected_seed = request.options.base_seed + request.shot.index
    expected = (
        asset.provider_id == provider_id
        and asset.model_id == model_id
        and asset.model_revision == model_revision
        and asset.model_sha256 == model_sha256
        and asset.shot_id == request.shot.id
        and asset.text == request.shot.narration
        and asset.speaker.casefold() == request.options.speaker.casefold()
        and asset.language.casefold() == request.options.language.casefold()
        and asset.seed == expected_seed
    )
    if not expected:
        return None, "旁白复用 DTO 与当前请求快照不一致"
    trace = read_json(Path(asset.trace_path))
    if trace is None:
        return None, "旁白复用 trace 缺失或不是合法 JSON"
    expected_trace = {
        "provider_id": provider_id,
        "model_id": model_id,
        "model_revision": model_revision,
        "model_sha256": model_sha256,
        "source_script_job_id": request.source_script_job_id,
        "source_image_job_id": request.source_image_job_id,
        "shot_id": request.shot.id,
        "shot_index": request.shot.index,
        "text": request.shot.narration,
        "text_sha256": hashlib.sha256(
            request.shot.narration.encode("utf-8")
        ).hexdigest(),
        "speaker": request.options.speaker,
        "language": request.options.language,
        "seed": expected_seed,
        "audio_sha256": asset.audio_sha256,
        "sample_rate": asset.sample_rate,
        "channels": asset.channels,
        "sample_width_bytes": asset.sample_width_bytes,
    }
    for key, expected_value in expected_trace.items():
        if trace.get(key) != expected_value:
            return None, f"旁白复用 trace 字段 {key} 与当前请求不一致"
    try:
        if Path(str(trace.get("audio_path", ""))).resolve() != Path(
            asset.audio_path
        ).resolve():
            return None, "旁白复用 trace 的 audio_path 与 DTO 不一致"
    except (OSError, ValueError):
        return None, "旁白复用 trace 的 audio_path 无效"
    try:
        validation = inspect_pcm16_wav(Path(asset.audio_path))
    except AudioValidationError as exc:
        return None, str(exc)
    if validation["sha256"] != asset.audio_sha256:
        return None, "旁白文件 SHA256 与 DTO 不一致"
    for key, expected_value in (
        ("sample_rate", asset.sample_rate),
        ("channels", asset.channels),
        ("sample_width_bytes", asset.sample_width_bytes),
    ):
        if validation[key] != expected_value:
            return None, f"旁白文件 {key} 与 DTO 不一致"
    if abs(float(validation["duration_seconds"]) - asset.duration_seconds) > 1e-6:
        return None, "旁白文件时长与 DTO 不一致"
    return replace(asset, reused=True), None


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _known_gpu_model_processes() -> list[str]:
    """只读识别有明确进程名的模型运行时；不把普通 python.exe 当作冲突。"""

    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    markers = ("llama-server", "comfyui", "ollama", "stable-diffusion")
    detected = {
        row[0]
        for row in csv.reader(io.StringIO(completed.stdout))
        if row and any(marker in row[0].casefold() for marker in markers)
    }
    return sorted(detected, key=str.casefold)


def _gpu_memory_used_mib() -> int | None:
    """读取整卡显存占用；Windows/WDDM 下不依赖进程名识别。"""

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
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.splitlines()[0].strip())
    except (IndexError, ValueError):
        return None


def audio_gpu_handoff_status(
    settings: Any | None = None,
    *,
    llama_host: str | None = None,
    llama_port: int | None = None,
    comfyui_host: str | None = None,
    comfyui_port: int | None = None,
    gpu_memory_used_mib: int | None = None,
    gpu_memory_limit_mib: int | None = None,
) -> dict[str, Any]:
    """只读检查已知本地模型端口；绝不结束外部进程。"""

    if settings is not None:
        parsed = urlparse(str(getattr(settings, "llama_server_base_url", "")))
        llama_host = llama_host or parsed.hostname
        llama_port = llama_port or parsed.port
        comfyui_host = comfyui_host or getattr(settings, "comfyui_host", None)
        comfyui_port = comfyui_port or getattr(settings, "comfyui_port", None)
        gpu_memory_limit_mib = gpu_memory_limit_mib or int(
            getattr(settings, "audio_gpu_handoff_max_used_mib", 2_048)
        )
    resolved_llama_host = llama_host or "127.0.0.1"
    resolved_llama_port = int(llama_port or 8081)
    resolved_comfyui_host = comfyui_host or "127.0.0.1"
    resolved_comfyui_port = int(comfyui_port or 8188)
    llama = _port_listening(resolved_llama_host, resolved_llama_port)
    comfy = _port_listening(resolved_comfyui_host, resolved_comfyui_port)
    detected_processes = _known_gpu_model_processes()
    folded_processes = [value.casefold() for value in detected_processes]
    llama_process = any("llama-server" in value for value in folded_processes)
    comfyui_process = any("comfyui" in value for value in folded_processes)
    observed_gpu_memory_mib = (
        _gpu_memory_used_mib()
        if gpu_memory_used_mib is None
        else int(gpu_memory_used_mib)
    )
    resolved_gpu_limit_mib = int(gpu_memory_limit_mib or 2_048)
    gpu_memory_conflict = (
        observed_gpu_memory_mib is not None
        and observed_gpu_memory_mib > resolved_gpu_limit_mib
    )
    return {
        "conflict": llama or comfy or bool(detected_processes) or gpu_memory_conflict,
        "llama_port_listening": llama,
        "comfyui_port_listening": comfy,
        "known_gpu_model_process_detected": bool(detected_processes),
        "detected_process_names": detected_processes,
        "llama_process_detected": llama_process,
        "comfyui_process_detected": comfyui_process,
        "gpu_memory_used_mib": observed_gpu_memory_mib,
        "gpu_memory_limit_mib": resolved_gpu_limit_mib,
        "gpu_memory_conflict": gpu_memory_conflict,
        "llama_host": resolved_llama_host,
        "llama_port": resolved_llama_port,
        "comfyui_host": resolved_comfyui_host,
        "comfyui_port": resolved_comfyui_port,
    }


def audio_gpu_handoff_error_payload(
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回可直接写入 Job result_json 的 GPU 交接失败。"""

    snapshot = dict(status or {})
    requires_qwen_shutdown = bool(
        snapshot.get("llama_port_listening")
        or snapshot.get("llama_process_detected")
    )
    requires_comfyui_shutdown = bool(
        snapshot.get("comfyui_port_listening")
        or snapshot.get("comfyui_process_detected")
    )
    suggestions: list[str] = []
    if requires_qwen_shutdown:
        suggestions.append("停止 llama-server 并确认 8081 端口已释放。")
    if requires_comfyui_shutdown:
        suggestions.append("停止 ComfyUI 并确认 8188 端口已释放。")
    if snapshot.get("gpu_memory_conflict"):
        suggestions.append(
            "关闭其他高显存程序，使整卡占用不高于 "
            f"{snapshot.get('gpu_memory_limit_mib')} MiB。"
        )
    suggestions.append("释放显存后手动重试；平台不会终止用户启动的外部进程。")
    payload = RealAudioJobError(
        code="GPU_HANDOFF_REQUIRED",
        stage="GPU_HANDOFF_REQUIRED",
        summary="本机 8GB 显存模式检测到模型服务或高显存占用，需要先完成 GPU 交接。",
        requires_qwen_shutdown=requires_qwen_shutdown,
        requires_comfyui_shutdown=requires_comfyui_shutdown,
        suggestions=suggestions,
    ).generation_error
    payload["gpu_memory_used_mib"] = snapshot.get("gpu_memory_used_mib")
    payload["gpu_memory_limit_mib"] = snapshot.get("gpu_memory_limit_mib")
    payload["detected_process_names"] = snapshot.get("detected_process_names", [])
    return payload


def _normalize_source_images(
    script: ScriptV1,
    source_images: Sequence[dict[str, Any]],
    *,
    source_image_provider: str,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for position, original in enumerate(source_images, start=1):
        if not isinstance(original, dict):
            raise ValueError(f"source_images[{position - 1}] 必须是对象")
        item = dict(original)
        shot_id = item.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id:
            raise ValueError(f"source_images[{position - 1}] 缺少 shot_id")
        if shot_id in by_id:
            raise ValueError(f"source_images shot_id 重复：{shot_id}")
        if item.get("status") not in {None, "SUCCEEDED", "REUSED"}:
            raise ValueError(f"来源图片尚未成功：{shot_id}")
        provider_id = item.get("provider_id")
        if provider_id not in {None, source_image_provider}:
            raise ValueError(f"来源图片 Provider 与 Job 不一致：{shot_id}")
        for key in ("image_path", "image_sha256"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"来源图片 {shot_id} 缺少 {key}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item["image_sha256"]).lower()):
            raise ValueError(f"来源图片 {shot_id} 的 image_sha256 无效")
        item["provider_id"] = source_image_provider
        by_id[shot_id] = item

    expected_ids = [shot.id for shot in script.shots]
    if set(by_id) != set(expected_ids):
        raise ValueError(
            "来源图片镜头集合与 ScriptV1 不一致："
            f"预期 {expected_ids}，实际 {list(by_id)}"
        )
    normalized: list[dict[str, Any]] = []
    for shot in script.shots:
        item = by_id[shot.id]
        item_index = item.get("shot_index", shot.index)
        if type(item_index) is not int or item_index != shot.index:
            raise ValueError(f"来源图片 shot_index 与 ScriptV1 不一致：{shot.id}")
        item["shot_index"] = shot.index
        normalized.append(item)
    return normalized


def create_audio_source_snapshot(
    settings: Any,
    *,
    project_id: str,
    audio_job_id: str,
    source_script_job_id: str,
    source_image_job_id: str | None,
    source_script_provider: str,
    source_image_provider: str | None,
    script: ScriptV1,
    source_images: Sequence[dict[str, Any]],
    source_trace: dict[str, Any],
) -> tuple[Path, str]:
    """原子固化 M5-B 的 ScriptV1 与真实关键帧追溯，不调用任何模型。"""

    required_identifiers = {
        "project_id": project_id,
        "audio_job_id": audio_job_id,
        "source_script_job_id": source_script_job_id,
        "source_script_provider": source_script_provider,
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in required_identifiers.values()
    ):
        raise ValueError("音频来源快照的项目、Script Job 和 Provider 标识不得为空")
    if (source_image_job_id is None) != (source_image_provider is None):
        raise ValueError("Image Job 与 ImageProvider 追溯必须同时提供或同时省略")
    if not isinstance(script, ScriptV1):
        raise TypeError("script 必须是已严格校验的 ScriptV1")
    if not isinstance(source_trace, dict):
        raise TypeError("source_trace 必须是对象")
    images = (
        _normalize_source_images(
            script,
            source_images,
            source_image_provider=str(source_image_provider),
        )
        if source_image_job_id is not None
        else []
    )
    if source_image_job_id is None and source_images:
        raise ValueError("没有 Image Job 追溯时不得把图片写入 Audio 来源快照")
    job_root = (settings.project_dir(project_id) / "jobs" / audio_job_id).resolve()
    job_root.mkdir(parents=True, exist_ok=True)
    path = job_root / "audio-source.json"
    payload = {
        "snapshot_version": "m8.audio-source.v1",
        **required_identifiers,
        "source_image_job_id": source_image_job_id,
        "source_image_provider": source_image_provider,
        "validated_script": script.model_dump(mode="json"),
        "source_images": images,
        "source_trace": source_trace,
    }
    atomic_json(path, payload)
    return path, sha256_file(path)


def load_audio_source_snapshot(
    settings: Any,
    *,
    project_id: str,
    audio_job_id: str,
    request_snapshot: dict[str, Any],
) -> tuple[ScriptV1, dict[str, Any]]:
    """校验归属、SHA 与两个来源 Job 后读取不可变 M5-B 来源快照。"""

    path_value = request_snapshot.get(
        "audio_source_snapshot_path", request_snapshot.get("source_snapshot_path")
    )
    expected_sha = request_snapshot.get(
        "audio_source_snapshot_sha256", request_snapshot.get("source_snapshot_sha256")
    )
    owner_job_id = request_snapshot.get(
        "audio_source_snapshot_owner_job_id",
        request_snapshot.get("source_snapshot_owner_job_id", audio_job_id),
    )
    if not isinstance(path_value, str) or not path_value:
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_MISSING",
            stage="SOURCE_REUSE",
            summary="真实旁白 Job 缺少来源快照路径。",
            retryable=False,
        )
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_INVALID",
            stage="SOURCE_REUSE",
            summary="真实旁白 Job 缺少来源快照 SHA256。",
            retryable=False,
        )
    if not isinstance(owner_job_id, str) or not owner_job_id:
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_INVALID",
            stage="SOURCE_REUSE",
            summary="真实旁白 Job 缺少来源快照归属 Job。",
            retryable=False,
        )
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(settings.data_dir) / path
    path = path.resolve()
    owner_root = (settings.project_dir(project_id) / "jobs" / owner_job_id).resolve()
    try:
        path.relative_to(owner_root)
    except ValueError as exc:
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_INVALID",
            stage="SOURCE_REUSE",
            summary="真实旁白来源快照路径越过归属 Job 目录。",
            retryable=False,
        ) from exc
    if not path.is_file() or sha256_file(path) != expected_sha.lower():
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_INVALID",
            stage="SOURCE_REUSE",
            summary="真实旁白来源快照缺失或 SHA256 不匹配。",
            retryable=False,
        )
    payload = read_json(path)
    try:
        if payload is None or payload.get("snapshot_version") not in {
            "m5.audio-source.v1",
            "m8.audio-source.v1",
        }:
            raise ValueError("snapshot_version 无效")
        if payload.get("project_id") != project_id:
            raise ValueError("project_id 不匹配")
        if payload.get("audio_job_id") != owner_job_id:
            raise ValueError("快照归属 Job 不匹配")
        for key in ("source_script_job_id", "source_image_job_id"):
            if payload.get(key) != request_snapshot.get(key):
                raise ValueError(f"{key} 不匹配")
        for key in ("source_script_provider", "source_image_provider"):
            expected = request_snapshot.get(key)
            if expected is not None and payload.get(key) != expected:
                raise ValueError(f"{key} 不匹配")
        script = ScriptV1.model_validate(payload["validated_script"])
        image_job_id = payload.get("source_image_job_id")
        image_provider = payload.get("source_image_provider")
        if (image_job_id is None) != (image_provider is None):
            raise ValueError("Image Job 与 ImageProvider 追溯不一致")
        images = (
            _normalize_source_images(
                script,
                payload["source_images"],
                source_image_provider=str(image_provider),
            )
            if image_job_id is not None
            else []
        )
        if image_job_id is None and payload.get("source_images") not in (None, []):
            raise ValueError("无 Image Job 的音频快照不得包含图片")
        payload["source_images"] = images
    except (KeyError, TypeError, ValueError) as exc:
        raise RealAudioJobError(
            code="AUDIO_SOURCE_SNAPSHOT_INVALID",
            stage="SOURCE_REUSE",
            summary=f"真实旁白来源快照无法通过严格校验：{exc}",
            retryable=False,
        ) from exc
    return script, payload


def build_media_timing_plan(
    *,
    script: ScriptV1,
    audio_assets: Sequence[GeneratedAudioAsset],
    fps: int = 24,
    lead_in_seconds: float = 0.20,
    lead_out_seconds: float = 0.35,
    max_total_duration_seconds: float = 60.0,
) -> dict[str, Any]:
    """按音频实测时长与视频帧边界生成确定性的媒体时序计划。"""

    if type(fps) is not int or fps <= 0:
        raise ValueError("fps 必须是正整数")
    if lead_in_seconds < 0 or lead_out_seconds < 0:
        raise ValueError("旁白 lead-in 和 lead-out 不得为负数")
    if not math.isfinite(max_total_duration_seconds) or max_total_duration_seconds <= 0:
        raise ValueError("媒体总时长上限必须大于 0")
    by_id = {asset.shot_id: asset for asset in audio_assets}
    if len(by_id) != len(audio_assets):
        raise ValueError("真实旁白 shot_id 不得重复")
    expected_ids = [shot.id for shot in script.shots]
    if set(by_id) != set(expected_ids):
        raise ValueError("真实旁白镜头集合与 ScriptV1 不一致")

    source_total = sum(float(shot.duration_seconds) for shot in script.shots)
    if not 20.0 <= source_total <= 40.0:
        raise ValueError(
            f"ScriptV1 计划总时长必须在 20—40 秒内，实际 {source_total:.3f} 秒"
        )
    shots: list[dict[str, Any]] = []
    rendered_total = 0.0
    for shot in script.shots:
        asset = by_id[shot.id]
        audio_duration = float(asset.duration_seconds)
        if not math.isfinite(audio_duration) or audio_duration <= 0:
            raise ValueError(f"真实旁白时长无效：{shot.id}")
        source_duration = float(shot.duration_seconds)
        padded_audio_duration = audio_duration + lead_in_seconds + lead_out_seconds
        raw_rendered_duration = max(source_duration, padded_audio_duration)
        rendered_duration = math.ceil(raw_rendered_duration * fps - 1e-9) / fps
        extension = max(0.0, rendered_duration - source_duration)
        if padded_audio_duration > source_duration + 1e-9:
            reason = "REAL_AUDIO_WITH_PADDING_EXCEEDS_SOURCE_SHOT"
        elif extension > 1e-9:
            reason = "SOURCE_SHOT_FRAME_ALIGNMENT"
        else:
            reason = "NO_EXTENSION"
        shots.append(
            {
                "shot_id": shot.id,
                "shot_index": shot.index,
                "source_shot_duration": round(source_duration, 6),
                "source_duration_seconds": round(source_duration, 6),
                "audio_duration": round(audio_duration, 6),
                "audio_duration_seconds": round(audio_duration, 6),
                "lead_in_seconds": round(lead_in_seconds, 6),
                "lead_out_seconds": round(lead_out_seconds, 6),
                "rendered_shot_duration": round(rendered_duration, 6),
                "rendered_duration_seconds": round(rendered_duration, 6),
                "extended_by_seconds": round(extension, 6),
                "extension_seconds": round(extension, 6),
                "extension_reason": reason,
            }
        )
        rendered_total += rendered_duration
    if rendered_total > max_total_duration_seconds + 1e-6:
        raise RealAudioJobError(
            code="AUDIO_TIMING_EXCEEDS_LIMIT",
            stage="AUDIO_TIMING",
            summary=(
                f"真实旁白渲染总时长 {rendered_total:.3f} 秒超过上限 "
                f"{max_total_duration_seconds:.3f} 秒。"
            ),
            completed_audio_count=len(audio_assets),
            total_audio_count=len(script.shots),
            retryable=False,
            suggestions=["改用更短的预置音色或缩短剧本旁白后重新生成。"],
        )
    return {
        "timing_plan_version": "m5.audio-timing.v1",
        "fps": fps,
        "lead_in_seconds": round(lead_in_seconds, 6),
        "lead_out_seconds": round(lead_out_seconds, 6),
        "source_total_duration_seconds": round(source_total, 6),
        "rendered_total_duration_seconds": round(rendered_total, 6),
        "max_total_duration_seconds": round(max_total_duration_seconds, 6),
        "shots": shots,
    }


__all__ = [
    "REAL_AUDIO_JOB_TYPE",
    "REAL_AUDIO_PROVIDER_ID",
    "REAL_AUDIO_SOURCE_TYPE",
    "AudioValidationError",
    "RealAudioJobError",
    "atomic_json",
    "audio_gpu_handoff_error_payload",
    "audio_gpu_handoff_status",
    "build_media_timing_plan",
    "create_audio_source_snapshot",
    "inspect_pcm16_wav",
    "load_audio_source_snapshot",
    "read_json",
    "sha256_file",
    "validate_reusable_audio_asset",
]

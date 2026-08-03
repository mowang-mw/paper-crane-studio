"""有界 Qwen3-TTS 本地 AudioProvider；后端进程不导入 PyTorch。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable

from .base import (
    AudioGenerationOptions,
    AudioGenerationRequest,
    AudioPlan,
    AudioProvider,
    GeneratedAudioAsset,
    ScriptShot,
)
from ..services.audio_jobs import (
    REAL_AUDIO_PROVIDER_ID,
    REAL_AUDIO_SOURCE_TYPE,
    AudioValidationError,
    RealAudioJobError,
    atomic_json,
    audio_gpu_handoff_status,
    inspect_pcm16_wav,
    read_json,
    sha256_file,
    validate_reusable_audio_asset,
)


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_LICENSE = "Apache-2.0"
PACKAGE_VERSION = "0.1.1"
MODEL_FILE_COUNT = 13
MODEL_TOTAL_SIZE_BYTES = 2_498_388_392


@dataclass(frozen=True, slots=True)
class ModelFileExpectation:
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.size_bytes <= 0:
            raise ValueError("模型文件大小必须大于 0")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256.lower()):
            raise ValueError("模型文件 SHA256 格式无效")


DEFAULT_MODEL_EXPECTATIONS = {
    "model.safetensors": ModelFileExpectation(
        1_811_626_576,
        "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb",
    ),
    "speech_tokenizer/model.safetensors": ModelFileExpectation(
        682_293_092,
        "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
    ),
}

ProgressCallback = Callable[[int, int, GeneratedAudioAsset], None]
HandoffCheck = Callable[[], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata_path(model_dir: Path, relative: str) -> Path:
    path = Path(relative)
    return (
        model_dir
        / ".cache"
        / "huggingface"
        / "download"
        / path.parent
        / f"{path.name}.metadata"
    )


def _terminate_owned_process(process: subprocess.Popen[Any]) -> dict[str, Any]:
    methods: list[str] = []
    if process.poll() is not None:
        return {"exited": True, "returncode": process.returncode, "methods": methods}
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            methods.append("CTRL_BREAK_EVENT")
            process.wait(timeout=10.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            process.terminate()
            methods.append("terminate")
            process.wait(timeout=10.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=30.0,
                shell=False,
            )
            methods.append("taskkill-tree")
            process.wait(timeout=30.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
            methods.append("kill")
            process.wait(timeout=10.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "exited": process.poll() is not None,
        "returncode": process.returncode,
        "methods": methods,
    }


class Qwen3TTSAudioProvider(AudioProvider):
    """一次子进程加载模型，按镜头顺序生成所有缺失旁白。"""

    provider_id = REAL_AUDIO_PROVIDER_ID
    source_type = REAL_AUDIO_SOURCE_TYPE
    model_id = MODEL_ID
    model_revision = MODEL_REVISION

    def __init__(
        self,
        *,
        tts_python: Path,
        model_path: Path,
        runner_path: Path,
        model_id: str = MODEL_ID,
        model_revision: str = MODEL_REVISION,
        model_sha256: str | None = None,
        model_license: str = MODEL_LICENSE,
        package_version: str = PACKAGE_VERSION,
        model_expectations: dict[str, ModelFileExpectation] | None = None,
        expected_model_file_count: int | None = MODEL_FILE_COUNT,
        expected_model_total_size_bytes: int | None = MODEL_TOTAL_SIZE_BYTES,
        handoff_check: HandoffCheck | None = None,
    ) -> None:
        self.tts_python = Path(tts_python).resolve()
        self.model_path = Path(model_path).resolve()
        self.runner_path = Path(runner_path).resolve()
        self.model_id = model_id.strip()
        self.model_revision = model_revision.strip().lower()
        self.model_license = model_license.strip()
        self.package_version = package_version.strip()
        self.model_expectations = dict(
            model_expectations or DEFAULT_MODEL_EXPECTATIONS
        )
        primary_expectation = self.model_expectations.get("model.safetensors")
        if primary_expectation is None and self.model_expectations:
            primary_expectation = next(iter(self.model_expectations.values()))
        self.model_sha256 = (
            model_sha256
            or (primary_expectation.sha256 if primary_expectation is not None else "")
        ).strip().lower()
        self.expected_model_file_count = expected_model_file_count
        self.expected_model_total_size_bytes = expected_model_total_size_bytes
        self.handoff_check = handoff_check or audio_gpu_handoff_status
        self.last_run_report: dict[str, Any] | None = None
        if not self.model_id or not self.model_license or not self.package_version:
            raise ValueError("模型 ID、许可证和包版本不得为空")
        if not re.fullmatch(r"[0-9a-f]{40}", self.model_revision):
            raise ValueError("model_revision 必须是 40 位十六进制 commit")
        if not re.fullmatch(r"[0-9a-f]{64}", self.model_sha256):
            raise ValueError("model_sha256 必须是 64 位十六进制 SHA256")
        if (
            primary_expectation is not None
            and self.model_sha256 != primary_expectation.sha256.lower()
        ):
            raise ValueError("model_sha256 必须与主模型文件校验值一致")

    def plan(self, *, shot: ScriptShot) -> AudioPlan:
        return AudioPlan(
            provider_id=self.provider_id,
            source_type=self.source_type,
            parameters={
                "speaker_strategy": "one_preset_speaker_per_job",
                "text_source": "ScriptV1.shot.narration",
                "shot_index": shot.shot_index,
                "audio_format": "WAV PCM_16",
            },
        )

    def generate(self, *, request: AudioGenerationRequest) -> GeneratedAudioAsset:
        return self._generate_requests(
            requests=(request,),
            reusable_assets=(),
            progress_callback=None,
            enforce_job_shot_count=False,
        )[0]

    def generate_batch(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...] = (),
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[GeneratedAudioAsset, ...]:
        return self._generate_requests(
            requests=requests,
            reusable_assets=reusable_assets,
            progress_callback=progress_callback,
            enforce_job_shot_count=True,
        )

    def _validate_requests(
        self,
        requests: tuple[AudioGenerationRequest, ...],
        *,
        enforce_job_shot_count: bool,
    ) -> None:
        if not requests:
            raise ValueError("至少需要一个 AudioGenerationRequest")
        if enforce_job_shot_count and not 3 <= len(requests) <= 5:
            raise ValueError("真实旁白 Job 必须包含 3—5 个镜头")
        indices = [item.shot.index for item in requests]
        if enforce_job_shot_count and indices != list(range(1, len(requests) + 1)):
            raise ValueError(f"旁白镜头必须连续排序，实际 {indices}")
        first = requests[0]
        for item in requests:
            if (
                item.project_id != first.project_id
                or item.job_id != first.job_id
                or item.source_script_job_id != first.source_script_job_id
                or item.source_image_job_id != first.source_image_job_id
                or item.script is not first.script
                or item.output_dir != first.output_dir
                or item.options != first.options
            ):
                raise ValueError("同一批旁白请求必须共享项目、Job、Script、目录和参数")
        if len({item.shot.id for item in requests}) != len(requests):
            raise ValueError("旁白请求 shot_id 不得重复")

    def _validate_model(self, report_path: Path) -> dict[str, Any]:
        if not self.tts_python.is_file():
            raise RealAudioJobError(
                code="TTS_ENV_NOT_FOUND",
                stage="AUDIO_GENERATION",
                summary=f"独立 TTS Python 不存在：{self.tts_python}",
                retryable=False,
            )
        if not self.runner_path.is_file():
            raise RealAudioJobError(
                code="TTS_ENV_NOT_FOUND",
                stage="AUDIO_GENERATION",
                summary=f"TTS runner 不存在：{self.runner_path}",
                retryable=False,
            )
        if not self.model_path.is_dir():
            raise RealAudioJobError(
                code="TTS_MODEL_NOT_FOUND",
                stage="AUDIO_GENERATION",
                summary=f"TTS 模型目录不存在：{self.model_path}",
                retryable=False,
            )
        files = sorted(
            path
            for path in self.model_path.rglob("*")
            if path.is_file() and ".cache" not in path.relative_to(self.model_path).parts
        )
        total_size = sum(path.stat().st_size for path in files)
        if (
            self.expected_model_file_count is not None
            and len(files) != self.expected_model_file_count
        ):
            raise RealAudioJobError(
                code="TTS_MODEL_HASH_MISMATCH",
                stage="AUDIO_GENERATION",
                summary=(
                    f"模型正式文件数不符：要求 {self.expected_model_file_count}，"
                    f"实际 {len(files)}"
                ),
                retryable=False,
            )
        if (
            self.expected_model_total_size_bytes is not None
            and total_size != self.expected_model_total_size_bytes
        ):
            raise RealAudioJobError(
                code="TTS_MODEL_HASH_MISMATCH",
                stage="AUDIO_GENERATION",
                summary=(
                    "模型总大小不符：要求 "
                    f"{self.expected_model_total_size_bytes}，实际 {total_size}"
                ),
                retryable=False,
            )
        critical: dict[str, Any] = {}
        for relative, expectation in self.model_expectations.items():
            candidate = (self.model_path / Path(relative)).resolve()
            try:
                candidate.relative_to(self.model_path)
            except ValueError as exc:
                raise ValueError("模型相对路径越过模型目录") from exc
            if not candidate.is_file():
                raise RealAudioJobError(
                    code="TTS_MODEL_NOT_FOUND",
                    stage="AUDIO_GENERATION",
                    summary=f"关键模型文件缺失：{relative}",
                    retryable=False,
                )
            if candidate.stat().st_size != expectation.size_bytes:
                raise RealAudioJobError(
                    code="TTS_MODEL_HASH_MISMATCH",
                    stage="AUDIO_GENERATION",
                    summary=f"关键模型文件大小不符：{relative}",
                    retryable=False,
                )
            actual_sha = sha256_file(candidate)
            if actual_sha != expectation.sha256.lower():
                raise RealAudioJobError(
                    code="TTS_MODEL_HASH_MISMATCH",
                    stage="AUDIO_GENERATION",
                    summary=f"关键模型文件 SHA256 不符：{relative}",
                    retryable=False,
                )
            metadata = _metadata_path(self.model_path, relative)
            try:
                lines = metadata.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise RealAudioJobError(
                    code="TTS_MODEL_HASH_MISMATCH",
                    stage="AUDIO_GENERATION",
                    summary=f"缺少 Hugging Face revision metadata：{relative}",
                    retryable=False,
                ) from exc
            if len(lines) < 2 or lines[0].strip().lower() != self.model_revision:
                raise RealAudioJobError(
                    code="TTS_MODEL_HASH_MISMATCH",
                    stage="AUDIO_GENERATION",
                    summary=f"模型下载 metadata revision 不符：{relative}",
                    retryable=False,
                )
            if lines[1].strip().lower() != expectation.sha256.lower():
                raise RealAudioJobError(
                    code="TTS_MODEL_HASH_MISMATCH",
                    stage="AUDIO_GENERATION",
                    summary=f"模型下载 metadata SHA256 不符：{relative}",
                    retryable=False,
                )
            critical[relative] = {
                "size_bytes": candidate.stat().st_size,
                "sha256": actual_sha,
                "metadata_path": str(metadata),
                "metadata_revision": lines[0].strip(),
            }
        report = {
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "model_license": self.model_license,
            "model_path": str(self.model_path),
            "file_count": len(files),
            "total_size_bytes": total_size,
            "critical_files": critical,
            "validated_at": _utc_now(),
        }
        atomic_json(report_path, report)
        return report

    def _asset_from_trace(
        self,
        *,
        request: AudioGenerationRequest,
        trace_path: Path,
    ) -> GeneratedAudioAsset:
        trace = read_json(trace_path)
        if trace is None:
            raise ValueError(f"TTS runner 追溯 JSON 无效：{trace_path}")
        expected_seed = request.options.base_seed + request.shot.index
        required_equal = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "project_id": request.project_id,
            "job_id": request.job_id,
            "source_script_job_id": request.source_script_job_id,
            "source_image_job_id": request.source_image_job_id,
            "shot_id": request.shot.id,
            "shot_index": request.shot.index,
            "text": request.shot.narration,
            "text_sha256": _text_sha256(request.shot.narration),
            "speaker": request.options.speaker,
            "language": request.options.language,
            "seed": expected_seed,
        }
        for key, expected in required_equal.items():
            if trace.get(key) != expected:
                raise ValueError(f"TTS runner 追溯字段 {key} 与请求不一致")
        audio_path = Path(str(trace.get("audio_path", ""))).resolve()
        try:
            audio_path.relative_to(request.output_dir)
        except ValueError as exc:
            raise ValueError("TTS runner 输出路径越过当前 Job 音频目录") from exc
        validation = inspect_pcm16_wav(audio_path)
        if trace.get("audio_sha256") != validation["sha256"]:
            raise ValueError("TTS runner 音频 SHA256 与实算值不一致")
        for key in ("sample_rate", "channels", "sample_width_bytes"):
            if trace.get(key) != validation[key]:
                raise ValueError(f"TTS runner 音频字段 {key} 与文件不一致")
        duration = float(validation["duration_seconds"])
        generation_seconds = float(trace.get("generation_seconds"))
        if not 0 < generation_seconds < 86_400:
            raise ValueError("TTS generation_seconds 无效")
        return GeneratedAudioAsset(
            provider_id=self.provider_id,
            model_id=self.model_id,
            model_revision=self.model_revision,
            model_sha256=self.model_sha256,
            shot_id=request.shot.id,
            audio_path=audio_path,
            trace_path=trace_path.resolve(),
            text=request.shot.narration,
            speaker=request.options.speaker,
            language=request.options.language,
            seed=expected_seed,
            sample_rate=int(validation["sample_rate"]),
            channels=int(validation["channels"]),
            sample_width_bytes=int(validation["sample_width_bytes"]),
            duration_seconds=duration,
            generation_seconds=generation_seconds,
            real_time_factor=round(generation_seconds / duration, 6),
            peak_amplitude=float(validation["peak_amplitude"]),
            rms=float(validation["rms"]),
            audio_sha256=str(validation["sha256"]),
            warnings=tuple(
                value for value in trace.get("warnings", []) if isinstance(value, str)
            ),
        )

    def _generate_requests(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...],
        progress_callback: ProgressCallback | None,
        enforce_job_shot_count: bool,
    ) -> tuple[GeneratedAudioAsset, ...]:
        """保证 Job 级成功或失败报告均原子落盘。"""

        self.last_run_report = None
        try:
            return self._generate_requests_impl(
                requests=requests,
                reusable_assets=reusable_assets,
                progress_callback=progress_callback,
                enforce_job_shot_count=enforce_job_shot_count,
            )
        except RealAudioJobError as exc:
            self._write_failure_report(
                requests=requests,
                reusable_assets=reusable_assets,
                error=exc,
            )
            raise
        except Exception as exc:
            error = RealAudioJobError(
                code=(
                    exc.code
                    if isinstance(exc, AudioValidationError)
                    else "TTS_GENERATION_FAILED"
                ),
                stage="AUDIO_GENERATION",
                summary=f"真实旁白生成失败：{str(exc)[:500]}",
                total_audio_count=len(requests) or None,
            )
            self._write_failure_report(
                requests=requests,
                reusable_assets=reusable_assets,
                error=error,
            )
            raise error from exc

    def _write_failure_report(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...],
        error: RealAudioJobError,
    ) -> None:
        if not requests:
            return
        job_dir = requests[0].output_dir.parent.resolve()
        audio_dir = requests[0].output_dir
        reusable_by_shot = {item.shot_id: item for item in reusable_assets}
        reusable_wavs: list[dict[str, Any]] = []
        for request in requests:
            asset: GeneratedAudioAsset | None = None
            trace_path = audio_dir / f"shot-{request.shot.index:02d}.result.json"
            if trace_path.is_file():
                try:
                    asset = self._asset_from_trace(
                        request=request,
                        trace_path=trace_path,
                    )
                except (OSError, TypeError, ValueError):
                    asset = None
            if asset is None:
                candidate = reusable_by_shot.get(request.shot.id)
                if candidate is not None:
                    asset, _ = validate_reusable_audio_asset(
                        asset=candidate,
                        request=request,
                        provider_id=self.provider_id,
                        model_id=self.model_id,
                        model_revision=self.model_revision,
                        model_sha256=self.model_sha256,
                    )
            if asset is not None:
                reusable_wavs.append(asset.as_dict())

        previous = self.last_run_report if isinstance(self.last_run_report, dict) else {}
        cleanup = previous.get("cleanup")
        owned_child_exited = not (
            isinstance(cleanup, dict) and cleanup.get("owned_child_exited") is False
        )
        progress_path = job_dir / "audio-runner-progress.json"
        summary_path = job_dir / "audio-runner-summary.json"
        stdout_path = job_dir / "tts.stdout.log"
        stderr_path = job_dir / "tts.stderr.log"
        runner_summary = read_json(summary_path) or {}
        report = {
            **previous,
            "status": "FAILED",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "generation_error": error.generation_error,
            "completed_audio_count": len(reusable_wavs),
            "total_audio_count": len(requests),
            "reusable_wavs": reusable_wavs,
            "model_load_count": runner_summary.get(
                "model_load_count", previous.get("model_load_count", 0)
            ),
            "sequential_generation": True,
            "max_audio_concurrency": 1,
            "mock_fallback": False,
            "cloud_api_used": False,
            "voice_cloning_used": False,
            "runner_summary": runner_summary,
            "gpu_memory_observed": {
                "baseline_bytes": runner_summary.get(
                    "gpu_memory_baseline_bytes"
                ),
                "peak_allocated_bytes": runner_summary.get(
                    "gpu_peak_allocated_bytes"
                ),
                "peak_reserved_bytes": runner_summary.get(
                    "gpu_peak_reserved_bytes"
                ),
                "after_cleanup_bytes": runner_summary.get(
                    "gpu_memory_after_cleanup_bytes"
                ),
                "method": (
                    "PyTorch child-process CUDA allocator; not GPU-wide WDDM memory"
                ),
            },
            "child_started": bool(
                previous.get(
                    "child_started", summary_path.exists() or progress_path.exists()
                )
            ),
            "child_process_exited": owned_child_exited,
            "log_paths": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "progress": str(progress_path),
                "summary": str(summary_path),
            },
            "cleanup": (
                cleanup
                if isinstance(cleanup, dict)
                else {"owned_child_exited": owned_child_exited}
            ),
        }
        self.last_run_report = report
        atomic_json(job_dir / "audio_generation_report.json", report)

    def _generate_requests_impl(
        self,
        *,
        requests: tuple[AudioGenerationRequest, ...],
        reusable_assets: tuple[GeneratedAudioAsset, ...],
        progress_callback: ProgressCallback | None,
        enforce_job_shot_count: bool,
    ) -> tuple[GeneratedAudioAsset, ...]:
        self._validate_requests(
            requests, enforce_job_shot_count=enforce_job_shot_count
        )
        first = requests[0]
        job_dir = first.output_dir.parent.resolve()
        first.output_dir.mkdir(parents=True, exist_ok=True)
        for request in requests:
            shot_prefix = first.output_dir / f"shot-{request.shot.index:02d}"
            text_path = shot_prefix.with_suffix(".text.txt")
            text_path.write_text(
                request.shot.narration,
                encoding="utf-8",
            )
            atomic_json(
                shot_prefix.with_suffix(".request.json"),
                {
                    "request_version": "m5.audio-shot-request.v1",
                    "provider_id": self.provider_id,
                    "model_id": self.model_id,
                    "model_revision": self.model_revision,
                    "model_sha256": self.model_sha256,
                    "project_id": request.project_id,
                    "job_id": request.job_id,
                    "source_script_job_id": request.source_script_job_id,
                    "source_image_job_id": request.source_image_job_id,
                    "shot_id": request.shot.id,
                    "shot_index": request.shot.index,
                    "text": request.shot.narration,
                    "text_sha256": _text_sha256(request.shot.narration),
                    "text_path": str(text_path),
                    "speaker": request.options.speaker,
                    "language": request.options.language,
                    "seed": request.options.base_seed + request.shot.index,
                    "planned_duration_seconds": request.shot.duration_seconds,
                },
            )
        reusable_by_shot = {item.shot_id: item for item in reusable_assets}
        completed: dict[str, GeneratedAudioAsset] = {}
        reuse_rejections: dict[str, str] = {}
        for request in requests:
            candidate = reusable_by_shot.get(request.shot.id)
            if candidate is None:
                continue
            reused, reason = validate_reusable_audio_asset(
                asset=candidate,
                request=request,
                provider_id=self.provider_id,
                model_id=self.model_id,
                model_revision=self.model_revision,
                model_sha256=self.model_sha256,
            )
            if reused is not None:
                completed[request.shot.id] = reused
            elif reason:
                reuse_rejections[request.shot.id] = reason
        missing = tuple(item for item in requests if item.shot.id not in completed)
        emitted_count = 0

        def emit_contiguous() -> None:
            nonlocal emitted_count
            while emitted_count < len(requests):
                request = requests[emitted_count]
                asset = completed.get(request.shot.id)
                if asset is None:
                    break
                emitted_count += 1
                if progress_callback:
                    progress_callback(emitted_count, len(requests), asset)

        emit_contiguous()
        if not missing:
            result = tuple(completed[item.shot.id] for item in requests)
            self.last_run_report = {
                "status": "SUCCEEDED",
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "model_sha256": self.model_sha256,
                "child_started": False,
                "model_load_count": 0,
                "generated_count": 0,
                "reused_count": len(result),
                "sequential_generation": True,
                "max_audio_concurrency": 1,
                "child_process_exited": True,
                "gpu_memory_observed": {
                    "skipped": "all_audio_reused",
                    "method": "no TTS child process was started",
                },
                "reuse_rejections": reuse_rejections,
                "model_validation": {"skipped": "all_audio_reused"},
                "model_license": self.model_license,
                "package_version": self.package_version,
                "audio_assets": [asset.as_dict() for asset in result],
            }
            atomic_json(job_dir / "audio_generation_report.json", self.last_run_report)
            return result

        model_report = self._validate_model(job_dir / "audio-model-files.json")
        handoff = self.handoff_check()
        if handoff.get("conflict"):
            raise RealAudioJobError(
                code="GPU_HANDOFF_REQUIRED",
                stage="GPU_HANDOFF_REQUIRED",
                summary="真实旁白生成前检测到模型服务或高显存占用，需要先完成 GPU 交接。",
                total_audio_count=len(requests),
                requires_qwen_shutdown=bool(
                    handoff.get("llama_port_listening")
                    or handoff.get("llama_process_detected")
                ),
                requires_comfyui_shutdown=bool(
                    handoff.get("comfyui_port_listening")
                    or handoff.get("comfyui_process_detected")
                ),
                suggestions=[
                    "确认 8081 与 8188 已释放后手动重试。",
                    f"只读检测到的模型进程：{handoff.get('detected_process_names', [])}",
                    (
                        "关闭其他高显存程序后重试。"
                        if handoff.get("gpu_memory_conflict")
                        else "确认没有其他高显存推理进程。"
                    ),
                    "平台不会终止用户启动的外部模型进程。",
                ],
            )

        trace_by_shot = {
            item.shot.id: first.output_dir / f"shot-{item.shot.index:02d}.result.json"
            for item in missing
        }
        runner_request = {
            "protocol_version": "m5b.qwen3-tts-job.v1",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "model_license": self.model_license,
            "package_version": self.package_version,
            "model_path": str(self.model_path),
            "output_dir": str(first.output_dir),
            "project_id": first.project_id,
            "job_id": first.job_id,
            "source_script_job_id": first.source_script_job_id,
            "source_image_job_id": first.source_image_job_id,
            "speaker": first.options.speaker,
            "language": first.options.language,
            "attention_implementation": "sdpa",
            "dtype": "bfloat16",
            "device_map": "cuda:0",
            "local_files_only": True,
            "voice_cloning_used": False,
            "cloud_api_used": False,
            "shots": [
                {
                    "shot_id": item.shot.id,
                    "shot_index": item.shot.index,
                    "text": item.shot.narration,
                    "text_sha256": _text_sha256(item.shot.narration),
                    "planned_duration_seconds": item.shot.duration_seconds,
                    "seed": item.options.base_seed + item.shot.index,
                    "audio_path": str(
                        first.output_dir / f"shot-{item.shot.index:02d}.wav"
                    ),
                    "trace_path": str(trace_by_shot[item.shot.id]),
                }
                for item in missing
            ],
        }
        request_path = job_dir / "audio-generation-request.json"
        progress_path = job_dir / "audio-runner-progress.json"
        summary_path = job_dir / "audio-runner-summary.json"
        stdout_path = job_dir / "tts.stdout.log"
        stderr_path = job_dir / "tts.stderr.log"
        atomic_json(request_path, runner_request)
        command = [
            str(self.tts_python),
            "-u",
            str(self.runner_path),
            "--request",
            str(request_path),
            "--progress",
            str(progress_path),
            "--summary",
            str(summary_path),
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        process: subprocess.Popen[Any] | None = None
        termination: dict[str, Any] = {}
        started = time.monotonic()
        last_progress_marker: tuple[str, Any, Any] = ("PROCESS_START", None, None)
        stage_started = started
        consumed: set[str] = set()
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=self.runner_path.parent,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        shell=False,
                        creationflags=creationflags,
                    )
                    self.last_run_report = {
                        "status": "RUNNING",
                        "provider_id": self.provider_id,
                        "child_started": True,
                        "child_pid": process.pid,
                    }
                except OSError as exc:
                    raise RealAudioJobError(
                        code="TTS_PROCESS_START_FAILED",
                        stage="AUDIO_GENERATION",
                        summary=f"无法启动独立 Qwen3-TTS 子进程：{exc}",
                        completed_audio_count=len(completed),
                        total_audio_count=len(requests),
                        retryable=False,
                        log_paths={
                            "stdout": str(stdout_path),
                            "stderr": str(stderr_path),
                        },
                    ) from exc
                while process.poll() is None:
                    progress = read_json(progress_path) or {}
                    stage = str(progress.get("stage") or "PROCESS_START")
                    progress_marker = (
                        stage,
                        progress.get("shot_id"),
                        progress.get("shot_index"),
                    )
                    if progress_marker != last_progress_marker:
                        last_progress_marker = progress_marker
                        stage_started = time.monotonic()
                    now = time.monotonic()
                    if now - started > first.options.job_timeout_seconds:
                        raise RealAudioJobError(
                            code="TTS_GENERATION_TIMEOUT",
                            stage="AUDIO_GENERATION",
                            summary="真实旁白 Job 超过总超时。",
                            failed_shot_id=progress.get("shot_id"),
                            failed_shot_index=progress.get("shot_index"),
                            completed_audio_count=len(completed),
                            total_audio_count=len(requests),
                        )
                    if stage in {"PROCESS_START", "IMPORT_RUNTIME", "MODEL_LOAD"} and (
                        now - stage_started > first.options.model_load_timeout_seconds
                    ):
                        raise RealAudioJobError(
                            code="TTS_MODEL_LOAD_TIMEOUT",
                            stage="AUDIO_GENERATION",
                            summary="Qwen3-TTS 模型加载超时。",
                            completed_audio_count=len(completed),
                            total_audio_count=len(requests),
                        )
                    if stage == "AUDIO_GENERATION" and (
                        now - stage_started > first.options.generation_timeout_seconds
                    ):
                        raise RealAudioJobError(
                            code="TTS_GENERATION_TIMEOUT",
                            stage="AUDIO_GENERATION",
                            summary="单镜头真实旁白生成超时。",
                            failed_shot_id=progress.get("shot_id"),
                            failed_shot_index=progress.get("shot_index"),
                            completed_audio_count=len(completed),
                            total_audio_count=len(requests),
                        )
                    for request in missing:
                        if request.shot.id in consumed:
                            continue
                        trace_path = trace_by_shot[request.shot.id]
                        if trace_path.is_file():
                            completed[request.shot.id] = self._asset_from_trace(
                                request=request, trace_path=trace_path
                            )
                            consumed.add(request.shot.id)
                            emit_contiguous()
                    time.sleep(0.25)
                returncode = process.returncode
            for request in missing:
                if request.shot.id not in consumed:
                    trace_path = trace_by_shot[request.shot.id]
                    if trace_path.is_file():
                        completed[request.shot.id] = self._asset_from_trace(
                            request=request, trace_path=trace_path
                        )
                        consumed.add(request.shot.id)
                        emit_contiguous()
            summary = read_json(summary_path) or {}
            if returncode != 0 or summary.get("status") != "SUCCEEDED":
                error_text = str(summary.get("error") or "请查看 TTS stderr 日志")
                error_code = str(
                    summary.get("error_code") or "TTS_GENERATION_FAILED"
                )
                error_stage = str(summary.get("error_stage") or "AUDIO_GENERATION")
                raise RealAudioJobError(
                    code=error_code,
                    stage=error_stage,
                    summary=f"Qwen3-TTS 子进程失败：{error_text[:500]}",
                    failed_shot_id=summary.get("failed_shot_id"),
                    failed_shot_index=summary.get("failed_shot_index"),
                    completed_audio_count=len(completed),
                    total_audio_count=len(requests),
                    oom=bool(summary.get("oom")),
                    log_paths={
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "summary": str(summary_path),
                    },
                )
            if summary.get("model_load_count") != 1:
                raise RealAudioJobError(
                    code="TTS_GENERATION_FAILED",
                    stage="AUDIO_GENERATION",
                    summary="TTS runner 未证明模型只加载一次。",
                    completed_audio_count=len(completed),
                    total_audio_count=len(requests),
                    retryable=False,
                )
            if len(completed) != len(requests):
                raise RealAudioJobError(
                    code="AUDIO_OUTPUT_MISSING",
                    stage="AUDIO_GENERATION",
                    summary="AudioProvider 返回的旁白数量不完整。",
                    completed_audio_count=len(completed),
                    total_audio_count=len(requests),
                )
            result = tuple(completed[item.shot.id] for item in requests)
            self.last_run_report = {
                "status": "SUCCEEDED",
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "model_sha256": self.model_sha256,
                "model_license": self.model_license,
                "package_version": self.package_version,
                "child_started": True,
                "child_pid": process.pid,
                "child_returncode": process.returncode,
                "model_load_count": 1,
                "generated_count": len(missing),
                "reused_count": len(requests) - len(missing),
                "sequential_generation": True,
                "max_audio_concurrency": 1,
                "mock_fallback": False,
                "cloud_api_used": False,
                "voice_cloning_used": False,
                "reuse_rejections": reuse_rejections,
                "model_validation": model_report,
                "runner_summary": summary,
                "audio_assets": [asset.as_dict() for asset in result],
                "gpu_memory_observed": {
                    "baseline_bytes": summary.get("gpu_memory_baseline_bytes"),
                    "peak_allocated_bytes": summary.get(
                        "gpu_peak_allocated_bytes"
                    ),
                    "peak_reserved_bytes": summary.get("gpu_peak_reserved_bytes"),
                    "after_cleanup_bytes": summary.get(
                        "gpu_memory_after_cleanup_bytes"
                    ),
                    "method": (
                        "PyTorch child-process CUDA allocator; not GPU-wide WDDM memory"
                    ),
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "child_process_exited": process.poll() is not None,
                "cleanup": {"owned_child_exited": process.poll() is not None},
                "log_paths": {
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                    "progress": str(progress_path),
                    "summary": str(summary_path),
                },
            }
            atomic_json(job_dir / "audio_generation_report.json", self.last_run_report)
            return result
        except RealAudioJobError:
            if process is not None and process.poll() is None:
                termination = _terminate_owned_process(process)
            if process is not None:
                self.last_run_report = {
                    **(
                        self.last_run_report
                        if isinstance(self.last_run_report, dict)
                        else {}
                    ),
                    "status": "FAILED",
                    "child_started": True,
                    "child_pid": process.pid,
                    "child_returncode": process.poll(),
                    "child_process_exited": process.poll() is not None,
                    "cleanup": {
                        "owned_child_exited": process.poll() is not None,
                        "termination": termination,
                    },
                }
            raise
        except Exception as exc:
            if process is not None and process.poll() is None:
                termination = _terminate_owned_process(process)
            progress = read_json(progress_path) or {}
            message = str(exc)
            oom = "out of memory" in message.lower() or "cuda oom" in message.lower()
            if process is not None:
                self.last_run_report = {
                    **(
                        self.last_run_report
                        if isinstance(self.last_run_report, dict)
                        else {}
                    ),
                    "status": "FAILED",
                    "child_started": True,
                    "child_pid": process.pid,
                    "child_returncode": process.poll(),
                    "child_process_exited": process.poll() is not None,
                    "cleanup": {
                        "owned_child_exited": process.poll() is not None,
                        "termination": termination,
                    },
                }
            raise RealAudioJobError(
                code=(
                    exc.code
                    if isinstance(exc, AudioValidationError)
                    else "TTS_GENERATION_FAILED"
                ),
                stage="AUDIO_GENERATION",
                summary=f"真实旁白生成失败：{message[:500]}",
                failed_shot_id=progress.get("shot_id"),
                failed_shot_index=progress.get("shot_index"),
                completed_audio_count=len(completed),
                total_audio_count=len(requests),
                oom=oom,
                log_paths={
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                    "summary": str(summary_path),
                },
            ) from exc
        finally:
            if process is not None and process.poll() is None:
                termination = _terminate_owned_process(process)
            if process is not None and process.poll() is None:
                # Never report success while the owned runtime is still resident.
                self.last_run_report = {
                    "status": "FAILED",
                    "provider_id": self.provider_id,
                    "cleanup": {
                        "owned_child_exited": False,
                        "termination": termination,
                    },
                }
                atomic_json(job_dir / "audio_generation_report.json", self.last_run_report)


def create_qwen3_tts_provider(settings: Any) -> Qwen3TTSAudioProvider:
    """从 Settings 构造正式 Provider；保持后端进程不导入 TTS 运行时。"""

    model_sha256 = str(settings.qwen_tts_model_sha256).strip().lower()
    tokenizer_sha256 = str(settings.qwen_tts_tokenizer_sha256).strip().lower()
    return Qwen3TTSAudioProvider(
        tts_python=Path(settings.qwen_tts_python),
        runner_path=Path(settings.qwen_tts_runner),
        model_path=Path(settings.qwen_tts_model_path),
        model_id=str(settings.qwen_tts_model_id),
        model_revision=str(settings.qwen_tts_model_revision),
        model_sha256=model_sha256,
        model_license=str(settings.qwen_tts_model_license),
        package_version=str(settings.qwen_tts_package_version),
        model_expectations={
            "model.safetensors": ModelFileExpectation(
                DEFAULT_MODEL_EXPECTATIONS["model.safetensors"].size_bytes,
                model_sha256,
            ),
            "speech_tokenizer/model.safetensors": ModelFileExpectation(
                DEFAULT_MODEL_EXPECTATIONS[
                    "speech_tokenizer/model.safetensors"
                ].size_bytes,
                tokenizer_sha256,
            ),
        },
        handoff_check=lambda: audio_gpu_handoff_status(settings),
    )


def audio_generation_options_from_settings(
    settings: Any,
    *,
    speaker: str | None = None,
) -> AudioGenerationOptions:
    """把 Job 已固化的 Settings 值转换为不可变 Provider 参数。"""

    return AudioGenerationOptions(
        speaker=speaker or str(settings.qwen_tts_default_speaker),
        language=str(settings.qwen_tts_language),
        base_seed=int(settings.qwen_tts_seed),
        model_load_timeout_seconds=float(
            settings.qwen_tts_model_load_timeout_seconds
        ),
        generation_timeout_seconds=float(settings.qwen_tts_shot_timeout_seconds),
        job_timeout_seconds=float(settings.qwen_tts_job_timeout_seconds),
    )


__all__ = [
    "DEFAULT_MODEL_EXPECTATIONS",
    "MODEL_FILE_COUNT",
    "MODEL_ID",
    "MODEL_LICENSE",
    "MODEL_REVISION",
    "MODEL_TOTAL_SIZE_BYTES",
    "ModelFileExpectation",
    "Qwen3TTSAudioProvider",
    "audio_generation_options_from_settings",
    "create_qwen3_tts_provider",
]

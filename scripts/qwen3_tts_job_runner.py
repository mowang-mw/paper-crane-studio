"""一次性 Qwen3-TTS 子进程：一次加载、顺序生成、写出可校验追溯。

本脚本只由正式 AudioProvider 以参数列表启动，不提供 Web 服务，也不下载模型。
"""

from __future__ import annotations

import argparse
from array import array
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import traceback
import wave
from typing import Any


PROTOCOL_VERSION = "m5b.qwen3-tts-job.v1"
MIN_AUDIBLE_PEAK = 1e-3
MIN_AUDIBLE_RMS = 1e-4
MAX_FULL_SCALE_RATIO = 1e-3


class RunnerFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def inspect_pcm16_wav(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise RunnerFailure("AUDIO_OUTPUT_MISSING", "WAV 不存在或为空")
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
        raise RunnerFailure("AUDIO_DECODE_FAILED", f"WAV 无法解码：{exc}") from exc
    if compression != "NONE" or sample_width != 2:
        raise RunnerFailure("AUDIO_DECODE_FAILED", "WAV 必须是未压缩 PCM16")
    if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
        raise RunnerFailure("AUDIO_DECODE_FAILED", "WAV 格式参数无效")
    expected_bytes = frame_count * channels * sample_width
    if len(raw) != expected_bytes or trailing:
        raise RunnerFailure("AUDIO_DECODE_FAILED", "WAV PCM 数据不完整")
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    peak_raw = max(abs(int(value)) for value in samples)
    sum_squares = sum(int(value) * int(value) for value in samples)
    peak = peak_raw / 32768.0
    rms = math.sqrt(sum_squares / len(samples)) / 32768.0
    full_scale_count = sum(value in (-32768, 32767) for value in samples)
    full_scale_ratio = full_scale_count / len(samples)
    if peak <= MIN_AUDIBLE_PEAK or rms <= MIN_AUDIBLE_RMS:
        raise RunnerFailure("AUDIO_SILENT", "WAV 近似静音")
    if full_scale_ratio > MAX_FULL_SCALE_RATIO:
        raise RunnerFailure("AUDIO_DECODE_FAILED", "WAV 存在明显数字削波")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frame_count": frame_count,
        "duration_seconds": round(frame_count / sample_rate, 6),
        "peak_amplitude": round(peak, 9),
        "rms": round(rms, 9),
        "full_scale_ratio": round(full_scale_ratio, 9),
        "file_size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "full_decode_ok": True,
        "all_samples_finite": True,
    }


def require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"请求缺少非空字符串字段：{key}")
    return value


def require_contained(path_value: str, output_dir: Path, label: str) -> Path:
    candidate = Path(path_value).resolve()
    try:
        candidate.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError(f"{label} 越过 output_dir") from exc
    return candidate


def optional_text(payload: dict[str, Any], key: str) -> str | None:
    """Read optional provenance without making audio-only requests image-bound."""
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Optional field must be a string or null: {key}")
    return value


def load_request(path: Path) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("请求根节点必须是对象")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("请求 protocol_version 不受支持")
    if payload.get("local_files_only") is not True:
        raise ValueError("正式 TTS runner 只允许 local_files_only=true")
    if payload.get("cloud_api_used") is not False:
        raise ValueError("正式 TTS runner 禁止云 API")
    if payload.get("voice_cloning_used") is not False:
        raise ValueError("正式 TTS runner 禁止声音克隆")
    if payload.get("attention_implementation") != "sdpa":
        raise ValueError("Windows M5-B 固定使用 PyTorch SDPA")
    if payload.get("dtype") != "bfloat16" or payload.get("device_map") != "cuda:0":
        raise ValueError("M5-B 固定使用 CUDA bfloat16")
    output_dir = Path(require_text(payload, "output_dir")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_shots = payload.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise ValueError("请求至少包含一个待生成镜头")
    shots: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_index = 0
    for position, original in enumerate(raw_shots):
        if not isinstance(original, dict):
            raise ValueError(f"shots[{position}] 必须是对象")
        item = dict(original)
        shot_id = require_text(item, "shot_id")
        text = require_text(item, "text")
        index = item.get("shot_index")
        seed = item.get("seed")
        if type(index) is not int or index <= previous_index:
            raise ValueError("待生成镜头必须按严格递增 shot_index 排序")
        if type(seed) is not int or not 0 <= seed < 2**63:
            raise ValueError(f"镜头 {shot_id} 的 seed 无效")
        if shot_id in seen_ids:
            raise ValueError(f"shot_id 重复：{shot_id}")
        if item.get("text_sha256") != text_sha256(text):
            raise ValueError(f"镜头 {shot_id} 的正文 SHA256 不匹配")
        item["audio_path"] = str(
            require_contained(require_text(item, "audio_path"), output_dir, "audio_path")
        )
        item["trace_path"] = str(
            require_contained(require_text(item, "trace_path"), output_dir, "trace_path")
        )
        seen_ids.add(shot_id)
        previous_index = index
        shots.append(item)
    return payload, output_dir, shots


def set_seed(seed: int, *, np: Any, torch: Any) -> None:
    random.seed(seed)
    np.random.seed(seed % (2**32))
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = args.request.resolve()
    progress_path = args.progress.resolve()
    summary_path = args.summary.resolve()
    started = time.monotonic()
    summary: dict[str, Any] = {
        "status": "FAILED",
        "protocol_version": PROTOCOL_VERSION,
        "model_load_count": 0,
        "completed_audio_count": 0,
        "oom": False,
        "cloud_api_used": False,
        "voice_cloning_used": False,
        "cpu_offload_used": False,
        "sequential_generation": True,
        "max_audio_concurrency": 1,
    }
    model: Any | None = None
    torch: Any | None = None
    current_shot: dict[str, Any] | None = None
    stage = "REQUEST_VALIDATION"
    try:
        atomic_json(progress_path, {"stage": stage, "completed_audio_count": 0})
        print("[tts-runner] stage=REQUEST_VALIDATION", flush=True)
        request, output_dir, shots = load_request(request_path)
        project_id = require_text(request, "project_id")
        job_id = require_text(request, "job_id")
        source_script_job_id = require_text(request, "source_script_job_id")
        source_image_job_id = optional_text(request, "source_image_job_id")
        summary.update(
            {
                "project_id": project_id,
                "job_id": job_id,
                "source_script_job_id": source_script_job_id,
                "source_image_job_id": source_image_job_id,
                "provider_id": require_text(request, "provider_id"),
                "model_id": require_text(request, "model_id"),
                "model_revision": require_text(request, "model_revision"),
                "model_sha256": require_text(request, "model_sha256"),
                "model_license": require_text(request, "model_license"),
                "speaker": require_text(request, "speaker"),
                "language": require_text(request, "language"),
                "output_dir": str(output_dir),
                "total_audio_count": len(shots),
            }
        )
        if summary["language"] != "Chinese":
            raise ValueError("M5-B language 必须为 Chinese")
        if summary["speaker"] not in {"Serena", "Vivian"}:
            raise ValueError("M5-B speaker 只允许 Serena 或 Vivian")

        model_path = Path(require_text(request, "model_path")).resolve()
        if not model_path.is_dir():
            raise RunnerFailure("TTS_MODEL_NOT_FOUND", f"本地模型目录不存在：{model_path}")
        metadata_path = (
            model_path
            / ".cache"
            / "huggingface"
            / "download"
            / "model.safetensors.metadata"
        )
        try:
            metadata_lines = metadata_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RunnerFailure(
                "TTS_MODEL_NOT_FOUND", "model.safetensors revision metadata 缺失"
            ) from exc
        if len(metadata_lines) < 2:
            raise RunnerFailure(
                "TTS_MODEL_HASH_MISMATCH",
                "model.safetensors revision metadata 不完整",
            )
        if metadata_lines[0].strip().lower() != summary["model_revision"].lower():
            raise RunnerFailure(
                "TTS_MODEL_HASH_MISMATCH", "本地模型 revision 与请求不一致"
            )
        if metadata_lines[1].strip().lower() != summary["model_sha256"].lower():
            raise RunnerFailure(
                "TTS_MODEL_HASH_MISMATCH",
                "本地模型 SHA256 metadata 与请求不一致",
            )

        stage = "IMPORT_RUNTIME"
        atomic_json(progress_path, {"stage": stage, "completed_audio_count": 0})
        print("[tts-runner] stage=IMPORT_RUNTIME", flush=True)
        import numpy as np
        import soundfile as sf
        import torch as imported_torch
        from qwen_tts import Qwen3TTSModel

        torch = imported_torch
        package_version = importlib.metadata.version("qwen-tts")
        if package_version != require_text(request, "package_version"):
            raise RuntimeError(
                f"qwen-tts 版本不符：要求 {request['package_version']}，实际 {package_version}"
            )
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch CUDA 不可用，拒绝静默转入 CPU")
        summary["environment"] = {
            "python_version": sys.version.split()[0],
            "qwen_tts_version": package_version,
            "torch_version": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "gpu_name": torch.cuda.get_device_name(0),
            "numpy_version": np.__version__,
            "soundfile_version": sf.__version__,
        }
        torch.cuda.reset_peak_memory_stats()
        summary["gpu_memory_baseline_bytes"] = int(torch.cuda.memory_allocated())

        stage = "MODEL_LOAD"
        atomic_json(progress_path, {"stage": stage, "completed_audio_count": 0})
        print("[tts-runner] stage=MODEL_LOAD", flush=True)
        model_started = time.monotonic()
        model = Qwen3TTSModel.from_pretrained(
            str(model_path),
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            local_files_only=True,
        )
        summary["model_load_count"] = 1
        summary["model_load_seconds"] = round(time.monotonic() - model_started, 6)
        print(
            "[tts-runner] model_load_count=1 "
            f"seconds={summary['model_load_seconds']}",
            flush=True,
        )
        supported_speakers = {value.casefold() for value in model.get_supported_speakers()}
        supported_languages = {value.casefold() for value in model.get_supported_languages()}
        if summary["speaker"].casefold() not in supported_speakers:
            raise RuntimeError(f"模型不支持预置音色：{summary['speaker']}")
        if summary["language"].casefold() not in supported_languages:
            raise RuntimeError(f"模型不支持语言：{summary['language']}")

        results: list[dict[str, Any]] = []
        for current_shot in shots:
            stage = "AUDIO_GENERATION"
            progress = {
                "stage": stage,
                "shot_id": current_shot["shot_id"],
                "shot_index": current_shot["shot_index"],
                "completed_audio_count": len(results),
                "total_audio_count": len(shots),
            }
            atomic_json(progress_path, progress)
            print(
                "[tts-runner] stage=AUDIO_GENERATION "
                f"shot_id={current_shot['shot_id']} "
                f"shot_index={current_shot['shot_index']}",
                flush=True,
            )
            set_seed(int(current_shot["seed"]), np=np, torch=torch)
            torch.cuda.synchronize()
            generation_started = time.monotonic()
            wavs, sample_rate = model.generate_custom_voice(
                text=current_shot["text"],
                language=summary["language"],
                speaker=summary["speaker"],
                instruct=None,
            )
            torch.cuda.synchronize()
            generation_seconds = time.monotonic() - generation_started
            if not isinstance(sample_rate, int) or sample_rate <= 0:
                raise RuntimeError("模型返回的采样率无效")
            if not isinstance(wavs, (list, tuple)) or len(wavs) != 1:
                raise RuntimeError("模型必须为单条文本返回一段音频")
            waveform = wavs[0]
            if hasattr(waveform, "detach"):
                waveform = waveform.detach().float().cpu().numpy()
            waveform = np.asarray(waveform, dtype=np.float32).squeeze()
            if waveform.ndim != 1 or waveform.size == 0:
                raise RuntimeError("模型返回的波形维度无效")
            if not bool(np.isfinite(waveform).all()):
                raise RuntimeError("模型返回的波形包含 NaN 或 Inf")
            if float(np.max(np.abs(waveform))) > 1.00001:
                raise RuntimeError("模型返回的浮点波形越过 [-1, 1]")

            audio_path = Path(current_shot["audio_path"])
            trace_path = Path(current_shot["trace_path"])
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_audio = audio_path.with_name(audio_path.name + ".part.wav")
            if temporary_audio.exists():
                temporary_audio.unlink()
            sf.write(
                str(temporary_audio),
                waveform,
                sample_rate,
                format="WAV",
                subtype="PCM_16",
            )
            validation = inspect_pcm16_wav(temporary_audio)
            os.replace(temporary_audio, audio_path)
            validation = inspect_pcm16_wav(audio_path)
            duration = float(validation["duration_seconds"])
            trace = {
                "trace_version": "m5.audio-shot.v1",
                "provider_id": summary["provider_id"],
                "model_id": summary["model_id"],
                "model_revision": summary["model_revision"],
                "model_sha256": summary["model_sha256"],
                "model_license": summary["model_license"],
                "project_id": project_id,
                "job_id": job_id,
                "source_script_job_id": source_script_job_id,
                "source_image_job_id": source_image_job_id,
                "shot_id": current_shot["shot_id"],
                "shot_index": current_shot["shot_index"],
                "text": current_shot["text"],
                "text_sha256": current_shot["text_sha256"],
                "text_source": "ScriptV1.shot.narration",
                "speaker": summary["speaker"],
                "language": summary["language"],
                "seed": current_shot["seed"],
                "audio_path": str(audio_path),
                "audio_sha256": validation["sha256"],
                "sample_rate": validation["sample_rate"],
                "channels": validation["channels"],
                "sample_width_bytes": validation["sample_width_bytes"],
                "duration_seconds": duration,
                "generation_seconds": round(generation_seconds, 6),
                "real_time_factor": round(generation_seconds / duration, 6),
                "peak_amplitude": validation["peak_amplitude"],
                "rms": validation["rms"],
                "full_scale_ratio": validation["full_scale_ratio"],
                "full_decode_ok": True,
                "all_samples_finite": True,
                "cloud_api_used": False,
                "voice_cloning_used": False,
                "warnings": ["发音和情感质量仍需人工试听确认。"],
            }
            atomic_json(trace_path, trace)
            results.append(trace)
            atomic_json(
                progress_path,
                {
                    **progress,
                    "stage": "AUDIO_WRITTEN",
                    "completed_audio_count": len(results),
                    "trace_path": str(trace_path),
                },
            )
            del waveform, wavs

        summary.update(
            {
                "status": "SUCCEEDED",
                "results": results,
                "completed_audio_count": len(results),
                "gpu_peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
                "gpu_peak_reserved_bytes": int(torch.cuda.max_memory_reserved()),
            }
        )
        return_code = 0
    except Exception as exc:  # noqa: BLE001 - 子进程边界必须固化所有失败
        message = str(exc)
        oom = "out of memory" in message.casefold() or "cuda oom" in message.casefold()
        if isinstance(exc, RunnerFailure):
            error_code = exc.code
        elif isinstance(exc, ModuleNotFoundError):
            error_code = "TTS_ENV_NOT_FOUND"
        else:
            error_code = "TTS_GENERATION_FAILED"
        summary.update(
            {
                "status": "FAILED",
                "error_code": error_code,
                "error_stage": stage,
                "error": message[:1000],
                "oom": oom,
                "failed_shot_id": (
                    current_shot.get("shot_id") if current_shot is not None else None
                ),
                "failed_shot_index": (
                    current_shot.get("shot_index") if current_shot is not None else None
                ),
                "traceback": traceback.format_exc(limit=20),
            }
        )
        print(
            f"[tts-runner] status=FAILED code={error_code} stage={stage}: "
            f"{message[:500]}",
            file=sys.stderr,
            flush=True,
        )
        return_code = 1
    finally:
        stage_before_cleanup = stage
        try:
            if model is not None:
                del model
            gc.collect()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                summary["gpu_memory_after_cleanup_bytes"] = int(
                    torch.cuda.memory_allocated()
                )
        except Exception as cleanup_exc:  # noqa: BLE001
            summary["cleanup_error"] = str(cleanup_exc)[:500]
        summary["elapsed_seconds"] = round(time.monotonic() - started, 6)
        summary["cleanup_completed"] = True
        atomic_json(summary_path, summary)
        atomic_json(
            progress_path,
            {
                "stage": "FINISHED" if summary["status"] == "SUCCEEDED" else "FAILED",
                "failed_stage": (
                    stage_before_cleanup if summary["status"] == "FAILED" else None
                ),
                "completed_audio_count": summary.get("completed_audio_count", 0),
                "total_audio_count": summary.get("total_audio_count"),
            },
        )
        print(
            f"[tts-runner] status={summary['status']} cleanup_completed=true",
            flush=True,
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())

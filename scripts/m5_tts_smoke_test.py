"""Bounded local Qwen3-TTS smoke test for two built-in Chinese voices.

The public entry point is a standard-library supervisor.  It launches one
short-lived child with the dedicated Python 3.12 environment, enforces stage
and total timeouts, samples GPU-wide memory, and owns cleanup.  The child loads
the fixed local CustomVoice checkpoint exactly once and generates Serena then
Vivian without a web service, cloud API, reference voice, or voice cloning.
"""

from __future__ import annotations

import argparse
import array
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TTS_PYTHON = PROJECT_ROOT / ".venv-qwen3-tts" / "python.exe"
DEFAULT_MODEL_DIR = (
    PROJECT_ROOT / "models" / "audio" / "Qwen3-TTS-12Hz-0.6B-CustomVoice"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "generated" / "m5" / "tts-smoke"

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_SOURCE = (
    "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
)
MODEL_LICENSE = "Apache-2.0"
PACKAGE_NAME = "qwen-tts"
PACKAGE_VERSION = "0.1.1"
LANGUAGE = "Chinese"
SPEAKERS = ("Serena", "Vivian")
TEXT = (
    "深夜的旧书店里，少女翻开一本会发光的画册。"
    "蓝色鲸鱼从书页中游出，带她穿过寂静的星光。"
)
TORCH_SEED = 20_260_803
ATTENTION_IMPLEMENTATION = "sdpa"

EXPECTED_MODEL_FILES = {
    ".gitattributes": 1_519,
    "README.md": 3_263,
    "config.json": 4_908,
    "generation_config.json": 245,
    "merges.txt": 1_671_839,
    "model.safetensors": 1_811_626_576,
    "preprocessor_config.json": 127,
    "speech_tokenizer/config.json": 2_336,
    "speech_tokenizer/configuration.json": 76,
    "speech_tokenizer/model.safetensors": 682_293_092,
    "speech_tokenizer/preprocessor_config.json": 234,
    "tokenizer_config.json": 7_344,
    "vocab.json": 2_776_833,
}
EXPECTED_MODEL_TOTAL_BYTES = 2_498_388_392
EXPECTED_CRITICAL_SHA256 = {
    "model.safetensors": (
        "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb"
    ),
    "speech_tokenizer/model.safetensors": (
        "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"
    ),
}

MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 60.0
MIN_PEAK_AMPLITUDE = 1e-3
MIN_RMS_AMPLITUDE = 1e-4
MAX_FULL_SCALE_RATIO = 1e-3
GPU_RELEASE_ALLOWANCE_MIB = 512
GPU_RELEASE_TIMEOUT_SECONDS = 60.0


class SmokeTestError(RuntimeError):
    """The bounded M5-A smoke test could not satisfy its contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def run_capture(
    command: list[str],
    *,
    timeout: float = 30.0,
    cwd: Path | None = None,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeTestError(
            f"命令失败（退出码 {completed.returncode}）："
            f"{subprocess.list2cmdline(command)}\n{detail}"
        )
    return completed.stdout.strip()


def gpu_memory_used_mib() -> int | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        output = run_capture(
            [
                executable,
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            timeout=10.0,
        )
        return int(output.splitlines()[0].strip())
    except (SmokeTestError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


class GpuMemoryMonitor:
    """Sample WDDM GPU-wide memory; this is not per-process attribution."""

    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.baseline_mib = gpu_memory_used_mib()
        self.peak_mib = self.baseline_mib
        self.sample_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        if self._started:
            self._stop.set()
            self._thread.join(timeout=10.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            value = gpu_memory_used_mib()
            if value is not None:
                self.sample_count += 1
                if self.peak_mib is None or value > self.peak_mib:
                    self.peak_mib = value
            self._stop.wait(self.interval_seconds)

    def summary(self, post_cleanup_mib: int | None) -> dict[str, Any]:
        additional = None
        if self.baseline_mib is not None and self.peak_mib is not None:
            additional = max(0, self.peak_mib - self.baseline_mib)
        return {
            "baseline_mib": self.baseline_mib,
            "peak_mib": self.peak_mib,
            "additional_mib": additional,
            "post_cleanup_mib": post_cleanup_mib,
            "sample_count": self.sample_count,
            "method": (
                "nvidia-smi GPU-wide memory.used sampled once per second; "
                "Windows WDDM includes display and unrelated GPU processes"
            ),
        }


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def validate_model(model_dir: Path, output_path: Path) -> dict[str, Any]:
    if not model_dir.is_dir():
        raise SmokeTestError(f"模型目录不存在：{model_dir}")

    actual_paths = {
        path.relative_to(model_dir).as_posix(): path
        for path in model_dir.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(model_dir).parts
    }
    missing = sorted(set(EXPECTED_MODEL_FILES) - set(actual_paths))
    unexpected = sorted(set(actual_paths) - set(EXPECTED_MODEL_FILES))
    if missing or unexpected:
        raise SmokeTestError(
            f"模型文件集合不符；缺失={missing}，额外={unexpected}"
        )

    records: list[dict[str, Any]] = []
    total_bytes = 0
    for relative in sorted(EXPECTED_MODEL_FILES):
        path = actual_paths[relative]
        size = path.stat().st_size
        expected_size = EXPECTED_MODEL_FILES[relative]
        if size != expected_size:
            raise SmokeTestError(
                f"模型文件大小不符：{relative}，要求 {expected_size}，实际 {size}"
            )
        digest = sha256_file(path)
        expected_sha = EXPECTED_CRITICAL_SHA256.get(relative)
        if expected_sha and digest != expected_sha:
            raise SmokeTestError(
                f"模型文件 SHA256 不符：{relative}，要求 {expected_sha}，实际 {digest}"
            )
        total_bytes += size
        records.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": digest,
                "official_sha256_expected": expected_sha,
                "critical_hash_verified": bool(expected_sha),
            }
        )
    if total_bytes != EXPECTED_MODEL_TOTAL_BYTES:
        raise SmokeTestError(
            f"模型总大小不符：要求 {EXPECTED_MODEL_TOTAL_BYTES}，实际 {total_bytes}"
        )

    metadata_root = model_dir / ".cache" / "huggingface" / "download"
    metadata_files = sorted(metadata_root.rglob("*.metadata"))
    revision_records: list[dict[str, Any]] = []
    for path in metadata_files:
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            raise SmokeTestError(f"无法读取下载 revision 元数据：{path}: {exc}") from exc
        revision_records.append(
            {
                "path": path.relative_to(model_dir).as_posix(),
                "revision": first_line,
                "matches_expected_revision": first_line == MODEL_REVISION,
            }
        )
    if len(revision_records) != len(EXPECTED_MODEL_FILES):
        raise SmokeTestError(
            f"下载 revision 证据数量不符：要求 {len(EXPECTED_MODEL_FILES)}，"
            f"实际 {len(revision_records)}"
        )
    if any(not item["matches_expected_revision"] for item in revision_records):
        raise SmokeTestError("模型下载 metadata 中存在非固定 revision")

    payload = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_source": MODEL_SOURCE,
        "model_license": MODEL_LICENSE,
        "model_dir": str(model_dir.resolve()),
        "file_count": len(records),
        "total_size_bytes": total_bytes,
        "total_size_gib": round(total_bytes / (1024**3), 6),
        "files": records,
        "revision_validation": {
            "source": "Hugging Face local-dir download metadata first line",
            "metadata_count": len(revision_records),
            "all_match": True,
            "records": revision_records,
        },
        "validated_at": utc_now(),
    }
    write_json(output_path, payload)
    return payload


def inspect_pcm16_wav(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise SmokeTestError(f"WAV 不存在或为空：{path}")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
            raw = handle.readframes(frame_count + 1)
    except (wave.Error, OSError) as exc:
        raise SmokeTestError(f"WAV 无法完整解码：{path}: {exc}") from exc

    if compression != "NONE":
        raise SmokeTestError(f"WAV 不是未压缩 PCM：{path}: {compression}")
    if sample_width != 2:
        raise SmokeTestError(f"WAV 不是 PCM16：{path}: sample_width={sample_width}")
    if channels < 1 or sample_rate <= 0 or frame_count <= 0:
        raise SmokeTestError(f"WAV 头信息无效：{path}")
    expected_bytes = frame_count * channels * sample_width
    if len(raw) != expected_bytes:
        raise SmokeTestError(
            f"WAV PCM 数据长度不符：{path}，要求 {expected_bytes}，实际 {len(raw)}"
        )

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise SmokeTestError(f"WAV 没有采样：{path}")
    peak_integer = max(abs(value) for value in samples)
    square_sum = sum(float(value) * float(value) for value in samples)
    peak = peak_integer / 32768.0
    rms = math.sqrt(square_sum / len(samples)) / 32768.0
    full_scale_count = sum(1 for value in samples if abs(value) >= 32767)
    full_scale_ratio = full_scale_count / len(samples)
    duration = frame_count / sample_rate

    if peak < MIN_PEAK_AMPLITUDE or rms < MIN_RMS_AMPLITUDE:
        raise SmokeTestError(
            f"WAV 疑似全静音：{path}，peak={peak:.8f}，rms={rms:.8f}"
        )
    if full_scale_ratio > MAX_FULL_SCALE_RATIO:
        raise SmokeTestError(
            f"WAV 存在明显数字削波：{path}，full_scale_ratio={full_scale_ratio:.8f}"
        )
    if not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise SmokeTestError(
            f"WAV 时长不合理：{path}，duration={duration:.6f}"
        )

    return {
        "path": str(path.resolve()),
        "file_size_bytes": path.stat().st_size,
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
        "clipping_threshold_ratio": MAX_FULL_SCALE_RATIO,
        "obvious_digital_clipping": False,
        "non_silent": True,
        "full_decode": True,
        "decoded_all_finite": True,
        "sha256": sha256_file(path),
    }


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            output = run_capture(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                timeout=10.0,
            )
        except (SmokeTestError, subprocess.TimeoutExpired):
            return True
        return f'"{pid}"' in output
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_owned_process(process: subprocess.Popen[Any]) -> dict[str, Any]:
    result = {
        "termination_requested": False,
        "forced_tree_kill": False,
        "final_returncode": process.poll(),
    }
    if process.poll() is not None:
        return result
    result["termination_requested"] = True
    try:
        if os.name == "nt" and hasattr(signal_module := __import__("signal"), "CTRL_BREAK_EVENT"):
            process.send_signal(signal_module.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            result["forced_tree_kill"] = True
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=30.0,
                shell=False,
            )
        else:
            process.kill()
        try:
            process.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            pass
    result["final_returncode"] = process.poll()
    return result


def wait_for_gpu_release(
    baseline_mib: int | None,
    timeout_seconds: float = GPU_RELEASE_TIMEOUT_SECONDS,
) -> tuple[int | None, bool]:
    if baseline_mib is None:
        return gpu_memory_used_mib(), False
    deadline = time.monotonic() + timeout_seconds
    last = gpu_memory_used_mib()
    while time.monotonic() < deadline:
        last = gpu_memory_used_mib()
        if last is not None and last <= baseline_mib + GPU_RELEASE_ALLOWANCE_MIB:
            return last, True
        time.sleep(1.0)
    return last, bool(
        last is not None and last <= baseline_mib + GPU_RELEASE_ALLOWANCE_MIB
    )


def child_environment(torch: Any) -> dict[str, Any]:
    driver = None
    gpu_name = None
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            driver = run_capture(
                [
                    executable,
                    "--query-gpu=driver_version",
                    "--format=csv,noheader,nounits",
                ],
                timeout=10.0,
            ).splitlines()[0].strip()
        except (SmokeTestError, subprocess.TimeoutExpired, IndexError):
            driver = None
    return {
        "recorded_at": utc_now(),
        "operating_system": platform.platform(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "qwen_tts_version": importlib.metadata.version(PACKAGE_NAME),
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "gpu_name": gpu_name,
        "nvidia_driver": driver,
        "attention_implementation": ATTENTION_IMPLEMENTATION,
        "flash_attention_installed": False,
        "cpu_offload_requested": False,
        "device_map_requested": "cuda:0",
        "offline_environment": {
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
            "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
            "HF_HUB_DISABLE_TELEMETRY": os.environ.get("HF_HUB_DISABLE_TELEMETRY"),
        },
        "cloud_api_used": False,
        "voice_cloning_used": False,
        "voice_design_used": False,
    }


def write_progress(run_dir: Path, stage: str, **extra: Any) -> None:
    write_json(
        run_dir / "progress.json",
        {"stage": stage, "updated_at": utc_now(), **extra},
    )
    print(f"[m5-tts] {stage}", flush=True)


def child_main(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    model_dir = args.model_dir.resolve()
    model: Any = None
    torch: Any = None
    started = time.perf_counter()
    try:
        write_progress(run_dir, "IMPORT_RUNTIME")
        import numpy as np
        import soundfile as sf
        import torch as torch_module
        from qwen_tts import Qwen3TTSModel

        torch = torch_module
        if importlib.metadata.version(PACKAGE_NAME) != PACKAGE_VERSION:
            raise SmokeTestError(
                f"qwen-tts 版本不符：要求 {PACKAGE_VERSION}，实际 "
                f"{importlib.metadata.version(PACKAGE_NAME)}"
            )
        environment = child_environment(torch)
        write_json(run_dir / "environment.json", environment)
        if not torch.cuda.is_available():
            raise SmokeTestError("独立 TTS 环境中 torch.cuda.is_available() 为 false")

        write_progress(run_dir, "MODEL_LOAD")
        load_started = time.perf_counter()
        model = Qwen3TTSModel.from_pretrained(
            str(model_dir),
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation=ATTENTION_IMPLEMENTATION,
            local_files_only=True,
        )
        model_load_seconds = time.perf_counter() - load_started
        speakers_supported = model.get_supported_speakers()
        languages_supported = model.get_supported_languages()
        for speaker in SPEAKERS:
            if not speakers_supported or speaker.lower() not in speakers_supported:
                raise SmokeTestError(f"模型未声明支持预置音色：{speaker}")
        if not languages_supported or LANGUAGE.lower() not in languages_supported:
            raise SmokeTestError(f"模型未声明支持语言：{LANGUAGE}")

        raw_device_map = getattr(model.model, "hf_device_map", None)
        device_map = None
        if isinstance(raw_device_map, dict):
            device_map = {str(key): str(value) for key, value in raw_device_map.items()}
        cpu_offload_used = bool(
            device_map
            and any(value.lower() in {"cpu", "disk"} for value in device_map.values())
        )

        speaker_results: list[dict[str, Any]] = []
        for speaker in SPEAKERS:
            write_progress(run_dir, f"{speaker.upper()}_GENERATION", speaker=speaker)
            torch.manual_seed(TORCH_SEED)
            torch.cuda.manual_seed_all(TORCH_SEED)
            np.random.seed(TORCH_SEED)
            generation_started = time.perf_counter()
            wavs, sample_rate = model.generate_custom_voice(
                text=TEXT,
                language=LANGUAGE,
                speaker=speaker,
                instruct=None,
            )
            generation_seconds = time.perf_counter() - generation_started
            if len(wavs) != 1:
                raise SmokeTestError(
                    f"{speaker} 返回波形数量不符：要求 1，实际 {len(wavs)}"
                )
            waveform = np.asarray(wavs[0], dtype=np.float32).squeeze()
            if waveform.ndim != 1 or waveform.size == 0:
                raise SmokeTestError(
                    f"{speaker} 返回的波形形状无效：{tuple(waveform.shape)}"
                )
            source_all_finite = bool(np.isfinite(waveform).all())
            if not source_all_finite:
                raise SmokeTestError(f"{speaker} 模型波形包含 NaN 或 Inf")
            source_peak = float(np.max(np.abs(waveform)))
            source_rms = float(np.sqrt(np.mean(np.square(waveform, dtype=np.float64))))
            if source_peak > 1.00001:
                raise SmokeTestError(
                    f"{speaker} 模型波形超出 PCM 归一范围且脚本拒绝静默归一化："
                    f"peak={source_peak:.8f}"
                )

            output_path = run_dir / f"{speaker.lower()}.wav"
            temporary = run_dir / f"{speaker.lower()}.part.wav"
            sf.write(
                str(temporary),
                waveform,
                int(sample_rate),
                format="WAV",
                subtype="PCM_16",
            )
            temporary.replace(output_path)
            wav_validation = inspect_pcm16_wav(output_path)
            duration = float(wav_validation["duration_seconds"])
            result = {
                "speaker": speaker,
                "text": TEXT,
                "text_sha256": text_sha256(TEXT),
                "text_passed_unchanged": True,
                "language": LANGUAGE,
                "torch_seed": TORCH_SEED,
                "output_path": str(output_path.resolve()),
                "generation_seconds": round(generation_seconds, 6),
                "real_time_factor": round(generation_seconds / duration, 6),
                "source_waveform": {
                    "sample_count": int(waveform.size),
                    "all_finite": source_all_finite,
                    "peak_amplitude": round(source_peak, 9),
                    "rms": round(source_rms, 9),
                    "silently_normalized_or_truncated": False,
                },
                **wav_validation,
            }
            write_json(run_dir / f"{speaker.lower()}.result.json", result)
            speaker_results.append(result)

        if speaker_results[0]["sha256"] == speaker_results[1]["sha256"]:
            raise SmokeTestError("Serena 与 Vivian WAV SHA256 相同，无法证明音色输出不同")
        if (run_dir / "serena.wav").read_bytes() == (run_dir / "vivian.wav").read_bytes():
            raise SmokeTestError("Serena 与 Vivian WAV 字节内容相同")

        summary = {
            "status": "SUCCEEDED",
            "model_load_count": 1,
            "model_load_seconds": round(model_load_seconds, 6),
            "model_device": str(getattr(model, "device", "unknown")),
            "hf_device_map": device_map,
            "cpu_offload_used": cpu_offload_used,
            "supported_speakers": speakers_supported,
            "supported_languages": languages_supported,
            "speaker_results": speaker_results,
            "child_elapsed_seconds": round(time.perf_counter() - started, 6),
        }
        write_json(run_dir / "child-result.json", summary)
        write_progress(run_dir, "CLEANUP", status="SUCCEEDED")
        return 0
    except Exception as exc:  # noqa: BLE001 - trace must be persisted for a spike
        failure = {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "failed_at": utc_now(),
        }
        write_json(run_dir / "child-error.json", failure)
        write_progress(run_dir, "FAILED", error_type=type(exc).__name__, error=str(exc))
        traceback.print_exc()
        return 1
    finally:
        try:
            if model is not None:
                del model
            gc.collect()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except (RuntimeError, AttributeError):
                    pass
        except Exception:  # noqa: BLE001 - cleanup failure is visible in stderr
            traceback.print_exc()


def supervisor_main(args: argparse.Namespace) -> int:
    run_id = args.run_id or default_run_id()
    output_root = args.output_root.resolve()
    run_dir = output_root / run_id
    if run_dir.exists():
        raise SmokeTestError(f"运行目录已存在，拒绝覆盖：{run_dir}")
    run_dir.mkdir(parents=True)

    tts_python = args.tts_python.resolve()
    model_dir = args.model_dir.resolve()
    result_path = run_dir / "result.json"
    process: subprocess.Popen[Any] | None = None
    monitor = GpuMemoryMonitor()
    termination: dict[str, Any] = {}
    started = time.perf_counter()
    failure: dict[str, Any] | None = None
    child_pid: int | None = None
    post_cleanup_mib: int | None = None
    gpu_released = False

    request = {
        "request_version": "m5a.qwen3-tts-smoke.v1",
        "created_at": utc_now(),
        "run_id": run_id,
        "provider": "qwen3-tts-local-spike",
        "formal_audio_provider": False,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_source": MODEL_SOURCE,
        "model_license": MODEL_LICENSE,
        "model_dir": str(model_dir),
        "package": f"{PACKAGE_NAME}=={PACKAGE_VERSION}",
        "text": TEXT,
        "text_sha256": text_sha256(TEXT),
        "text_character_count": len(TEXT),
        "language": LANGUAGE,
        "speakers": list(SPEAKERS),
        "speaker_order": list(SPEAKERS),
        "torch_seed_reused_per_speaker": TORCH_SEED,
        "attention_implementation": ATTENTION_IMPLEMENTATION,
        "dtype": "bfloat16",
        "device_map": "cuda:0",
        "local_files_only": True,
        "cloud_api_used": False,
        "reference_audio_used": False,
        "voice_cloning_used": False,
        "voice_design_used": False,
        "text_rewriting_or_truncation": False,
        "timeouts_seconds": {
            "model_load": args.model_load_timeout,
            "per_speaker_generation": args.generation_timeout,
            "total_child": args.total_timeout,
            "gpu_release": GPU_RELEASE_TIMEOUT_SECONDS,
        },
    }
    write_json(run_dir / "request.json", request)

    try:
        if not tts_python.is_file():
            raise SmokeTestError(f"独立 TTS Python 不存在：{tts_python}")
        if not port_is_free("127.0.0.1", 8081) or not port_is_free("127.0.0.1", 8188):
            raise SmokeTestError("8081 或 8188 正在监听；拒绝与 Qwen/ComfyUI 同时运行")
        model_files = validate_model(model_dir, run_dir / "model_files.json")

        command = [
            str(tts_python),
            "-u",
            str(Path(__file__).resolve()),
            "--child",
            "--run-dir",
            str(run_dir),
            "--model-dir",
            str(model_dir),
        ]
        request["child_command"] = subprocess.list2cmdline(command)
        write_json(run_dir / "request.json", request)

        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HOME": str((PROJECT_ROOT / ".cache-qwen3-tts" / "runtime").resolve()),
                "PYTHONUTF8": "1",
            }
        )
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        monitor.start()
        with (run_dir / "stdout.log").open("wb") as stdout_handle, (
            run_dir / "stderr.log"
        ).open("wb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                creationflags=creationflags,
            )
            child_pid = process.pid
            last_stage = "PROCESS_START"
            stage_started = time.monotonic()
            total_started = stage_started
            while process.poll() is None:
                progress = read_json(run_dir / "progress.json") or {}
                stage = str(progress.get("stage") or "PROCESS_START")
                if stage != last_stage:
                    last_stage = stage
                    stage_started = time.monotonic()
                elapsed_stage = time.monotonic() - stage_started
                elapsed_total = time.monotonic() - total_started
                if elapsed_total > args.total_timeout:
                    raise SmokeTestError(
                        f"TTS 子进程超过总超时 {args.total_timeout:.0f} 秒；阶段={stage}"
                    )
                if stage in {"PROCESS_START", "IMPORT_RUNTIME", "MODEL_LOAD"}:
                    if elapsed_stage > args.model_load_timeout:
                        raise SmokeTestError(
                            f"模型加载阶段超过 {args.model_load_timeout:.0f} 秒；阶段={stage}"
                        )
                if stage in {"SERENA_GENERATION", "VIVIAN_GENERATION"}:
                    if elapsed_stage > args.generation_timeout:
                        raise SmokeTestError(
                            f"{stage} 超过 {args.generation_timeout:.0f} 秒"
                        )
                time.sleep(0.5)

            if process.returncode != 0:
                child_error = read_json(run_dir / "child-error.json") or {}
                raise SmokeTestError(
                    f"TTS 子进程失败（退出码 {process.returncode}）："
                    f"{child_error.get('error') or '请查看 stderr.log'}"
                )

        child_result = read_json(run_dir / "child-result.json")
        if not child_result or child_result.get("status") != "SUCCEEDED":
            raise SmokeTestError("子进程未写入成功的 child-result.json")
        if child_result.get("model_load_count") != 1:
            raise SmokeTestError("模型并非只加载一次")
        runtime_environment = read_json(run_dir / "environment.json")
        if not runtime_environment:
            raise SmokeTestError("子进程未写入 environment.json")

        speaker_results: list[dict[str, Any]] = []
        for speaker in SPEAKERS:
            speaker_path = run_dir / f"{speaker.lower()}.result.json"
            payload = read_json(speaker_path)
            if not payload:
                raise SmokeTestError(f"缺少音色追溯文件：{speaker_path}")
            parent_validation = inspect_pcm16_wav(run_dir / f"{speaker.lower()}.wav")
            if payload.get("sha256") != parent_validation["sha256"]:
                raise SmokeTestError(f"{speaker} 子进程与父进程 WAV SHA256 不一致")
            if payload.get("text") != TEXT or not payload.get("text_passed_unchanged"):
                raise SmokeTestError(f"{speaker} 未保留原始测试文本")
            payload["supervisor_wave_validation"] = parent_validation
            write_json(speaker_path, payload)
            speaker_results.append(payload)
        if speaker_results[0]["sha256"] == speaker_results[1]["sha256"]:
            raise SmokeTestError("两个音色的 WAV SHA256 相同")

        result = {
            "status": "SUCCEEDED",
            "provider": "qwen3-tts-local-spike",
            "formal_audio_provider": False,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_source": MODEL_SOURCE,
            "model_license": MODEL_LICENSE,
            "model_path": str(model_dir),
            "model_total_size_bytes": model_files["total_size_bytes"],
            "critical_model_sha256": EXPECTED_CRITICAL_SHA256,
            "text": TEXT,
            "text_sha256": text_sha256(TEXT),
            "text_passed_unchanged": True,
            "speakers": list(SPEAKERS),
            "sample_rate": speaker_results[0]["sample_rate"],
            "channels": speaker_results[0]["channels"],
            "sample_width_bytes": speaker_results[0]["sample_width_bytes"],
            "audio_format": speaker_results[0]["format"],
            "speaker_results": speaker_results,
            "model_load_count": child_result["model_load_count"],
            "model_load_seconds": child_result["model_load_seconds"],
            "cpu_offload_used": child_result["cpu_offload_used"],
            "python_version": runtime_environment["python_version"],
            "pytorch_version": runtime_environment["torch_version"],
            "cuda_runtime": runtime_environment["cuda_runtime"],
            "oom": False,
            "cloud_api_used": False,
            "voice_cloning_used": False,
            "reference_audio_used": False,
            "attention_implementation": ATTENTION_IMPLEMENTATION,
            "two_voice_hashes_differ": True,
            "automatic_pronunciation_or_emotion_acceptance": False,
            "manual_listening_required": True,
            "child_pid": child_pid,
            "child_returncode": process.returncode,
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
            "output_dir": str(run_dir),
            "completed_at": utc_now(),
        }
        write_json(result_path, result)
    except Exception as exc:  # noqa: BLE001 - final trace belongs in result.json
        failure = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if "out of memory" in str(exc).lower() or "cuda oom" in str(exc).lower():
            failure["oom"] = True
        if process is not None and process.poll() is None:
            termination = terminate_owned_process(process)
    finally:
        if process is not None and process.poll() is None:
            termination = terminate_owned_process(process)
        post_cleanup_mib, gpu_released = wait_for_gpu_release(monitor.baseline_mib)
        monitor.stop()
        if process is not None:
            # The supervisor owns this Popen handle.  A non-None return code is
            # stronger evidence than a system-wide task-list query and remains
            # available in restricted Windows shells where tasklist is denied.
            process_residual = process.poll() is None
            process_verification = "owned subprocess.Popen.poll()"
        else:
            process_residual = bool(child_pid and process_exists(child_pid))
            process_verification = "system process lookup fallback"
        gpu_summary = monitor.summary(post_cleanup_mib)
        cleanup = {
            "child_pid": child_pid,
            "owned_child_exited": not process_residual,
            "owned_tts_process_residual": process_residual,
            "owned_process_verification": process_verification,
            "termination": termination,
            "gpu_released_to_baseline_allowance": gpu_released,
            "gpu_release_allowance_mib": GPU_RELEASE_ALLOWANCE_MIB,
            "ports": {
                "8081_free": port_is_free("127.0.0.1", 8081),
                "8188_free": port_is_free("127.0.0.1", 8188),
            },
        }
        existing = read_json(result_path) or {
            "status": "FAILED",
            "provider": "qwen3-tts-local-spike",
            "formal_audio_provider": False,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_source": MODEL_SOURCE,
            "model_license": MODEL_LICENSE,
            "text": TEXT,
            "speakers": list(SPEAKERS),
            "cloud_api_used": False,
            "voice_cloning_used": False,
            "reference_audio_used": False,
            "oom": bool(failure and failure.get("oom")),
        }
        existing["gpu_memory_observed"] = gpu_summary
        existing["cleanup"] = cleanup
        if failure:
            existing["failure"] = failure
        if existing.get("status") == "SUCCEEDED" and (
            process_residual or not gpu_released or not all(cleanup["ports"].values())
        ):
            existing["status"] = "FAILED"
            existing["failure"] = {
                "error_type": "CleanupValidationError",
                "error": "TTS 进程、GPU 显存或端口未完成清理验收",
            }
        existing["finalized_at"] = utc_now()
        write_json(result_path, existing)

    final = read_json(result_path) or {}
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final.get("status") == "SUCCEEDED" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="有界生成 Serena/Vivian 两个真实本地中文 TTS WAV"
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id")
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--tts-python", type=Path, default=DEFAULT_TTS_PYTHON)
    parser.add_argument("--model-load-timeout", type=float, default=300.0)
    parser.add_argument("--generation-timeout", type=float, default=300.0)
    parser.add_argument("--total-timeout", type=float, default=900.0)
    args = parser.parse_args()
    for name in ("model_load_timeout", "generation_timeout", "total_timeout"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须大于 0")
    if args.child and args.run_dir is None:
        parser.error("--child 需要 --run-dir")
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.child:
            return child_main(args)
        return supervisor_main(args)
    except (SmokeTestError, OSError, ValueError) as exc:
        print(f"M5-A TTS SMOKE TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

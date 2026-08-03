"""M4-B 真实图像 Job 的受控 ScriptV1 快照与 GPU 交接检查。"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..media.ffmpeg import sha256_file
from ..models import GenerationJob, Project
from ..script_schema import ScriptV1


REAL_IMAGE_JOB_TYPE = "GENERATE_REAL_IMAGE_VIDEO"
REAL_IMAGE_PROVIDER_ID = "comfyui-animagine-xl-4"


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


class GpuMemoryMonitor:
    """按 GPU 全卡采样显存；WDDM 下是审计观测，不冒充进程精确统计。"""

    def __init__(self, *, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.baseline_mib = _gpu_memory_used_mib()
        self.peak_mib = self.baseline_mib
        self.sample_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sample,
            name="m4-gpu-memory-monitor",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._thread.join(timeout=max(5.0, self.interval_seconds * 2))

    def _sample(self) -> None:
        while not self._stop.is_set():
            value = _gpu_memory_used_mib()
            if value is not None:
                self.sample_count += 1
                if self.peak_mib is None or value > self.peak_mib:
                    self.peak_mib = value
            self._stop.wait(self.interval_seconds)

    def summary(self) -> dict[str, Any]:
        additional = None
        if self.baseline_mib is not None and self.peak_mib is not None:
            additional = max(0, self.peak_mib - self.baseline_mib)
        return {
            "baseline_mib": self.baseline_mib,
            "peak_mib": self.peak_mib,
            "additional_mib": additional,
            "sample_count": self.sample_count,
            "method": (
                "nvidia-smi GPU-wide memory.used sampled once per second; "
                "Windows WDDM includes display and other GPU processes"
            ),
        }


class RealImageJobError(RuntimeError):
    """携带可直接落库和展示的 M4-B 结构化失败。"""

    def __init__(
        self,
        *,
        code: str,
        stage: str,
        summary: str,
        failed_shot_id: str | None = None,
        failed_shot_index: int | None = None,
        completed_image_count: int = 0,
        total_image_count: int | None = None,
        retryable: bool = True,
        requires_qwen_shutdown: bool = False,
        oom: bool = False,
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
            "completed_image_count": completed_image_count,
            "total_image_count": total_image_count,
            "retryable": retryable,
            "requires_qwen_shutdown": requires_qwen_shutdown,
            "oom": oom,
            "log_paths": log_paths or {},
            "suggestions": suggestions or [],
            "provider_id": REAL_IMAGE_PROVIDER_ID,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
        }


def _tcp_listening(host: str, port: int, timeout_seconds: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _llama_process_detected() -> bool:
    """只读检查进程名；不查询命令行，更不会终止外部进程。"""

    if os.name != "nt":
        return False
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
        return False
    if completed.returncode != 0:
        return False
    for row in csv.reader(io.StringIO(completed.stdout)):
        if row and "llama-server" in row[0].lower():
            return True
    return False


def gpu_handoff_status(
    *,
    llama_host: str = "127.0.0.1",
    llama_port: int = 8081,
) -> dict[str, bool]:
    port_listening = _tcp_listening(llama_host, llama_port)
    process_detected = _llama_process_detected()
    return {
        "conflict": port_listening or process_detected,
        "llama_port_listening": port_listening,
        "llama_process_detected": process_detected,
    }


def gpu_handoff_error_payload() -> dict[str, Any]:
    return RealImageJobError(
        code="GPU_HANDOFF_REQUIRED",
        stage="GPU_HANDOFF_REQUIRED",
        summary="本机8GB显存模式需要先停止Qwen服务，再开始真实图像生成。",
        requires_qwen_shutdown=True,
        suggestions=[
            "停止 llama-server 并确认 8081 已释放。",
            "不要让 Qwen 与 Animagine 同时占用 RTX 4060 8GB 显存。",
            "释放显存后手动重试；平台不会终止用户进程。",
        ],
    ).generation_error


def _trace_script(
    settings: Settings,
    *,
    project_id: str,
    source_job_id: str,
    source_result: dict[str, Any],
) -> tuple[ScriptV1 | None, dict[str, Any]]:
    trace = source_result.get("script_trace")
    if not isinstance(trace, dict):
        return None, {}
    report_value = trace.get("validation_report_path")
    if not isinstance(report_value, str) or not report_value:
        return None, trace
    report_path = Path(report_value)
    if not report_path.is_absolute():
        report_path = Path(settings.data_dir) / report_path
    trace_path = (report_path.resolve().parent / "trace.json").resolve()
    source_root = (settings.project_dir(project_id) / "jobs" / source_job_id).resolve()
    try:
        trace_path.relative_to(source_root)
    except ValueError as exc:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="来源 ScriptV1 追溯路径越过受控 Job 目录。",
            retryable=False,
        ) from exc
    try:
        payload = json.loads(trace_path.read_text(encoding="utf-8"))
        return ScriptV1.model_validate(payload["validated_script"]), trace
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="来源 Job 的严格 ScriptV1 追溯文件缺失或损坏。",
            retryable=False,
        ) from exc


def script_from_source_job(
    settings: Settings,
    *,
    project: Project,
    source_job: GenerationJob,
) -> tuple[ScriptV1, dict[str, Any]]:
    """读取成功来源 Job 的最终 ScriptV1；绝不调用 ScriptProvider。"""

    source_result = dict(source_job.result_json or {})
    embedded = source_result.get("script_json")
    if isinstance(embedded, dict):
        try:
            return ScriptV1.model_validate(embedded), {
                "source": "source_job.result_json.script_json",
                "script_trace": source_result.get("script_trace") or {},
            }
        except ValueError as exc:
            raise RealImageJobError(
                code="SCRIPT_SNAPSHOT_INVALID",
                stage="SCRIPT_REUSE",
                summary="来源 Job 内嵌的 ScriptV1 已损坏。",
                retryable=False,
            ) from exc

    traced, trace = _trace_script(
        settings,
        project_id=project.id,
        source_job_id=source_job.id,
        source_result=source_result,
    )
    if traced is not None:
        return traced, {"source": "source_job.trace.json", "script_trace": trace}

    # 兼容早期 Mock 成功任务：这些任务没有独立 trace.json。仅当项目当前
    # ScriptV1 与来源 Job 的镜头数一致时允许立即做不可变快照，并明确记录来源。
    if isinstance(project.script_json, dict):
        try:
            current = ScriptV1.model_validate(project.script_json)
        except ValueError as exc:
            raise RealImageJobError(
                code="SCRIPT_SNAPSHOT_INVALID",
                stage="SCRIPT_REUSE",
                summary="项目当前 ScriptV1 无法通过严格校验。",
                retryable=False,
            ) from exc
        actual_count = source_result.get("actual_shot_count")
        if actual_count == len(current.shots):
            return current, {
                "source": "project.script_json_legacy_mock_compatibility",
                "script_trace": source_result.get("script_trace") or {},
                "warnings": [
                    "来源为早期 Mock Job；入队时已把项目当前严格 ScriptV1 固化到新 Job。"
                ],
            }

    raise RealImageJobError(
        code="SCRIPT_SNAPSHOT_MISSING",
        stage="SCRIPT_REUSE",
        summary="来源 Job 没有可安全复用的严格 ScriptV1。",
        retryable=False,
        suggestions=["重新生成并成功保存一次结构化剧本后再生成真实动漫画面。"],
    )


def write_script_snapshot(
    settings: Settings,
    *,
    project_id: str,
    image_job_id: str,
    source_job_id: str,
    source_script_provider: str,
    script: ScriptV1,
    source_trace: dict[str, Any],
) -> tuple[Path, str]:
    job_root = (settings.project_dir(project_id) / "jobs" / image_job_id).resolve()
    job_root.mkdir(parents=True, exist_ok=True)
    path = job_root / "script-source.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "snapshot_version": "m4.script-reuse.v1",
                "source_script_job_id": source_job_id,
                "source_script_provider": source_script_provider,
                "validated_script": script.model_dump(mode="json"),
                "source_trace": source_trace,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path, sha256_file(path)


def load_script_snapshot(
    settings: Settings,
    *,
    project_id: str,
    image_job_id: str,
    request_snapshot: dict[str, Any],
) -> tuple[ScriptV1, dict[str, Any]]:
    path_value = request_snapshot.get("script_snapshot_path")
    expected_sha = request_snapshot.get("script_snapshot_sha256")
    if not isinstance(path_value, str) or not path_value:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_MISSING",
            stage="SCRIPT_REUSE",
            summary="真实图像 Job 缺少 ScriptV1 快照路径。",
            retryable=False,
        )
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="真实图像 Job 缺少 ScriptV1 快照 SHA256。",
            retryable=False,
        )
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(settings.data_dir) / path
    path = path.resolve()
    owner_job_id = request_snapshot.get("script_snapshot_owner_job_id", image_job_id)
    if not isinstance(owner_job_id, str) or not owner_job_id:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="ScriptV1 快照缺少合法的归属 Job。",
            retryable=False,
        )
    job_root = (settings.project_dir(project_id) / "jobs" / owner_job_id).resolve()
    try:
        path.relative_to(job_root)
    except ValueError as exc:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="ScriptV1 快照路径越过当前 Job 目录。",
            retryable=False,
        ) from exc
    if not path.is_file() or sha256_file(path) != expected_sha.lower():
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="ScriptV1 快照缺失或 SHA256 不匹配。",
            retryable=False,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        script = ScriptV1.model_validate(payload["validated_script"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="ScriptV1 快照无法通过严格校验。",
            retryable=False,
        ) from exc
    if payload.get("source_script_job_id") != request_snapshot.get(
        "source_script_job_id"
    ):
        raise RealImageJobError(
            code="SCRIPT_SNAPSHOT_INVALID",
            stage="SCRIPT_REUSE",
            summary="ScriptV1 快照的来源 Job 与请求快照不一致。",
            retryable=False,
        )
    return script, payload

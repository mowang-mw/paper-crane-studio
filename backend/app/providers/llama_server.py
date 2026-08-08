"""Job-scoped llama-server lifecycle with explicit process ownership."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)

LlamaRuntimeState = Literal[
    "READY_TO_START",
    "ONLINE",
    "CONFIG_ERROR",
    "PORT_CONFLICT",
]


@dataclass(frozen=True, slots=True)
class LlamaServerInspection:
    state: LlamaRuntimeState
    detail: str
    server_version: str | None = None
    model_ids: tuple[str, ...] = ()


class LlamaServerLifecycleError(RuntimeError):
    """Startup/ownership failures surfaced as structured Job failures."""

    def __init__(self, code: str, message: str, *, model_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.generation_error = {
            "code": code,
            "stage": "MODEL_STARTUP",
            "summary": message,
            "story_char_count": None,
            "story_length_valid": None,
            "desired_shot_count": None,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
            "suggestions": (
                [
                    "先结束 ComfyUI 并确认显存释放，再手动重试 Script Job。",
                    "平台不会为了启动 Qwen 而结束外部 GPU 进程。",
                ]
                if code == "GPU_HANDOFF_REQUIRED"
                else [
                    "检查 llama-server 可执行文件、GGUF 模型与 8081 端口后手动重试。",
                    "若端口由外部程序占用，请先确认其身份；平台不会结束未知进程。",
                ]
            ),
            "provider_id": "llamacpp",
            "model_id": model_id,
            "raw_response_path": None,
            "repair_response_path": None,
            "validation_report_path": None,
        }


def _service_root(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value[:-3] if value.endswith("/v1") else value


def _endpoint(base_url: str) -> tuple[str, int]:
    parsed = urlparse(_service_root(base_url))
    if parsed.hostname is None:
        raise ValueError("llama-server 地址缺少主机名")
    return parsed.hostname, parsed.port or 80


def _port_is_listening(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port_release(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _port_is_listening(host, port):
            return True
        time.sleep(0.25)
    return not _port_is_listening(host, port)


def inspect_llama_server(
    *,
    base_url: str,
    model_id: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
    port_is_listening: Callable[[str, int], bool] | None = None,
) -> LlamaServerInspection:
    """Classify an endpoint without starting or terminating any process."""

    host, port = _endpoint(base_url)
    listener = port_is_listening or _port_is_listening
    if not listener(host, port):
        return LlamaServerInspection(
            state="READY_TO_START",
            detail="llama.cpp 已配置，当前未运行；可在 Script Job 中按需启动。",
        )

    root = _service_root(base_url)
    owns_client = client is None
    active_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        health = active_client.get(f"{root}/health")
        health.raise_for_status()
        models = active_client.get(f"{root}/v1/models")
        models.raise_for_status()
        props = active_client.get(f"{root}/props")
        props.raise_for_status()
        health_payload = health.json()
        models_payload = models.json()
        props_payload = props.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return LlamaServerInspection(
            state="PORT_CONFLICT",
            detail=(
                f"端口 {port} 已被占用，但不是可复用的 llama.cpp 服务："
                f"{type(exc).__name__}"
            ),
        )
    finally:
        if owns_client:
            active_client.close()

    model_items = models_payload.get("data") if isinstance(models_payload, dict) else None
    model_ids = tuple(
        item["id"]
        for item in model_items or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    server_version = (
        props_payload.get("build_info") if isinstance(props_payload, dict) else None
    )
    compatible = (
        isinstance(health_payload, dict)
        and health_payload.get("status") == "ok"
        and model_id in model_ids
        and isinstance(server_version, str)
        and bool(server_version.strip())
    )
    if not compatible:
        return LlamaServerInspection(
            state="PORT_CONFLICT",
            detail=(
                f"端口 {port} 上存在服务，但健康状态或模型 ID 与当前配置不兼容；"
                "平台不会结束该外部进程。"
            ),
            server_version=server_version if isinstance(server_version, str) else None,
            model_ids=model_ids,
        )
    return LlamaServerInspection(
        state="ONLINE",
        detail=f"兼容的 llama.cpp 服务在线；已加载：{', '.join(model_ids)}",
        server_version=server_version.strip(),
        model_ids=model_ids,
    )


ProcessFactory = Callable[..., Any]
InspectionFactory = Callable[[], LlamaServerInspection]
ProcessTerminator = Callable[[Any], None]


class LlamaServerJobSession:
    """Reuse compatible external servers; own and reclaim only Job-started ones."""

    def __init__(
        self,
        *,
        executable: Path,
        model_path: Path,
        model_id: str,
        base_url: str,
        run_dir: Path,
        context_size: int,
        gpu_layers: int,
        startup_timeout_seconds: float,
        health_timeout_seconds: float,
        process_factory: ProcessFactory | None = None,
        inspection_factory: InspectionFactory | None = None,
        process_terminator: ProcessTerminator | None = None,
        blocked_gpu_ports: tuple[int, ...] = (),
        port_release_waiter: Callable[[str, int, float], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.executable = Path(executable).resolve()
        self.model_path = Path(model_path).resolve()
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.run_dir = Path(run_dir).resolve()
        self.context_size = context_size
        self.gpu_layers = gpu_layers
        self.startup_timeout_seconds = startup_timeout_seconds
        self.health_timeout_seconds = health_timeout_seconds
        self.process_factory = process_factory or subprocess.Popen
        self.inspection_factory = inspection_factory or self._inspect
        self.process_terminator = process_terminator or self._terminate_process_tree
        self.blocked_gpu_ports = blocked_gpu_ports
        self.port_release_waiter = port_release_waiter or _wait_for_port_release
        self.sleep = sleep
        self.process: Any = None
        self.ownership: Literal["OWNED_BY_JOB", "EXTERNAL_REUSED"] | None = None
        self.cleaned_up = False
        self.ready_inspection: LlamaServerInspection | None = None
        self.stdout_path = self.run_dir / "llama-server.stdout.log"
        self.stderr_path = self.run_dir / "llama-server.stderr.log"
        self._stdout_handle: Any = None
        self._stderr_handle: Any = None

    def _inspect(self) -> LlamaServerInspection:
        return inspect_llama_server(
            base_url=self.base_url,
            model_id=self.model_id,
            timeout_seconds=self.health_timeout_seconds,
        )

    def __enter__(self) -> "LlamaServerJobSession":
        try:
            host, _ = _endpoint(self.base_url)
            occupied_gpu_ports = [
                port for port in self.blocked_gpu_ports if _port_is_listening(host, port)
            ]
            if occupied_gpu_ports:
                raise LlamaServerLifecycleError(
                    "GPU_HANDOFF_REQUIRED",
                    "检测到 ComfyUI 仍在运行；拒绝同时启动或调用 Qwen。"
                    f"请先释放端口 {', '.join(str(port) for port in occupied_gpu_ports)} "
                    "及 GPU 显存。",
                    model_id=self.model_id,
                )
            inspection = self.inspection_factory()
            if inspection.state == "ONLINE":
                self.ownership = "EXTERNAL_REUSED"
                self.ready_inspection = inspection
                return self
            if inspection.state == "PORT_CONFLICT":
                raise LlamaServerLifecycleError(
                    "LLAMA_SERVER_PORT_CONFLICT",
                    inspection.detail,
                    model_id=self.model_id,
                )
            self._start_owned()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
            logger.exception("llama-server cleanup failed while handling another error")

    def _start_owned(self) -> None:
        if not self.executable.is_file() or self.executable.suffix.casefold() != ".exe":
            raise LlamaServerLifecycleError(
                "LLAMA_SERVER_CONFIG_ERROR",
                f"llama-server 可执行文件不存在或不是 .exe：{self.executable}",
                model_id=self.model_id,
            )
        if not self.model_path.is_file() or self.model_path.suffix.casefold() != ".gguf":
            raise LlamaServerLifecycleError(
                "LLAMA_SERVER_CONFIG_ERROR",
                f"GGUF 模型文件不存在或格式不正确：{self.model_path}",
                model_id=self.model_id,
            )
        host, port = _endpoint(self.base_url)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "--model",
            str(self.model_path),
            "--alias",
            self.model_id,
            "--host",
            host,
            "--port",
            str(port),
            "--ctx-size",
            str(self.context_size),
            "--n-gpu-layers",
            str(self.gpu_layers),
            "--parallel",
            "1",
            "--flash-attn",
            "on",
            "--jinja",
            "--reasoning",
            "off",
            "--metrics",
            "--cors-origins",
            "localhost",
            "--no-cors-credentials",
            "--no-webui",
        ]
        (self.run_dir / "llama-server-command.json").write_text(
            json.dumps({"args": command}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._stdout_handle = self.stdout_path.open("wb")
        self._stderr_handle = self.stderr_path.open("wb")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = self.process_factory(
                command,
                cwd=self.executable.parent,
                stdin=subprocess.DEVNULL,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                shell=False,
                creationflags=creationflags,
            )
            self.ownership = "OWNED_BY_JOB"
        except OSError as exc:
            raise LlamaServerLifecycleError(
                "LLAMA_SERVER_START_FAILED",
                f"无法启动 llama-server：{exc}",
                model_id=self.model_id,
            ) from exc

        deadline = time.monotonic() + self.startup_timeout_seconds
        last_detail = "等待健康检查"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LlamaServerLifecycleError(
                    "LLAMA_SERVER_START_FAILED",
                    f"llama-server 在就绪前退出，退出码 {self.process.returncode}",
                    model_id=self.model_id,
                )
            inspection = self.inspection_factory()
            last_detail = inspection.detail
            if inspection.state == "ONLINE":
                self.ready_inspection = inspection
                return
            self.sleep(0.5)
        raise LlamaServerLifecycleError(
            "LLAMA_SERVER_START_TIMEOUT",
            f"llama-server 启动后未在 {self.startup_timeout_seconds:g}s 内就绪：{last_detail}",
            model_id=self.model_id,
        )

    @staticmethod
    def _terminate_process_tree(process: Any) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=15.0)
                return
            except (OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=30.0,
                    shell=False,
                )
        else:
            process.terminate()
        try:
            process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10.0)

    def close(self) -> None:
        try:
            if self.ownership == "OWNED_BY_JOB" and self.process is not None:
                self.process_terminator(self.process)
                host, port = _endpoint(self.base_url)
                if not self.port_release_waiter(host, port, 30.0):
                    raise LlamaServerLifecycleError(
                        "LLAMA_SERVER_CLEANUP_FAILED",
                        f"Job 启动的 llama-server 结束后端口 {port} 未释放。",
                        model_id=self.model_id,
                    )
                self.cleaned_up = True
        finally:
            for handle_name in ("_stdout_handle", "_stderr_handle"):
                handle = getattr(self, handle_name)
                if handle is not None and not handle.closed:
                    handle.close()

    def snapshot(self) -> dict[str, Any]:
        inspection = self.ready_inspection
        return {
            "ownership": self.ownership,
            "started_by_job": self.ownership == "OWNED_BY_JOB",
            "external_server_reused": self.ownership == "EXTERNAL_REUSED",
            "cleaned_up": self.cleaned_up,
            "pid": getattr(self.process, "pid", None),
            "runtime_state": inspection.state if inspection else None,
            "server_version": inspection.server_version if inspection else None,
            "model_ids": list(inspection.model_ids) if inspection else [],
            "stdout_path": str(self.stdout_path) if self.ownership == "OWNED_BY_JOB" else None,
            "stderr_path": str(self.stderr_path) if self.ownership == "OWNED_BY_JOB" else None,
        }

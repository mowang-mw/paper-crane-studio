"""Run one bounded Animagine XL image spike through the local ComfyUI API.

This is an M4-A experiment only.  It deliberately does not import the backend,
start the application worker, or modify the production ImageProvider contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_FILENAME = "animagine-xl-4.0-opt.safetensors"
MODEL_NAME = "cagliostrolab/animagine-xl-4.0"
MODEL_SHA256 = "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
MODEL_LICENSE = "CreativeML Open RAIL++-M"
DEFAULT_SEED = 20260802
DEFAULT_PORT = 8188
ORIGINAL_CHINESE_VISUAL = (
    "深夜的旧书店里，一名原创少女翻开发光的画册，一条蓝色发光鲸鱼从书页中游出；"
    "蓝紫色夜间光线，动漫电影关键帧，横向构图。"
)
POSITIVE_PROMPT = (
    "1girl, original character, solo, young woman, dark blue bob cut, modest casual clothing, "
    "inside an old bookstore at midnight, wooden bookshelves, holding open book, open picture book, "
    "looking at book, glowing book, (blue whale:1.3), (whale floating above book:1.3), "
    "whale emerging from book, magical blue particles, astonished expression, "
    "blue and violet night lighting, cinematic anime keyframe, medium wide shot, horizontal composition, "
    "safe, no text, no watermark, masterpiece, high score, great score, absurdres"
)
NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, extra fingers, missing fingers, malformed hands, text, watermark, "
    "logo, signature, blurry, worst quality, low quality, low score, bad score, average score, cropped"
)


class SmokeTestError(RuntimeError):
    """Raised when the bounded spike cannot complete safely."""


class ComfyExecutionError(SmokeTestError):
    """Raised when ComfyUI reports a workflow execution error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def run_capture(args: list[str], *, timeout: float = 30.0) -> str:
    completed = subprocess.run(
        args,
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
            f"命令失败（退出码 {completed.returncode}）：{args!r}\n{detail}"
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
    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.baseline_mib = gpu_memory_used_mib()
        self.peak_mib = self.baseline_mib
        self.sample_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
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

    def summary(self) -> dict[str, Any]:
        additional = None
        if self.baseline_mib is not None and self.peak_mib is not None:
            additional = max(0, self.peak_mib - self.baseline_mib)
        return {
            "baseline_mib": self.baseline_mib,
            "peak_mib": self.peak_mib,
            "additional_mib": additional,
            "sample_count": self.sample_count,
            "method": "nvidia-smi GPU-wide memory.used sampled once per second; WDDM includes display processes",
        }


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def wait_for_port_release(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if port_is_free(host, port):
            return True
        time.sleep(0.5)
    return port_is_free(host, port)


def api_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SmokeTestError(f"ComfyUI API 请求失败：{method} {url}: {exc}") from exc


def wait_for_health(base_url: str, process: subprocess.Popen[bytes], timeout_seconds: float) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error = "尚未响应"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeTestError(f"ComfyUI 在健康检查前退出，退出码 {process.returncode}")
        try:
            return api_json("GET", f"{base_url}/system_stats", timeout=5.0)
        except SmokeTestError as exc:
            last_error = str(exc)
        time.sleep(1.0)
    raise SmokeTestError(f"ComfyUI 健康检查超时（{timeout_seconds:.0f}s）：{last_error}")


def make_workflow(
    *,
    width: int,
    height: int,
    steps: int,
    seed: int,
    cfg: float,
    sampler: str,
    scheduler: str,
) -> dict[str, Any]:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": MODEL_FILENAME},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": POSITIVE_PROMPT, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": NEGATIVE_PROMPT, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "m4_smoke", "images": ["6", 0]},
        },
    }


def submit_and_wait(
    *,
    base_url: str,
    workflow: dict[str, Any],
    client_id: str,
    timeout_seconds: float,
) -> tuple[str, dict[str, Any], float]:
    started = time.monotonic()
    response = api_json(
        "POST",
        f"{base_url}/prompt",
        {"prompt": workflow, "client_id": client_id},
        timeout=30.0,
    )
    prompt_id = str(response.get("prompt_id") or "")
    if not prompt_id:
        raise SmokeTestError(f"ComfyUI 未返回 prompt_id：{response!r}")

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        history = api_json("GET", f"{base_url}/history/{prompt_id}", timeout=15.0)
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status")
            status_str = status.get("status_str") if isinstance(status, dict) else None
            outputs = entry.get("outputs")
            if status_str == "error":
                raise ComfyExecutionError(
                    "ComfyUI 工作流执行失败："
                    + json.dumps(status, ensure_ascii=False)
                )
            if isinstance(outputs, dict) and outputs:
                return prompt_id, entry, time.monotonic() - started
        time.sleep(1.0)
    raise SmokeTestError(f"图像生成总超时（{timeout_seconds:.0f}s），prompt_id={prompt_id}")


def image_descriptor(history_entry: dict[str, Any]) -> dict[str, Any]:
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        raise SmokeTestError("ComfyUI history 不包含 outputs")
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        images = node_output.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return images[0]
    raise SmokeTestError("ComfyUI history 中没有 SaveImage 输出")


def download_output(base_url: str, descriptor: dict[str, Any], target: Path) -> None:
    query = urllib.parse.urlencode(
        {
            "filename": descriptor.get("filename", ""),
            "subfolder": descriptor.get("subfolder", ""),
            "type": descriptor.get("type", "output"),
        }
    )
    request = urllib.request.Request(f"{base_url}/view?{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60.0) as response:
            data = response.read()
    except urllib.error.URLError as exc:
        raise SmokeTestError(f"下载 ComfyUI 输出失败：{exc}") from exc
    if not data:
        raise SmokeTestError("ComfyUI 返回了空图片")
    target.write_bytes(data)


def verify_png(path: Path, expected_size: tuple[int, int]) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            actual_size = image.size
            image_format = image.format
            image_mode = image.mode
    except Exception as exc:
        raise SmokeTestError(f"PNG 无法完整解码：{exc}") from exc
    if image_format != "PNG":
        raise SmokeTestError(f"输出格式不是 PNG：{image_format}")
    if actual_size != expected_size:
        raise SmokeTestError(f"PNG 分辨率不符：要求 {expected_size}，实际 {actual_size}")
    return {
        "format": image_format,
        "width": actual_size[0],
        "height": actual_size[1],
        "mode": image_mode,
        "complete_decode": True,
    }


def terminate_process_tree(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "pid": process.pid,
        "graceful_signal_sent": False,
        "forced_tree_kill": False,
    }
    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            outcome["graceful_signal_sent"] = True
            process.wait(timeout=15.0)
        except (OSError, subprocess.TimeoutExpired):
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30.0,
                shell=False,
            )
            outcome["forced_tree_kill"] = True
            outcome["taskkill_returncode"] = completed.returncode
            outcome["taskkill_output"] = (completed.stdout + completed.stderr).strip()
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                outcome["wait_after_taskkill_timed_out"] = True
    outcome["returncode"] = process.poll()
    outcome["process_exited"] = process.poll() is not None
    return outcome


def build_environment(
    *,
    repo_root: Path,
    comfy_root: Path,
    model_path: Path,
) -> dict[str, Any]:
    import torch

    commit = run_capture(["git", "-C", str(comfy_root), "rev-parse", "HEAD"])
    driver = run_capture(
        [
            shutil.which("nvidia-smi") or "nvidia-smi",
            "--query-gpu=driver_version,name,memory.total",
            "--format=csv,noheader",
        ]
    )
    return {
        "captured_at": utc_now(),
        "project_root": str(repo_root),
        "operating_system": platform.platform(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "comfyui_git_commit": commit,
        "comfyui_version": "0.29.0",
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "nvidia_driver_query": driver,
        "model_path": str(model_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--startup-timeout", type=float, default=240.0)
    parser.add_argument("--generation-timeout", type=float, default=1200.0)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cfg", type=float, default=5.0)
    parser.add_argument("--sampler", default="euler_ancestral")
    parser.add_argument("--scheduler", default="normal")
    parser.add_argument("--no-lowvram", action="store_true")
    parser.add_argument("--run-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    comfy_root = repo_root / "tools" / "ComfyUI"
    model_path = repo_root / "models" / "image" / MODEL_FILENAME
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = repo_root / "data" / "generated" / "m4" / "smoke" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "comfyui.stdout.log"
    stderr_path = run_dir / "comfyui.stderr.log"
    result_path = run_dir / "result.json"
    environment_path = run_dir / "environment.json"
    positive_path = run_dir / "positive_prompt.txt"
    negative_path = run_dir / "negative_prompt.txt"
    workflow_path = run_dir / "workflow_api.json"
    request_path = run_dir / "request.json"
    extra_paths_path = run_dir / "extra_model_paths.yaml"
    output_path = run_dir / "generated.png"
    host = "127.0.0.1"
    base_url = f"http://{host}:{args.port}"
    lowvram = not args.no_lowvram
    process: subprocess.Popen[bytes] | None = None
    monitor = GpuMemoryMonitor()
    cleanup: dict[str, Any] = {
        "process_exited": True,
        "port_released": port_is_free(host, args.port),
    }
    result: dict[str, Any] = {
        "success": False,
        "provider": "comfyui-local-api",
        "model_name": MODEL_NAME,
        "model_path": str(model_path),
        "model_sha256": None,
        "model_license": MODEL_LICENSE,
        "started_at": utc_now(),
        "run_id": run_id,
    }

    try:
        if not comfy_root.joinpath("main.py").is_file():
            raise SmokeTestError(f"ComfyUI 源码不存在：{comfy_root}")
        if not model_path.is_file():
            raise SmokeTestError(f"模型文件不存在：{model_path}")
        if not port_is_free(host, args.port):
            raise SmokeTestError(f"端口 {args.port} 已被占用，拒绝启动第二个服务")

        actual_model_sha = sha256_file(model_path)
        result["model_sha256"] = actual_model_sha
        if actual_model_sha != MODEL_SHA256:
            raise SmokeTestError(
                f"模型 SHA256 不符：要求 {MODEL_SHA256}，实际 {actual_model_sha}"
            )

        environment = build_environment(
            repo_root=repo_root,
            comfy_root=comfy_root,
            model_path=model_path,
        )
        write_json(environment_path, environment)
        positive_path.write_text(POSITIVE_PROMPT + "\n", encoding="utf-8")
        negative_path.write_text(NEGATIVE_PROMPT + "\n", encoding="utf-8")
        workflow = make_workflow(
            width=args.width,
            height=args.height,
            steps=args.steps,
            seed=args.seed,
            cfg=args.cfg,
            sampler=args.sampler,
            scheduler=args.scheduler,
        )
        write_json(workflow_path, workflow)
        request = {
            "created_at": utc_now(),
            "original_chinese_visual_description": ORIGINAL_CHINESE_VISUAL,
            "positive_prompt": POSITIVE_PROMPT,
            "negative_prompt": NEGATIVE_PROMPT,
            "provider": "comfyui-local-api",
            "model_name": MODEL_NAME,
            "model_filename": MODEL_FILENAME,
            "parameters": {
                "batch_size": 1,
                "width": args.width,
                "height": args.height,
                "seed": args.seed,
                "steps": args.steps,
                "cfg": args.cfg,
                "sampler": args.sampler,
                "scheduler": args.scheduler,
                "denoise": 1.0,
                "lowvram": lowvram,
            },
            "api_url": base_url,
            "timeouts_seconds": {
                "startup": args.startup_timeout,
                "generation": args.generation_timeout,
            },
        }
        write_json(request_path, request)
        extra_paths_path.write_text(
            "m4_spike:\n"
            f"  base_path: {repo_root.joinpath('models').as_posix()}\n"
            "  checkpoints: image\n",
            encoding="utf-8",
        )

        command = [
            sys.executable,
            str(comfy_root / "main.py"),
            "--listen",
            host,
            "--port",
            str(args.port),
            "--disable-auto-launch",
            "--disable-all-custom-nodes",
            "--preview-method",
            "none",
            "--extra-model-paths-config",
            str(extra_paths_path),
            "--output-directory",
            str(run_dir / "comfyui-output"),
            "--temp-directory",
            str(run_dir / "comfyui-temp"),
            "--user-directory",
            str(run_dir / "comfyui-user"),
            "--database-url",
            "sqlite:///:memory:",
        ]
        if lowvram:
            command.append("--lowvram")
        for directory in (
            run_dir / "comfyui-output",
            run_dir / "comfyui-temp",
            run_dir / "comfyui-user",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        result["comfyui_command"] = command

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            monitor.start()
            process = subprocess.Popen(
                command,
                cwd=comfy_root,
                stdout=stdout_handle,
                stderr=stderr_handle,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(),
                shell=False,
                creationflags=creationflags,
            )
            result["comfyui_pid"] = process.pid
            health = wait_for_health(base_url, process, args.startup_timeout)
            client_id = str(uuid.uuid4())
            generation_started = time.monotonic()
            try:
                prompt_id, history_entry, generation_seconds = submit_and_wait(
                    base_url=base_url,
                    workflow=workflow,
                    client_id=client_id,
                    timeout_seconds=args.generation_timeout,
                )
                oom_retry = False
            except ComfyExecutionError as first_error:
                if "out of memory" not in str(first_error).lower():
                    raise
                oom_retry = True
                fallback = {"width": 896, "height": 512, "steps": 18}
                api_json(
                    "POST",
                    f"{base_url}/free",
                    {"unload_models": True, "free_memory": True},
                    timeout=30.0,
                )
                workflow = make_workflow(
                    width=fallback["width"],
                    height=fallback["height"],
                    steps=fallback["steps"],
                    seed=args.seed,
                    cfg=args.cfg,
                    sampler=args.sampler,
                    scheduler=args.scheduler,
                )
                write_json(workflow_path, workflow)
                prompt_id, history_entry, _ = submit_and_wait(
                    base_url=base_url,
                    workflow=workflow,
                    client_id=client_id,
                    timeout_seconds=args.generation_timeout,
                )
                generation_seconds = time.monotonic() - generation_started
                args.width = fallback["width"]
                args.height = fallback["height"]
                args.steps = fallback["steps"]

            descriptor = image_descriptor(history_entry)
            download_output(base_url, descriptor, output_path)
            png = verify_png(output_path, (args.width, args.height))
            output_sha = sha256_file(output_path)
            result.update(
                {
                    "success": True,
                    "prompt_id": prompt_id,
                    "seed": args.seed,
                    "width": args.width,
                    "height": args.height,
                    "steps": args.steps,
                    "cfg": args.cfg,
                    "sampler": args.sampler,
                    "scheduler": args.scheduler,
                    "denoise": 1.0,
                    "generation_seconds": round(generation_seconds, 3),
                    "output_path": str(output_path),
                    "output_sha256": output_sha,
                    "png_validation": png,
                    "comfyui_version": environment["comfyui_version"],
                    "comfyui_git_commit": environment["comfyui_git_commit"],
                    "pytorch_version": environment["torch_version"],
                    "cuda_runtime": environment["cuda_runtime"],
                    "lowvram": lowvram,
                    "oom_retry": oom_retry,
                    "workflow_uses_builtin_nodes_only": True,
                    "source_type": "REAL_LOCAL_MODEL",
                    "semantic_review": "pending_manual_visual_review",
                    "comfyui_health": health,
                    "comfyui_output_descriptor": descriptor,
                }
            )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        if process is not None:
            cleanup = terminate_process_tree(process)
        cleanup["port_released"] = wait_for_port_release(host, args.port, 30.0)
        monitor.stop()
        result["peak_gpu_memory_observed"] = monitor.summary()
        result["cleanup"] = cleanup
        result["finished_at"] = utc_now()
        result["output_exists"] = output_path.is_file()
        write_json(result_path, result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") and cleanup.get("process_exited") and cleanup.get("port_released") else 1


if __name__ == "__main__":
    raise SystemExit(main())

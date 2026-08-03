"""Run one bounded M4-A Animagine smoke test through the production ImageProvider.

The script is deliberately a thin orchestration wrapper.  The production
``ComfyUIImageProvider`` owns model verification, the built-in-node workflow,
HTTP timeouts, the one-shot ComfyUI process lifecycle, PNG validation and
cleanup.  This wrapper preserves the original M4-A CLI and trace layout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.providers import (  # noqa: E402
    ComfyUIImageProvider,
    ImageGenerationOptions,
    ImageGenerationRequest,
    ImageProviderError,
)
from backend.app.script_schema import Character, Scene, ScriptV1, Shot  # noqa: E402


MODEL_FILENAME = "animagine-xl-4.0-opt.safetensors"
MODEL_NAME = "cagliostrolab/animagine-xl-4.0"
MODEL_SHA256 = "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
MODEL_LICENSE = "CreativeML Open RAIL++-M"
DEFAULT_SEED = 20_260_802
DEFAULT_PORT = 8188
ORIGINAL_CHINESE_VISUAL = (
    "深夜的旧书店里，一名原创少女翻开发光的画册，一条蓝色发光鲸鱼从书页中游出；"
    "蓝紫色夜间光线，动漫电影关键帧，横向构图。"
)


class SmokeTestError(RuntimeError):
    """M4-A wrapper could not complete its bounded verification."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def run_capture(
    args: list[str],
    *,
    timeout: float = 30.0,
    cwd: Path | None = None,
) -> str:
    completed = subprocess.run(
        args,
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
    """Record an explicitly GPU-wide WDDM observation, not process VRAM."""

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
                "WDDM includes display processes"
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


def wait_for_port_release(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if port_is_free(host, port):
            return True
        time.sleep(0.25)
    return port_is_free(host, port)


def _comfy_runtime(comfy_python: Path, comfy_root: Path) -> dict[str, Any]:
    probe = (
        "import json, platform, sys, torch; "
        "print(json.dumps({"
        "'python_version': sys.version, "
        "'python_executable': sys.executable, "
        "'operating_system': platform.platform(), "
        "'torch_version': torch.__version__, "
        "'cuda_runtime': torch.version.cuda, "
        "'cuda_available': torch.cuda.is_available(), "
        "'cuda_device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None"
        "}))"
    )
    try:
        payload = json.loads(
            run_capture([str(comfy_python), "-c", probe], timeout=60.0, cwd=comfy_root)
        )
    except (json.JSONDecodeError, SmokeTestError) as exc:
        raise SmokeTestError(f"无法读取 ComfyUI 独立环境版本：{exc}") from exc
    if not isinstance(payload, dict):
        raise SmokeTestError("ComfyUI 独立环境版本探针未返回 JSON 对象")
    return payload


def build_environment(
    *,
    repo_root: Path,
    comfy_python: Path,
    comfy_root: Path,
    model_path: Path,
) -> dict[str, Any]:
    runtime = _comfy_runtime(comfy_python, comfy_root)
    commit = run_capture(
        ["git", "-c", f"safe.directory={comfy_root.as_posix()}", "-C", str(comfy_root), "rev-parse", "HEAD"]
    )
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
        "wrapper_python_version": sys.version,
        "wrapper_python_executable": sys.executable,
        "operating_system": runtime.get("operating_system") or platform.platform(),
        "python_version": runtime.get("python_version"),
        "python_executable": runtime.get("python_executable"),
        "comfyui_git_commit": commit,
        "comfyui_version": "0.29.0",
        "torch_version": runtime.get("torch_version"),
        "cuda_runtime": runtime.get("cuda_runtime"),
        "cuda_available": runtime.get("cuda_available"),
        "cuda_device": runtime.get("cuda_device"),
        "nvidia_driver_query": driver,
        "model_path": str(model_path),
        "provider_id": "comfyui-animagine-xl-4",
        "provider_lifecycle": "backend.app.providers.comfyui.ComfyUIJobSession",
    }


def build_smoke_script() -> ScriptV1:
    """Construct a valid ScriptV1 while generating only its first shot."""

    character = Character(
        id="original_girl",
        name="原创少女",
        role="在旧书店发现魔法画册的主角",
        appearance="原创少女，蓝色短发，蓝色眼睛，年轻女性。",
        personality="好奇、谨慎、勇敢",
        costume="朴素便服与黑色外套",
        consistency_prompt="同一原创少女，蓝色短发、蓝色眼睛、黑色外套。",
    )
    scenes = [
        Scene(
            id=f"bookstore_scene_{index}",
            name=f"旧书店场景 {index}",
            description=(
                "深夜旧书店，少女翻开发光画册，蓝色发光鲸鱼从书页中游出。"
                if index == 1
                else "蓝色鲸鱼在旧书店的书架与蓝紫色微光之间游动。"
            ),
            time="深夜",
            lighting="蓝紫色夜间光线与画册微光",
            consistency_prompt="同一深夜旧书店、木制书架、蓝紫色灯光。",
        )
        for index in range(1, 4)
    ]
    shots = [
        Shot(
            id=f"smoke_shot_{index}",
            index=index,
            title=f"画册中的蓝鲸 {index}",
            scene_id=f"bookstore_scene_{index}",
            character_ids=["original_girl"],
            visual_description=(
                ORIGINAL_CHINESE_VISUAL
                if index == 1
                else "原创少女注视蓝色发光鲸鱼在旧书店上空游动。"
            ),
            camera="中远景，缓慢推进",
            image_prompt=(
                "少女手持并翻开发光画册，蓝色鲸鱼漂浮在书页上方，"
                "安全内容，无文字，无水印。"
            ),
            negative_prompt="文字，水印，标志，模糊，手部畸形",
            narration=f"蓝鲸从画册中醒来，这是第 {index} 个故事节点。",
            duration_seconds=8.0,
        )
        for index in range(1, 4)
    ]
    return ScriptV1(
        schema_version="script.v1",
        title="画册里的蓝鲸",
        synopsis="少女在深夜旧书店翻开发光画册，一条蓝色鲸鱼从书页中游出。",
        characters=[character],
        scenes=scenes,
        shots=shots,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--startup-timeout", type=float, default=240.0)
    parser.add_argument("--generation-timeout", type=float, default=1200.0)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="最终单张图片的确定性 seed（Provider 内部保存 base_seed=seed-1）",
    )
    parser.add_argument("--cfg", type=float, default=5.0)
    parser.add_argument("--sampler", default="euler_ancestral")
    parser.add_argument("--scheduler", default="normal")
    parser.add_argument("--no-lowvram", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--comfy-python",
        type=Path,
        default=PROJECT_ROOT / ".venv-comfyui" / "Scripts" / "python.exe",
        help="M4-A 已安装的独立 ComfyUI Python；不会使用当前 anime-platform Python 启动模型",
    )
    return parser.parse_args()


def _copy_provider_trace(asset: Any, run_dir: Path) -> None:
    mappings = (
        (asset.image_path, run_dir / "generated.png"),
        (asset.workflow_path, run_dir / "workflow_api.json"),
        (asset.trace_path.parent / "shot-01.request.json", run_dir / "request.json"),
        (asset.trace_path.parent / "shot-01.positive.txt", run_dir / "positive_prompt.txt"),
        (asset.trace_path.parent / "shot-01.negative.txt", run_dir / "negative_prompt.txt"),
    )
    for source, target in mappings:
        if not Path(source).is_file():
            raise SmokeTestError(f"Provider 未生成必需追溯文件：{source}")
        shutil.copy2(source, target)


def main() -> int:
    args = parse_args()
    if args.seed < 1:
        raise SystemExit("--seed 必须至少为 1，才能映射到 base_seed + shot.index")
    repo_root = PROJECT_ROOT
    comfy_root = repo_root / "tools" / "ComfyUI"
    comfy_python = args.comfy_python.resolve()
    model_path = repo_root / "models" / "image" / MODEL_FILENAME
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = repo_root / "data" / "generated" / "m4" / "smoke" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    environment_path = run_dir / "environment.json"
    output_path = run_dir / "generated.png"
    host = "127.0.0.1"
    lowvram = not args.no_lowvram
    monitor = GpuMemoryMonitor()
    cleanup: dict[str, Any] = {
        "managed_by": "ComfyUIJobSession",
        "process_exited": False,
        "port_released": port_is_free(host, args.port),
    }
    result: dict[str, Any] = {
        "success": False,
        "provider": "comfyui-animagine-xl-4",
        "runtime_backend": "comfyui-local-api",
        "model_name": MODEL_NAME,
        "model_path": str(model_path),
        "model_sha256": None,
        "model_license": MODEL_LICENSE,
        "started_at": utc_now(),
        "run_id": run_id,
    }

    try:
        environment = build_environment(
            repo_root=repo_root,
            comfy_python=comfy_python,
            comfy_root=comfy_root,
            model_path=model_path,
        )
        write_json(environment_path, environment)
        script = build_smoke_script()
        options = ImageGenerationOptions(
            width=args.width,
            height=args.height,
            steps=args.steps,
            cfg=args.cfg,
            sampler=args.sampler,
            scheduler=args.scheduler,
            denoise=1.0,
            batch_size=1,
            base_seed=args.seed - 1,
            lowvram=lowvram,
            startup_timeout_seconds=args.startup_timeout,
            generation_timeout_seconds=args.generation_timeout,
            job_timeout_seconds=args.startup_timeout + args.generation_timeout + 60.0,
            http_timeout_seconds=30.0,
        )
        request = ImageGenerationRequest(
            project_id="m4-a-image-spike",
            job_id=run_id,
            script=script,
            shot=script.shots[0],
            characters=(script.characters[0],),
            scene=script.scenes[0],
            output_dir=image_dir,
            options=options,
        )
        provider = ComfyUIImageProvider(
            comfy_python=comfy_python,
            comfy_root=comfy_root,
            model_path=model_path,
            model_sha256=MODEL_SHA256,
            host=host,
            port=args.port,
        )
        monitor.start()
        asset = provider.generate(request=request)
        _copy_provider_trace(asset, run_dir)
        provider_trace = json.loads(asset.trace_path.read_text(encoding="utf-8"))
        compatibility_request_path = run_dir / "request.json"
        compatibility_request = json.loads(
            compatibility_request_path.read_text(encoding="utf-8")
        )
        compatibility_request.update(
            {
                "original_chinese_visual_description": ORIGINAL_CHINESE_VISUAL,
                "provider": asset.provider_id,
                "model_name": MODEL_NAME,
                "model_filename": MODEL_FILENAME,
                "parameters": {**options.as_dict(), "seed": asset.seed},
                "api_url": f"http://{host}:{args.port}",
            }
        )
        write_json(compatibility_request_path, compatibility_request)
        actual_model_sha = asset.model_sha256
        if actual_model_sha != MODEL_SHA256:
            raise SmokeTestError(
                f"模型 SHA256 不符：要求 {MODEL_SHA256}，实际 {actual_model_sha}"
            )
        output_sha = sha256_file(output_path)
        if output_sha != asset.image_sha256:
            raise SmokeTestError("兼容输出 generated.png 与 Provider 图片 SHA256 不一致")
        session_result = provider_trace.get("session_result")
        if not isinstance(session_result, dict):
            session_result = {}
        command_path = run_dir / "comfyui-command.json"
        comfyui_command = None
        if command_path.is_file():
            command_payload = json.loads(command_path.read_text(encoding="utf-8"))
            if isinstance(command_payload, dict):
                comfyui_command = command_payload.get("args")
        result.update(
            {
                "success": True,
                "model_sha256": actual_model_sha,
                "seed": asset.seed,
                "base_seed": options.base_seed,
                "width": asset.width,
                "height": asset.height,
                "steps": options.steps,
                "cfg": options.cfg,
                "sampler": options.sampler,
                "scheduler": options.scheduler,
                "denoise": options.denoise,
                "generation_seconds": asset.generation_seconds,
                "output_path": str(output_path),
                "output_sha256": output_sha,
                "png_validation": provider_trace.get("png_validation"),
                "comfyui_version": environment["comfyui_version"],
                "comfyui_git_commit": environment["comfyui_git_commit"],
                "pytorch_version": environment["torch_version"],
                "cuda_runtime": environment["cuda_runtime"],
                "lowvram": lowvram,
                "oom_retry": False,
                "workflow_uses_builtin_nodes_only": True,
                "source_type": "REAL_LOCAL_MODEL",
                "semantic_review": "pending_manual_visual_review",
                "prompt_id": session_result.get("prompt_id"),
                "comfyui_output_descriptor": session_result.get("output_descriptor"),
                "comfyui_command": comfyui_command,
                "positive_prompt": asset.positive_prompt,
                "negative_prompt": asset.negative_prompt,
                "provider_trace_path": str(asset.trace_path),
                "image_generation_report_path": str(
                    run_dir / "image_generation_report.json"
                ),
                "warnings": list(asset.warnings),
            }
        )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        if isinstance(exc, ImageProviderError):
            result["generation_error"] = exc.generation_error
        if model_path.is_file():
            try:
                result["model_sha256"] = sha256_file(model_path)
            except OSError:
                pass
    finally:
        monitor.stop()
        cleanup["port_released"] = wait_for_port_release(host, args.port, 30.0)
        # ComfyUIJobSession only returns after its owned process has exited; the
        # independent wrapper check above prevents a false success if 8188 remains.
        cleanup["process_exited"] = cleanup["port_released"]
        result["peak_gpu_memory_observed"] = monitor.summary()
        result["cleanup"] = cleanup
        result["finished_at"] = utc_now()
        result["output_exists"] = output_path.is_file()
        write_json(result_path, result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") and cleanup["port_released"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

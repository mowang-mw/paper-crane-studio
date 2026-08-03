"""M3 ScriptProvider 注册信息与本机 llama.cpp 健康检查。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

import httpx

from ..config import Settings
from ..services.image_jobs import gpu_handoff_status


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _service_root(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value[:-3] if value.endswith("/v1") else value


def check_llamacpp(settings: Settings) -> dict[str, Any]:
    """执行一次短超时健康检查；失败只返回脱敏、可操作的信息。"""

    model_path = Path(settings.llama_model_path)
    configured = model_path.is_file()
    result: dict[str, Any] = {
        "provider_id": "llamacpp",
        "display_name": "本地 Qwen",
        "available": False,
        "configured": configured,
        "model_id": settings.llama_model_id,
        "source_type": "LOCAL_MODEL",
        "server_version": None,
        "detail": None,
    }
    if not configured:
        result["detail"] = "GGUF 模型文件不存在，请先完成 M3 模型准备。"
        return result

    root = _service_root(settings.llama_server_base_url)
    try:
        with httpx.Client(timeout=settings.llama_health_timeout_seconds) as client:
            health = client.get(f"{root}/health")
            health.raise_for_status()
            health_payload = health.json()
            models = client.get(f"{root}/v1/models")
            models.raise_for_status()
            models_payload = models.json()
            props = client.get(f"{root}/props")
            props.raise_for_status()
            props_payload = props.json()
        model_items = models_payload.get("data") if isinstance(models_payload, dict) else None
        server_version = (
            props_payload.get("build_info")
            if isinstance(props_payload, dict)
            else None
        )
        healthy = (
            isinstance(health_payload, dict)
            and health_payload.get("status") == "ok"
            and isinstance(model_items, list)
            and bool(model_items)
            and isinstance(server_version, str)
            and bool(server_version.strip())
        )
        if not healthy:
            result["detail"] = "llama.cpp 响应可达，但健康状态或模型列表无效。"
            return result
        actual_ids = [
            item.get("id")
            for item in model_items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if settings.llama_model_id not in actual_ids:
            result["detail"] = (
                "llama.cpp 已在线，但加载的模型 ID 与 LLAMA_MODEL_ID 不一致。"
            )
            return result
        result["available"] = True
        result["server_version"] = server_version.strip()
        result["detail"] = (
            f"服务在线；已加载：{', '.join(actual_ids)}"
            if actual_ids
            else "服务在线。"
        )
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        result["detail"] = (
            "本地 Qwen 服务离线，请在项目根目录运行 "
            ".\\scripts\\run_llm_server.ps1。"
        )
        return result


def provider_registry(settings: Settings) -> dict[str, Any]:
    image_status = check_comfyui_image(settings)
    return {
        "default_script_provider": settings.script_provider,
        "checked_at": utc_now(),
        "providers": [
            {
                "provider_id": "mock",
                "display_name": "Mock 离线",
                "available": True,
                "configured": True,
                "model_id": "mock-script.v1",
                "source_type": "MOCK",
                "server_version": None,
                "detail": "无需网络、API Key 或模型权重。",
            },
            check_llamacpp(settings),
        ],
        "default_image_provider": settings.image_provider,
        "image_providers": [
            {
                "provider_id": "mock",
                "display_name": "Mock 视觉",
                "available": True,
                "configured": True,
                "model_id": "deterministic-ffmpeg-visual",
                "source_type": "MOCK",
                "detail": "现有确定性 FFmpeg 几何画面保底。",
                "requires_gpu_handoff": False,
            },
            image_status,
        ],
    }


def check_comfyui_image(settings: Settings) -> dict[str, Any]:
    """报告按 Job 启动的真实 ImageProvider 是否已配置，而非要求 8188 常驻。"""

    comfy_root = Path(settings.comfyui_root)
    comfy_python = Path(settings.comfyui_python)
    model_path = Path(settings.comfyui_model_path)
    configured = (
        comfy_root.joinpath("main.py").is_file()
        and comfy_python.is_file()
        and model_path.is_file()
    )
    commit_matches = False
    if configured:
        try:
            completed = subprocess.run(
                ["git", "-C", str(comfy_root), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                shell=False,
            )
            commit_matches = (
                completed.returncode == 0
                and completed.stdout.strip() == settings.comfyui_expected_commit
            )
        except (OSError, subprocess.TimeoutExpired):
            commit_matches = False
    handoff = gpu_handoff_status()
    ready = configured and commit_matches and not handoff["conflict"]
    if not configured:
        detail = "ComfyUI、独立 Python 环境或 Animagine 模型文件缺失。"
    elif not commit_matches:
        detail = "ComfyUI commit 与 M4-A 已验证版本不一致。"
    elif handoff["conflict"]:
        detail = "需要先停止 Qwen 并释放 8081/显存，然后再启动真实图像任务。"
    else:
        detail = "M4-A 环境已就绪；ComfyUI 将由单个 Job 有界启动，不需要常驻。"
    return {
        "provider_id": "comfyui-animagine-xl-4",
        "display_name": "真实动漫视觉 · Animagine XL 4.0",
        "available": ready,
        "configured": configured and commit_matches,
        "model_id": settings.comfyui_model_id,
        "source_type": "LOCAL_MODEL",
        "detail": detail,
        "requires_gpu_handoff": handoff["conflict"],
    }

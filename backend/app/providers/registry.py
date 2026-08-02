"""M3 ScriptProvider 注册信息与本机 llama.cpp 健康检查。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings


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
    }

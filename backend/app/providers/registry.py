"""M3 ScriptProvider 注册信息与本机 llama.cpp 健康检查。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from ..config import Settings
from ..services.audio_jobs import audio_gpu_handoff_status
from ..services.image_jobs import gpu_handoff_status
from .llama_server import inspect_llama_server


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def check_llamacpp(settings: Settings) -> dict[str, Any]:
    """Report configuration separately from the optional running server."""

    model_path = Path(settings.llama_model_path)
    executable = Path(settings.llama_server_executable)
    configured = (
        model_path.is_file()
        and model_path.suffix.casefold() == ".gguf"
        and executable.is_file()
        and executable.suffix.casefold() == ".exe"
    )
    result: dict[str, Any] = {
        "provider_id": "llamacpp",
        "display_name": "本地 Qwen",
        "available": False,
        "configured": configured,
        "model_id": settings.llama_model_id,
        "source_type": "LOCAL_MODEL",
        "server_version": None,
        "runtime_state": "CONFIG_ERROR",
        "detail": None,
    }
    if not configured:
        missing = []
        if not executable.is_file() or executable.suffix.casefold() != ".exe":
            missing.append("llama-server.exe")
        if not model_path.is_file() or model_path.suffix.casefold() != ".gguf":
            missing.append("GGUF 模型")
        result["detail"] = f"本地 Qwen 配置不完整：缺少 {', '.join(missing)}。"
        return result

    try:
        inspection = inspect_llama_server(
            base_url=settings.llama_server_base_url,
            model_id=settings.llama_model_id,
            timeout_seconds=settings.llama_health_timeout_seconds,
        )
    except ValueError as exc:
        result["detail"] = f"本地 Qwen 地址配置无效：{exc}"
        return result
    result["runtime_state"] = inspection.state
    result["detail"] = inspection.detail
    result["server_version"] = inspection.server_version
    result["available"] = inspection.state in {"READY_TO_START", "ONLINE"}
    return result


def provider_registry(settings: Settings) -> dict[str, Any]:
    image_status = check_comfyui_image(settings)
    audio_status = check_qwen3_tts_audio(settings)
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
                "runtime_state": "NOT_APPLICABLE",
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
        "default_audio_provider": settings.audio_provider,
        "audio_providers": [
            {
                "provider_id": "mock",
                "display_name": "Mock 音频",
                "available": True,
                "configured": True,
                "model_id": "deterministic-pcm-wave",
                "source_type": "MOCK",
                "detail": "确定性提示音离线保底，不代表真实中文旁白。",
                "requires_gpu_handoff": False,
                "speakers": ["Serena", "Vivian"],
                "default_speaker": "Serena",
                "language": "Chinese",
            },
            audio_status,
        ],
        "default_video_provider": "none",
        "video_providers": [
            {
                "provider_id": "mock-video",
                "display_name": "Mock 动态视频",
                "available": True,
                "configured": True,
                "model_id": "deterministic-ffmpeg-keyframe-video",
                "source_type": "MOCK",
                "detail": "由已有关键帧确定性生成测试 MP4，不代表真实 AI 视频模型。",
                "requires_gpu_handoff": False,
            }
        ],
    }


def check_qwen3_tts_audio(settings: Settings) -> dict[str, Any]:
    """轻量检查固定环境与 revision；实际 Job 启动前才完整核对权重哈希。"""

    python_path = Path(settings.qwen_tts_python)
    runner_path = Path(settings.qwen_tts_runner)
    model_path = Path(settings.qwen_tts_model_path)
    root_weight = model_path / "model.safetensors"
    tokenizer_weight = model_path / "speech_tokenizer" / "model.safetensors"
    required = (
        python_path.is_file(),
        runner_path.is_file(),
        root_weight.is_file(),
        tokenizer_weight.is_file(),
    )
    configured = all(required)
    revision_ok = False
    if configured:
        metadata = sorted(model_path.rglob("*.metadata"))
        revisions: set[str] = set()
        try:
            for path in metadata:
                with path.open("r", encoding="utf-8") as handle:
                    revisions.add(handle.readline().strip())
            revision_ok = bool(metadata) and revisions == {
                settings.qwen_tts_model_revision
            }
        except OSError:
            revision_ok = False
    handoff_status = audio_gpu_handoff_status(settings)
    handoff = bool(handoff_status["conflict"])
    available = configured and revision_ok and not handoff
    if not configured:
        detail = "独立 Qwen3-TTS 环境、运行器或固定模型文件缺失。"
    elif not revision_ok:
        detail = "本地模型下载 metadata 与固定 Hugging Face revision 不一致。"
    elif handoff:
        detail = "需要先停止 Qwen/ComfyUI 并释放 GPU；平台不会结束外部进程。"
    else:
        detail = "固定本地模型已就绪；真实旁白由一次性离线子进程生成。"
    return {
        "provider_id": "qwen3-tts-0.6b-customvoice",
        "display_name": "真实 AI 旁白 · Qwen3-TTS 0.6B",
        "available": available,
        "configured": configured and revision_ok,
        "model_id": settings.qwen_tts_model_id,
        "source_type": "LOCAL_MODEL",
        "detail": detail,
        "requires_gpu_handoff": handoff,
        "speakers": ["Serena", "Vivian"],
        "default_speaker": settings.qwen_tts_default_speaker,
        "language": settings.qwen_tts_language,
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

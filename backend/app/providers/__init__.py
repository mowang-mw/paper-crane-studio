"""M2/M3 Provider 接口、离线 Mock 与 llama-server 文本实现。"""

from .base import (
    AudioProvider,
    GeneratedImageAsset,
    ImageGenerationOptions,
    ImageGenerationRequest,
    ImageProvider,
    ScriptProvider,
    GeneratedVideoAsset,
    VideoGenerationOptions,
    VideoGenerationRequest,
    VideoProvider,
)
from .comfyui import ComfyUIImageProvider, ComfyUIJobSession, ImageProviderError
from .llama_cpp import (
    LlamaCppOutputError,
    LlamaCppProtocolError,
    LlamaCppProviderError,
    LlamaCppScriptProvider,
    LlamaCppTransportError,
)
from .mock import MockAudioProvider, MockImageProvider, MockScriptProvider, MockVideoProvider

__all__ = [
    "AudioProvider",
    "ComfyUIImageProvider",
    "ComfyUIJobSession",
    "GeneratedImageAsset",
    "ImageGenerationOptions",
    "ImageGenerationRequest",
    "ImageProvider",
    "ImageProviderError",
    "LlamaCppOutputError",
    "LlamaCppProtocolError",
    "LlamaCppProviderError",
    "LlamaCppScriptProvider",
    "LlamaCppTransportError",
    "MockAudioProvider",
    "MockImageProvider",
    "MockScriptProvider",
    "MockVideoProvider",
    "ScriptProvider",
    "GeneratedVideoAsset",
    "VideoGenerationOptions",
    "VideoGenerationRequest",
    "VideoProvider",
]

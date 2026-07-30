"""M2 Provider 接口及离线 Mock 实现。"""

from .base import AudioProvider, ImageProvider, ScriptProvider
from .mock import MockAudioProvider, MockImageProvider, MockScriptProvider

__all__ = [
    "AudioProvider",
    "ImageProvider",
    "MockAudioProvider",
    "MockImageProvider",
    "MockScriptProvider",
    "ScriptProvider",
]

"""M0—M4 可复用媒体与烧录字幕工具。"""

from .ffmpeg import (
    MediaToolError,
    decode_media_fully,
    extract_shot_midpoint_frames,
    media_duration_tolerance_seconds,
    resolve_media_tools,
    validate_planned_encoded_duration,
    verify_media,
)
from .mock_pipeline import (
    generate_m0_smoke,
    generate_m1_short,
    render_image_project_short,
    render_mock_project_short,
    resume_mock_project_short,
)
from .subtitles import (
    BurnedSubtitle,
    prepare_burned_subtitle,
    wrap_subtitle_text,
)

__all__ = [
    "BurnedSubtitle",
    "MediaToolError",
    "decode_media_fully",
    "extract_shot_midpoint_frames",
    "generate_m0_smoke",
    "generate_m1_short",
    "media_duration_tolerance_seconds",
    "prepare_burned_subtitle",
    "render_image_project_short",
    "render_mock_project_short",
    "resume_mock_project_short",
    "resolve_media_tools",
    "verify_media",
    "validate_planned_encoded_duration",
    "wrap_subtitle_text",
]

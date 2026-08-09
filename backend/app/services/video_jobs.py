"""Constants and structured failures for the optional video-provider stage."""

from __future__ import annotations

from typing import Any


VIDEO_JOB_TYPE = "GENERATE_VIDEO"
VIDEO_PROVIDER_ID = "mock-video"
VIDEO_SOURCE_TYPE = "MOCK"


class VideoJobError(RuntimeError):
    def __init__(self, code: str, summary: str, *, retryable: bool = True) -> None:
        super().__init__(summary)
        self.generation_error: dict[str, Any] = {
            "code": code,
            "stage": "VIDEO_GENERATION",
            "summary": summary,
            "retryable": retryable,
            "provider_id": VIDEO_PROVIDER_ID,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
        }

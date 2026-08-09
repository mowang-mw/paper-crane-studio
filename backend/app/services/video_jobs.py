"""Constants and structured failures for the optional video-provider stage."""

from __future__ import annotations

from typing import Any


VIDEO_JOB_TYPE = "GENERATE_VIDEO"
MOCK_VIDEO_PROVIDER_ID = "mock-video"
CLOUD_WAN_VIDEO_PROVIDER_ID = "cloud-wan-2.7"
VIDEO_PROVIDER_IDS = frozenset({MOCK_VIDEO_PROVIDER_ID, CLOUD_WAN_VIDEO_PROVIDER_ID})
VIDEO_PROVIDER_ID = MOCK_VIDEO_PROVIDER_ID
VIDEO_SOURCE_TYPE = "MOCK"


class VideoJobError(RuntimeError):
    def __init__(
        self,
        code: str,
        summary: str,
        *,
        retryable: bool = True,
        provider_id: str = VIDEO_PROVIDER_ID,
    ) -> None:
        super().__init__(summary)
        self.generation_error: dict[str, Any] = {
            "code": code,
            "stage": "VIDEO_GENERATION",
            "summary": summary,
            "retryable": retryable,
            "provider_id": provider_id,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
        }

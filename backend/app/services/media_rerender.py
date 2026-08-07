"""Media-only rerender job contract and structured failures."""

from __future__ import annotations

from typing import Any


MEDIA_RERENDER_JOB_TYPE = "MEDIA_RERENDER"
MEDIA_RERENDER_PROVIDER_ID = "ffmpeg"


def media_rerender_error_payload(
    code: str,
    summary: str,
    *,
    stage: str = "SOURCE_REUSE",
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "stage": stage,
        "summary": summary,
        "retryable": retryable,
        "provider_id": MEDIA_RERENDER_PROVIDER_ID,
        "media_only": True,
        "suggestions": [],
        "first_attempt_errors": [],
        "repair_attempt_errors": [],
    }


class MediaRerenderJobError(RuntimeError):
    def __init__(
        self,
        code: str,
        summary: str,
        *,
        stage: str = "SOURCE_REUSE",
        retryable: bool = False,
    ) -> None:
        super().__init__(summary)
        self.generation_error = media_rerender_error_payload(
            code,
            summary,
            stage=stage,
            retryable=retryable,
        )


__all__ = [
    "MEDIA_RERENDER_JOB_TYPE",
    "MEDIA_RERENDER_PROVIDER_ID",
    "MediaRerenderJobError",
    "media_rerender_error_payload",
]

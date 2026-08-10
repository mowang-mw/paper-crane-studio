"""Alibaba Cloud Model Studio Wan 2.7 image-to-video provider."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from ..media.ffmpeg import (
    MediaToolError,
    ffprobe_json,
    resolve_media_tools,
    run_command,
    sha256_file,
)
from .base import (
    GeneratedVideoAsset,
    ScriptShot,
    VideoGenerationRequest,
    VideoPlan,
    VideoProvider,
)


CLOUD_WAN_PROVIDER_ID = "cloud-wan-2.7"
CLOUD_WAN_MODEL_ID = "wan2.7-i2v-2026-04-25"
CLOUD_WAN_SOURCE_TYPE = "REAL_CLOUD_MODEL"
SUPPORTED_TASK_STATES = {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED"}
SUPPORTED_VIDEO_CODECS = {"h264", "hevc", "vp9", "av1", "mpeg4"}
MIN_FIRST_FRAME_SIDE = 256
MAX_FIRST_FRAME_SIDE = 4_096
MAX_FIRST_FRAME_PIXELS = 16_777_216
ALPHA_PIXEL_FORMATS = {
    "rgba",
    "rgba64be",
    "rgba64le",
    "bgra",
    "argb",
    "abgr",
    "ya8",
    "ya16be",
    "ya16le",
    "pal8",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class CloudWanVideoProviderError(RuntimeError):
    def __init__(self, code: str, summary: str, *, retryable: bool = False) -> None:
        super().__init__(summary)
        self.generation_error = {
            "code": code,
            "stage": "VIDEO_GENERATION",
            "summary": summary,
            "retryable": retryable,
            "provider_id": CLOUD_WAN_PROVIDER_ID,
            "model_id": CLOUD_WAN_MODEL_ID,
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
        }


ProbeFunction = Callable[[Path], dict[str, Any]]
ImageDecodeFunction = Callable[[Path], None]
ImageNormalizeFunction = Callable[[Path, Path], None]


class CloudWanVideoProvider(VideoProvider):
    provider_id = CLOUD_WAN_PROVIDER_ID
    source_type = CLOUD_WAN_SOURCE_TYPE

    def __init__(
        self,
        *,
        api_key: str | None,
        workspace_id: str | None,
        region: str = "beijing",
        model_id: str = CLOUD_WAN_MODEL_ID,
        poll_interval_seconds: float = 15.0,
        overall_timeout_seconds: float = 1_800.0,
        http_timeout_seconds: float = 30.0,
        max_source_image_bytes: int = 10 * 1024 * 1024,
        client: httpx.Client | None = None,
        probe: ProbeFunction | None = None,
        image_probe: ProbeFunction | None = None,
        image_decode: ImageDecodeFunction | None = None,
        image_normalize: ImageNormalizeFunction | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.workspace_id = (workspace_id or "").strip() or None
        self.region = region.strip().lower()
        self.model_id = model_id
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.overall_timeout_seconds = float(overall_timeout_seconds)
        self.http_timeout_seconds = float(http_timeout_seconds)
        self.max_source_image_bytes = int(max_source_image_bytes)
        self.client = client
        self.probe = probe
        self.image_probe = image_probe
        self.image_decode = image_decode
        self.image_normalize = image_normalize
        self.sleep = sleep
        self.clock = clock
        if self.region != "beijing":
            raise CloudWanVideoProviderError(
                "WAN_CONFIG_ERROR", "CloudWanVideoProvider 当前只支持华北 2（北京）。"
            )
        if self.workspace_id is not None and not re.fullmatch(
            r"[A-Za-z0-9-]+", self.workspace_id
        ):
            raise CloudWanVideoProviderError(
                "WAN_CONFIG_ERROR", "DASHSCOPE_WORKSPACE_ID 不是安全的 DNS 标签。"
            )
        if any(
            value <= 0
            for value in (
                self.poll_interval_seconds,
                self.overall_timeout_seconds,
                self.http_timeout_seconds,
                self.max_source_image_bytes,
            )
        ):
            raise CloudWanVideoProviderError(
                "WAN_CONFIG_ERROR", "Wan HTTP 超时、总超时、轮询间隔和图片上限必须大于 0。"
            )

    @property
    def endpoint(self) -> str:
        self._require_config()
        return f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com"

    def _require_config(self) -> None:
        if not self.api_key or not self.workspace_id:
            raise CloudWanVideoProviderError(
                "WAN_CONFIG_ERROR",
                "缺少 DASHSCOPE_API_KEY 或 DASHSCOPE_WORKSPACE_ID。",
            )

    def plan(self, *, shot: ScriptShot) -> VideoPlan:
        return VideoPlan(
            provider_id=self.provider_id,
            source_type=self.source_type,
            parameters={
                "model_id": self.model_id,
                "mode": "first-frame-to-video",
                "resolution": "720P",
                "duration_seconds": 5,
                "prompt_extend": True,
                "watermark": False,
                "shot_index": shot.shot_index,
            },
        )

    @staticmethod
    def _image_file_info(path: Path, max_bytes: int) -> tuple[str, int]:
        if not path.is_file():
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID", "来源关键帧文件不存在。"
            )
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID",
                f"来源关键帧必须是非空文件且不超过 {max_bytes} 字节。",
            )
        prefix = path.read_bytes()[:16]
        suffix = path.suffix.lower()
        if suffix == ".png" and prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = "image/png"
        elif suffix in {".jpg", ".jpeg"} and prefix.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        else:
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID", "Wan 首帧当前只接受可识别的 PNG 或 JPEG。"
            )
        return mime_type, size

    @classmethod
    def _image_data_url(cls, path: Path, max_bytes: int) -> tuple[str, str, int]:
        mime_type, size = cls._image_file_info(path, max_bytes)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}", mime_type, size

    @staticmethod
    def _has_alpha(pixel_format: str) -> bool:
        normalized = pixel_format.strip().lower()
        return (
            normalized in ALPHA_PIXEL_FORMATS
            or normalized.startswith("yuva")
            or normalized.startswith("gbrap")
        )

    def _default_image_decode(self, path: Path) -> None:
        tools = resolve_media_tools()
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=60,
        )

    def _default_image_normalize(self, source: Path, destination: Path) -> None:
        tools = resolve_media_tools()
        run_command(
            [
                tools.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "format=rgb24",
                "-c:v",
                "png",
                destination,
            ],
            timeout_seconds=60,
        )

    def _inspect_first_frame(self, path: Path) -> dict[str, Any]:
        try:
            payload = (
                self.image_probe(path)
                if self.image_probe
                else ffprobe_json(resolve_media_tools(), path)
            )
            streams = payload.get("streams")
            if not isinstance(streams, list):
                raise ValueError("missing streams")
            videos = [item for item in streams if item.get("codec_type") == "video"]
            if len(videos) != 1:
                raise ValueError("expected exactly one image stream")
            video = videos[0]
            codec = str(video.get("codec_name") or "")
            width = int(video["width"])
            height = int(video["height"])
            pixel_format = str(video.get("pix_fmt") or "")
            expected_codec = "png" if path.suffix.lower() == ".png" else "mjpeg"
            if codec != expected_codec:
                raise ValueError("codec does not match file type")
            if (
                not MIN_FIRST_FRAME_SIDE <= width <= MAX_FIRST_FRAME_SIDE
                or not MIN_FIRST_FRAME_SIDE <= height <= MAX_FIRST_FRAME_SIDE
                or width * height > MAX_FIRST_FRAME_PIXELS
            ):
                raise ValueError("image dimensions are outside local safety bounds")
            if not pixel_format:
                raise ValueError("missing pixel format")
            if self.image_decode:
                self.image_decode(path)
            else:
                self._default_image_decode(path)
        except (KeyError, TypeError, ValueError, OSError, MediaToolError) as exc:
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID",
                "Wan 首帧无法解码、格式不匹配或尺寸超出 256—4096 像素安全边界。",
            ) from exc
        return {
            "codec": codec,
            "width": width,
            "height": height,
            "pixel_format": pixel_format,
            "has_alpha": self._has_alpha(pixel_format),
        }

    def _prepare_first_frame(
        self, source: Path, output_dir: Path
    ) -> tuple[Path, dict[str, Any]]:
        original = self._inspect_first_frame(source)
        if not original["has_alpha"]:
            return source, {
                "input_normalized": False,
                "original": original,
                "submitted": original,
            }
        normalized = output_dir / f"{source.stem}.wan-rgb.png"
        normalized.unlink(missing_ok=True)
        try:
            if self.image_normalize:
                self.image_normalize(source, normalized)
            else:
                self._default_image_normalize(source, normalized)
            if not normalized.is_file() or normalized.stat().st_size <= 0:
                raise ValueError("normalizer produced no output")
            submitted = self._inspect_first_frame(normalized)
            if submitted["has_alpha"]:
                raise ValueError("normalized image still has alpha")
            if (submitted["width"], submitted["height"]) != (
                original["width"],
                original["height"],
            ):
                raise ValueError("normalization changed image dimensions")
        except (ValueError, OSError, MediaToolError, CloudWanVideoProviderError) as exc:
            normalized.unlink(missing_ok=True)
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID", "透明首帧无法安全规范化为 RGB PNG。"
            ) from exc
        return normalized, {
            "input_normalized": True,
            "original": original,
            "submitted": submitted,
        }

    @staticmethod
    def _motion_prompt(request: VideoGenerationRequest) -> str:
        if request.motion_prompt and request.motion_prompt.strip():
            return "\n".join(
                [
                    request.motion_prompt.strip(),
                    (
                        "Create subtle character and environmental movement with cinematic natural "
                        "motion. Preserve character appearance, key objects, spatial relationships "
                        "and the original composition. Avoid sudden scene changes."
                    ),
                ]
            )
        parts = [
            request.shot.visual_description.strip(),
            request.prompt.strip(),
            request.motion_description.strip(),
            (
                "Create subtle character and environmental movement with cinematic natural "
                "motion. Preserve character appearance, key objects, spatial relationships "
                "and the original composition. Avoid sudden scene changes."
            ),
        ]
        return "\n".join(dict.fromkeys(part for part in parts if part))

    @staticmethod
    def _response_json(response: httpx.Response, code: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # Do not chain the remote response body into Worker traceback/logs.
            raise CloudWanVideoProviderError(code, "Wan API 返回了无效 JSON。") from None
        if not isinstance(payload, dict):
            raise CloudWanVideoProviderError(code, "Wan API JSON 顶层结构无效。")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            code = "WAN_AUTH_FAILED"
            summary = "Wan API 鉴权失败，请检查后端 API Key 与 Workspace 配置。"
        elif response.status_code == 429:
            code = "WAN_RATE_LIMITED"
            summary = "Wan API 已限流；本次 Job 明确失败，未进行无限重试。"
        else:
            code = "WAN_HTTP_ERROR"
            summary = f"Wan API HTTP 请求失败（状态码 {response.status_code}）。"
        raise CloudWanVideoProviderError(code, summary, retryable=response.status_code >= 500)

    def _post_task(self, client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.post(
                f"{self.endpoint}/api/v1/services/aigc/video-generation/video-synthesis",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                },
                json=body,
                timeout=self.http_timeout_seconds,
            )
        except httpx.HTTPError:
            # Suppress the request object so headers/body cannot reach traceback logs.
            raise CloudWanVideoProviderError(
                "WAN_NETWORK_ERROR", "无法连接 Wan API。", retryable=True
            ) from None
        self._raise_for_status(response)
        return self._response_json(response, "WAN_TASK_RESPONSE_INVALID")

    def _poll_task(self, client: httpx.Client, task_id: str) -> dict[str, Any]:
        try:
            response = client.get(
                f"{self.endpoint}/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.http_timeout_seconds,
            )
        except httpx.HTTPError:
            raise CloudWanVideoProviderError(
                "WAN_NETWORK_ERROR", "查询 Wan 云任务失败。", retryable=True
            ) from None
        self._raise_for_status(response)
        return self._response_json(response, "WAN_TASK_RESPONSE_INVALID")

    @staticmethod
    def _task_output(payload: dict[str, Any]) -> dict[str, Any]:
        output = payload.get("output")
        if not isinstance(output, dict):
            raise CloudWanVideoProviderError(
                "WAN_TASK_RESPONSE_INVALID", "Wan 任务响应缺少 output。"
            )
        return output

    @staticmethod
    def _task_status(output: dict[str, Any]) -> str:
        status = str(output.get("task_status") or "").upper()
        if status not in SUPPORTED_TASK_STATES:
            raise CloudWanVideoProviderError(
                "WAN_TASK_UNKNOWN", f"Wan 返回未知任务状态：{status or '<empty>'}。"
            )
        return status

    @staticmethod
    def _video_url(output: dict[str, Any]) -> str:
        direct = output.get("video_url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        results = output.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    value = item.get("url") or item.get("video_url")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        raise CloudWanVideoProviderError(
            "WAN_VIDEO_URL_MISSING", "Wan 任务成功，但响应中缺少 video_url。"
        )

    @staticmethod
    def _validate_download_url(value: str) -> None:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not hostname
            or not (hostname == "aliyuncs.com" or hostname.endswith(".aliyuncs.com"))
            or parsed.username
            or parsed.password
        ):
            raise CloudWanVideoProviderError(
                "WAN_VIDEO_DOWNLOAD_FAILED", "Wan 临时视频 URL 不符合安全下载边界。"
            )

    def _download_video(self, client: httpx.Client, url: str, output_path: Path) -> None:
        self._validate_download_url(url)
        partial = output_path.with_suffix(".part.mp4")
        partial.unlink(missing_ok=True)
        try:
            with client.stream(
                "GET", url, timeout=self.http_timeout_seconds, follow_redirects=False
            ) as response:
                self._raise_for_status(response)
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except CloudWanVideoProviderError as exc:
            partial.unlink(missing_ok=True)
            raise CloudWanVideoProviderError(
                "WAN_VIDEO_DOWNLOAD_FAILED",
                "Wan 临时视频 URL 下载失败。",
                retryable=bool(exc.generation_error.get("retryable")),
            ) from None
        except (httpx.HTTPError, OSError):
            partial.unlink(missing_ok=True)
            raise CloudWanVideoProviderError(
                "WAN_VIDEO_DOWNLOAD_FAILED", "Wan 视频下载失败。", retryable=True
            ) from None
        if not partial.is_file() or partial.stat().st_size <= 0:
            partial.unlink(missing_ok=True)
            raise CloudWanVideoProviderError(
                "WAN_VIDEO_DOWNLOAD_FAILED", "Wan 视频下载结果为空。"
            )
        partial.replace(output_path)

    def _probe_video(self, path: Path) -> dict[str, Any]:
        try:
            payload = self.probe(path) if self.probe else ffprobe_json(resolve_media_tools(), path)
            streams = payload.get("streams")
            if not isinstance(streams, list):
                raise ValueError("missing streams")
            videos = [item for item in streams if item.get("codec_type") == "video"]
            audios = [item for item in streams if item.get("codec_type") == "audio"]
            if not videos:
                raise ValueError("missing video stream")
            video = videos[0]
            width = int(video["width"])
            height = int(video["height"])
            codec = str(video["codec_name"])
            fps_value = video.get("avg_frame_rate") or video.get("r_frame_rate")
            fps = float(Fraction(str(fps_value)))
            format_payload = payload.get("format")
            if not isinstance(format_payload, dict):
                raise ValueError("missing format")
            duration = float(format_payload["duration"])
            if (
                width <= 0
                or height <= 0
                or codec not in SUPPORTED_VIDEO_CODECS
                or not 0 < fps <= 120
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError("invalid video metadata")
            file_size_bytes = path.stat().st_size
            if file_size_bytes <= 0:
                raise ValueError("empty video file")
        except (
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            OSError,
            MediaToolError,
        ) as exc:
            raise CloudWanVideoProviderError(
                "WAN_VIDEO_INVALID", "下载结果未通过 ffprobe 视频校验。"
            ) from exc
        return {
            "ffprobe_ok": True,
            "duration_seconds": round(duration, 6),
            "width": width,
            "height": height,
            "fps": round(fps, 6),
            "video_codec": codec,
            "video_stream_count": len(videos),
            "audio_stream_count": len(audios),
            "contains_audio_stream": bool(audios),
            "file_size_bytes": file_size_bytes,
        }

    def generate(self, *, request: VideoGenerationRequest) -> GeneratedVideoAsset:
        self._require_config()
        duration = request.options.duration_seconds
        if not float(duration).is_integer() or not 2 <= int(duration) <= 15:
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID", "Wan 2.7 duration 必须是 2—15 秒整数。"
            )
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = request.output_dir / f"{request.shot.provider_shot_id}.mp4"
        trace_path = request.output_dir / f"{request.shot.provider_shot_id}.video-trace.json"
        project_root = request.output_dir.parents[2]
        try:
            source_relative = request.source_image_path.relative_to(project_root).as_posix()
        except ValueError as exc:
            raise CloudWanVideoProviderError(
                "WAN_INPUT_INVALID", "来源关键帧不在当前项目目录内。"
            ) from exc
        # Reject oversized or mislabeled files before ffprobe/ffmpeg touches them.
        self._image_file_info(
            request.source_image_path, self.max_source_image_bytes
        )
        source_sha256 = sha256_file(request.source_image_path)
        prepared_image_path, image_preflight = self._prepare_first_frame(
            request.source_image_path, request.output_dir
        )
        trace: dict[str, Any] = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "source_type": self.source_type,
            "project_id": request.project_id,
            "shot_id": request.shot.provider_shot_id,
            "source_image_path": source_relative,
            "source_image_sha256": source_sha256,
            "input_normalized": image_preflight["input_normalized"],
            "original_image_probe": image_preflight["original"],
            "submitted_image_probe": image_preflight["submitted"],
            "prompt": self._motion_prompt(request),
            "motion_description": request.motion_description,
            "requested_resolution": "720P",
            "requested_duration_seconds": int(duration),
            "prompt_extend": True,
            "watermark": False,
            "seed": None,
            "cloud_task_id": None,
            "create_task_request_count": 0,
            "request_id": None,
            "cloud_status_progression": [],
            "submitted_at_utc": None,
            "generation_elapsed_seconds": None,
            "poll_elapsed_seconds": None,
            "remote_output_downloaded": False,
            "downloaded_local_path": None,
            "downloaded_sha256": None,
            "ffprobe": None,
            "contains_audio_stream": None,
            "ai_video_generated": True,
            "success": False,
        }
        started = self.clock()
        poll_started = started
        try:
            data_url, image_mime_type, image_size = self._image_data_url(
                prepared_image_path, self.max_source_image_bytes
            )
        finally:
            if prepared_image_path != request.source_image_path:
                try:
                    prepared_image_path.unlink(missing_ok=True)
                except OSError:
                    raise CloudWanVideoProviderError(
                        "WAN_INPUT_INVALID",
                        "透明首帧派生 RGB 输入无法安全清理；未创建云任务。",
                    ) from None
        trace["submitted_image_mime_type"] = image_mime_type
        trace["submitted_image_size_bytes"] = image_size
        body = {
            "model": self.model_id,
            "input": {
                "prompt": trace["prompt"],
                "media": [{"type": "first_frame", "url": data_url}],
            },
            "parameters": {
                "resolution": "720P",
                "duration": int(duration),
                "prompt_extend": True,
                "watermark": False,
            },
        }
        owns_client = self.client is None
        client = self.client or httpx.Client(follow_redirects=False)
        try:
            trace["submitted_at_utc"] = _utc_now()
            trace["create_task_request_count"] = 1
            created = self._post_task(client, body)
            output = self._task_output(created)
            task_id = output.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise CloudWanVideoProviderError(
                    "WAN_TASK_RESPONSE_INVALID", "Wan 创建任务响应缺少 task_id。"
                )
            task_id = task_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
                raise CloudWanVideoProviderError(
                    "WAN_TASK_RESPONSE_INVALID", "Wan task_id 格式无效。"
                )
            trace["cloud_task_id"] = task_id
            request_id = created.get("request_id")
            trace["request_id"] = request_id if isinstance(request_id, str) else None
            status = self._task_status(output)
            trace["cloud_status_progression"].append(status)
            poll_started = self.clock()
            deadline = started + self.overall_timeout_seconds
            while status in {"PENDING", "RUNNING"}:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise CloudWanVideoProviderError(
                        "WAN_TIMEOUT", "Wan 云任务超过总等待时限。", retryable=True
                    )
                self.sleep(min(self.poll_interval_seconds, remaining))
                polled = self._poll_task(client, task_id)
                output = self._task_output(polled)
                status = self._task_status(output)
                if trace["cloud_status_progression"][-1] != status:
                    trace["cloud_status_progression"].append(status)
                poll_request_id = polled.get("request_id")
                if trace["request_id"] is None and isinstance(poll_request_id, str):
                    trace["request_id"] = poll_request_id
            if status == "FAILED":
                raise CloudWanVideoProviderError("WAN_TASK_FAILED", "Wan 云任务执行失败。")
            if status == "CANCELED":
                raise CloudWanVideoProviderError("WAN_TASK_CANCELED", "Wan 云任务已取消。")
            video_url = self._video_url(output)
            self._download_video(client, video_url, output_path)
            probe = self._probe_video(output_path)
            downloaded_sha256 = sha256_file(output_path)
            trace.update(
                {
                    "generation_elapsed_seconds": round(self.clock() - started, 6),
                    "poll_elapsed_seconds": round(self.clock() - poll_started, 6),
                    "remote_output_downloaded": True,
                    "downloaded_local_path": output_path.relative_to(project_root).as_posix(),
                    "downloaded_sha256": downloaded_sha256,
                    "ffprobe": probe,
                    "contains_audio_stream": probe["contains_audio_stream"],
                    "success": True,
                }
            )
            _atomic_json(trace_path, trace)
            metadata = {
                "model_id": self.model_id,
                "cloud_task_id": task_id,
                "create_task_request_count": trace["create_task_request_count"],
                "request_id": trace["request_id"],
                "cloud_status_progression": list(trace["cloud_status_progression"]),
                "source_image_sha256": source_sha256,
                "requested_resolution": "720P",
                "requested_duration_seconds": int(duration),
                "prompt_extend": True,
                "watermark": False,
                "ffprobe": probe,
                "contains_audio_stream": probe["contains_audio_stream"],
                "remote_output_downloaded": True,
                "ai_video_generated": True,
            }
            return GeneratedVideoAsset(
                provider_id=self.provider_id,
                shot_id=request.shot.provider_shot_id,
                video_path=output_path,
                duration_seconds=float(probe["duration_seconds"]),
                width=int(probe["width"]),
                height=int(probe["height"]),
                fps=int(round(float(probe["fps"]))),
                source_type=self.source_type,
                video_sha256=downloaded_sha256,
                trace_path=trace_path,
                metadata=metadata,
            )
        except CloudWanVideoProviderError as exc:
            output_path.unlink(missing_ok=True)
            trace.update(
                {
                    "generation_elapsed_seconds": round(self.clock() - started, 6),
                    "poll_elapsed_seconds": round(self.clock() - poll_started, 6),
                    "error_code": exc.generation_error["code"],
                    "error_summary": exc.generation_error["summary"],
                    "success": False,
                }
            )
            _atomic_json(trace_path, trace)
            exc.generation_error["trace_path"] = trace_path.relative_to(
                project_root
            ).as_posix()
            raise
        finally:
            if owns_client:
                client.close()

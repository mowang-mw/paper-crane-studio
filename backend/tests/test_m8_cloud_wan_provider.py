from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import traceback
from typing import Any, Callable

import httpx
import pytest

from backend.app.config import Settings
from backend.app import crud
from backend.app.database import Database
from backend.app.media.ffmpeg import MediaToolError
from backend.app.providers.base import (
    ScriptShot,
    VideoGenerationOptions,
    VideoGenerationRequest,
)
from backend.app.providers.cloud_wan import (
    CLOUD_WAN_MODEL_ID,
    CLOUD_WAN_PROVIDER_ID,
    CloudWanVideoProvider,
    CloudWanVideoProviderError,
)
from backend.app.providers.registry import provider_registry
from backend.app.services.video_jobs import VIDEO_JOB_TYPE
from backend.app.worker import Worker
from backend.app.main import create_app
from fastapi.testclient import TestClient
from backend.tests.test_m8_video_provider import _seed_source_image_job


API_KEY = "test-secret-key-never-persist"
WORKSPACE_ID = "workspace-test"
VIDEO_URL = "https://result-bucket.aliyuncs.com/output.mp4?signature=sensitive"


def _probe(_path: Path) -> dict[str, Any]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "24/1",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "5.000"},
    }


def _image_probe(_path: Path) -> dict[str, Any]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "png",
                "width": 1280,
                "height": 720,
                "pix_fmt": "rgb24",
            }
        ],
        "format": {},
    }


def _image_decode(_path: Path) -> None:
    return None


def _request(tmp_path: Path) -> VideoGenerationRequest:
    project_root = tmp_path / "project"
    source = project_root / "source" / "shot_01.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"source-image")
    return VideoGenerationRequest(
        project_id="project-1",
        job_id="job-1",
        shot=ScriptShot(
            provider_shot_id="shot_01",
            shot_index=1,
            title="镜头 1",
            visual_description="少女站在雨夜车站，保持当前构图。",
            narration="旁白",
            duration_seconds=7.0,
            camera="自然的小幅动作与雨丝运动",
            image_prompt="anime keyframe, one girl, station",
        ),
        source_image_path=source,
        prompt="anime keyframe, one girl, station",
        motion_description="自然的小幅动作与雨丝运动",
        output_dir=project_root / "jobs" / "job-1" / "video",
        options=VideoGenerationOptions(duration_seconds=5.0),
    )


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    probe: Callable[[Path], dict[str, Any]] = _probe,
    image_probe: Callable[[Path], dict[str, Any]] = _image_probe,
    image_decode: Callable[[Path], None] = _image_decode,
    image_normalize: Callable[[Path, Path], None] | None = None,
    max_source_image_bytes: int = 10 * 1024 * 1024,
    overall_timeout_seconds: float = 60.0,
    clock: Callable[[], float] | None = None,
) -> CloudWanVideoProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CloudWanVideoProvider(
        api_key=API_KEY,
        workspace_id=WORKSPACE_ID,
        client=client,
        probe=probe,
        image_probe=image_probe,
        image_decode=image_decode,
        image_normalize=image_normalize,
        max_source_image_bytes=max_source_image_bytes,
        poll_interval_seconds=1.0,
        overall_timeout_seconds=overall_timeout_seconds,
        http_timeout_seconds=5.0,
        sleep=lambda _seconds: None,
        **({"clock": clock} if clock is not None else {}),
    )


def _queue_cloud_worker_job(
    database: Database, settings: Settings
) -> tuple[str, str]:
    project_id, source_job_id = _seed_source_image_job(database, settings)
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        job = crud.create_job(
            session,
            project=project,
            provider_id=CLOUD_WAN_PROVIDER_ID,
            job_type=VIDEO_JOB_TYPE,
            request_json={
                "project_id": project_id,
                "source_image_job_id": source_job_id,
                "video_provider": CLOUD_WAN_PROVIDER_ID,
                "video_model_id": CLOUD_WAN_MODEL_ID,
                "video_options": {
                    "width": 1280,
                    "height": 720,
                    "fps": 24,
                    "duration_seconds": 5,
                    "motion_preset": "gentle_zoom",
                },
                "final_media_consumes_video": False,
                "fallback_media_path": "KEYFRAME_FFMPEG_MOTION",
            },
        )
        job_id = job.id
        session.commit()
    return project_id, job_id


def test_missing_cloud_config_is_unavailable(tmp_path: Path) -> None:
    settings = Settings.for_data_dir(tmp_path / "data")
    cloud = next(
        item
        for item in provider_registry(settings)["video_providers"]
        if item["provider_id"] == CLOUD_WAN_PROVIDER_ID
    )
    assert cloud["available"] is False
    assert cloud["configured"] is False
    assert cloud["runtime_state"] == "CONFIG_ERROR"
    assert "DASHSCOPE_API_KEY" in cloud["detail"]


def test_unconfigured_cloud_job_is_rejected_before_queue(
    client, database: Database, settings: Settings
) -> None:
    project_id, source_job_id = _seed_source_image_job(database, settings)
    response = client.post(
        f"/api/projects/{project_id}/render-video",
        json={
            "source_image_job_id": source_job_id,
            "video_provider": CLOUD_WAN_PROVIDER_ID,
            "duration_seconds": 5,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "WAN_CONFIG_ERROR"


def test_configured_cloud_job_snapshot_never_persists_credentials(
    database: Database, settings: Settings
) -> None:
    project_id, source_job_id = _seed_source_image_job(database, settings)
    configured = replace(
        settings,
        dashscope_api_key=API_KEY,
        dashscope_workspace_id=WORKSPACE_ID,
    )
    assert API_KEY not in repr(configured)
    with TestClient(create_app(configured, database=database)) as test_client:
        registry_text = test_client.get("/api/providers").text
        response = test_client.post(
            f"/api/projects/{project_id}/render-video",
            json={
                "source_image_job_id": source_job_id,
                "video_provider": CLOUD_WAN_PROVIDER_ID,
                "duration_seconds": 5,
            },
        )
        assert response.status_code == 202
        job = test_client.get(f"/api/jobs/{response.json()['job_id']}").json()
    snapshot_text = json.dumps(job["request_json"], ensure_ascii=False)
    assert job["request_json"]["video_provider"] == CLOUD_WAN_PROVIDER_ID
    assert job["request_json"]["video_model_id"] == CLOUD_WAN_MODEL_ID
    assert API_KEY not in snapshot_text
    assert WORKSPACE_ID not in snapshot_text
    assert API_KEY not in registry_text


def test_pending_running_succeeded_downloads_and_traces_without_secrets(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    polls = iter(("RUNNING", "SUCCEEDED"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == CLOUD_WAN_MODEL_ID
            assert body["input"]["media"][0]["type"] == "first_frame"
            assert body["input"]["media"][0]["url"].startswith("data:image/png;base64,")
            assert body["parameters"] == {
                "resolution": "720P",
                "duration": 5,
                "prompt_extend": True,
                "watermark": False,
            }
            assert request.headers["authorization"] == f"Bearer {API_KEY}"
            assert request.headers["x-dashscope-async"] == "enable"
            return httpx.Response(
                200,
                json={
                    "request_id": "request-create",
                    "output": {"task_id": "task-1", "task_status": "PENDING"},
                },
            )
        if request.url.path == "/api/v1/tasks/task-1":
            status = next(polls)
            output: dict[str, Any] = {"task_id": "task-1", "task_status": status}
            if status == "SUCCEEDED":
                output["video_url"] = VIDEO_URL
            return httpx.Response(200, json={"request_id": "request-poll", "output": output})
        if request.url.host == "result-bucket.aliyuncs.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"valid-cloud-mp4")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    request = _request(tmp_path)
    asset = _provider(handler).generate(request=request)
    assert asset.provider_id == CLOUD_WAN_PROVIDER_ID
    assert asset.source_type == "REAL_CLOUD_MODEL"
    assert asset.metadata["ai_video_generated"] is True
    assert asset.metadata["contains_audio_stream"] is True
    assert asset.metadata["cloud_status_progression"] == [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert asset.video_path.read_bytes() == b"valid-cloud-mp4"
    assert sum(item.method == "POST" for item in calls) == 1
    trace_text = asset.trace_path.read_text(encoding="utf-8")
    trace = json.loads(trace_text)
    assert trace["cloud_task_id"] == "task-1"
    assert trace["create_task_request_count"] == 1
    assert trace["request_id"] == "request-create"
    assert trace["source_image_path"] == "source/shot_01.png"
    assert trace["remote_output_downloaded"] is True
    assert trace["input_normalized"] is False
    assert trace["success"] is True
    assert API_KEY not in trace_text
    assert "Authorization" not in trace_text
    assert "data:image/png;base64" not in trace_text
    assert "signature=sensitive" not in trace_text


@pytest.mark.parametrize("alpha_pixel_format", ["rgba", "ya8", "pal8"])
def test_alpha_first_frame_is_normalized_and_derived_file_is_removed(
    tmp_path: Path, alpha_pixel_format: str,
) -> None:
    submitted_data_urls: list[str] = []

    def image_probe(path: Path) -> dict[str, Any]:
        payload = _image_probe(path)
        payload["streams"][0]["pix_fmt"] = (
            "rgb24" if path.name.endswith(".wan-rgb.png") else alpha_pixel_format
        )
        return payload

    def image_normalize(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"\x89PNG\r\n\x1a\n" + b"normalized-rgb")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            submitted_data_urls.append(body["input"]["media"][0]["url"])
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "task-alpha",
                        "task_status": "SUCCEEDED",
                        "video_url": VIDEO_URL,
                    }
                },
            )
        return httpx.Response(200, content=b"valid-cloud-mp4")

    request = _request(tmp_path)
    asset = _provider(
        handler,
        image_probe=image_probe,
        image_normalize=image_normalize,
    ).generate(request=request)

    trace = json.loads(asset.trace_path.read_text(encoding="utf-8"))
    assert trace["input_normalized"] is True
    assert trace["submitted_image_probe"]["pixel_format"] == "rgb24"
    assert "submitted_image_path" not in trace
    assert submitted_data_urls[0].startswith("data:image/png;base64,")
    assert not list(request.output_dir.glob("*.wan-rgb.png"))


def test_invalid_first_frame_fails_before_any_http_call(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(
            handler,
            image_probe=lambda _path: {"streams": [], "format": {}},
        ).generate(request=_request(tmp_path))

    assert caught.value.generation_error["code"] == "WAN_INPUT_INVALID"
    assert calls == 0


def test_oversized_first_frame_fails_before_probe_or_http(tmp_path: Path) -> None:
    image_probe_calls = 0
    http_calls = 0

    def image_probe(path: Path) -> dict[str, Any]:
        nonlocal image_probe_calls
        image_probe_calls += 1
        return _image_probe(path)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={})

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(
            handler,
            image_probe=image_probe,
            max_source_image_bytes=8,
        ).generate(request=_request(tmp_path))

    assert caught.value.generation_error["code"] == "WAN_INPUT_INVALID"
    assert image_probe_calls == 0
    assert http_calls == 0


def test_invalid_first_frame_dimensions_fail_before_http(tmp_path: Path) -> None:
    http_calls = 0

    def image_probe(path: Path) -> dict[str, Any]:
        payload = _image_probe(path)
        payload["streams"][0]["width"] = 128
        return payload

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={})

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler, image_probe=image_probe).generate(request=_request(tmp_path))

    assert caught.value.generation_error["code"] == "WAN_INPUT_INVALID"
    assert http_calls == 0


@pytest.mark.parametrize(
    ("cloud_status", "expected_code"),
    [
        ("FAILED", "WAN_TASK_FAILED"),
        ("CANCELED", "WAN_TASK_CANCELED"),
        ("UNKNOWN", "WAN_TASK_UNKNOWN"),
    ],
)
def test_terminal_and_unknown_cloud_states_fail(
    tmp_path: Path, cloud_status: str, expected_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"task_id": "task-state", "task_status": "PENDING"}},
            )
        return httpx.Response(
            200, json={"output": {"task_id": "task-state", "task_status": cloud_status}}
        )

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == expected_code


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (401, "WAN_AUTH_FAILED"),
        (403, "WAN_AUTH_FAILED"),
        (429, "WAN_RATE_LIMITED"),
        (500, "WAN_HTTP_ERROR"),
    ],
)
def test_http_auth_and_rate_limit_fail_without_retry(
    tmp_path: Path, status_code: int, code: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"code": "ignored"})

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == code
    assert calls == 1


def test_create_network_failure_is_not_retried_or_leaked(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(
            f"synthetic failure containing {API_KEY}", request=request
        )

    request = _request(tmp_path)
    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=request)

    assert caught.value.generation_error["code"] == "WAN_NETWORK_ERROR"
    assert calls == 1
    rendered = "".join(traceback.format_exception(caught.value))
    assert API_KEY not in rendered
    trace_text = next(request.output_dir.glob("*.video-trace.json")).read_text(
        encoding="utf-8"
    )
    assert json.loads(trace_text)["create_task_request_count"] == 1
    assert API_KEY not in trace_text
    assert "data:image/png;base64" not in trace_text


def test_poll_timeout_fails(tmp_path: Path) -> None:
    values = iter((0.0, 2.0, 2.0, 2.0, 2.0, 2.0))
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "POST":
            post_calls += 1
        return httpx.Response(
            200, json={"output": {"task_id": "task-timeout", "task_status": "PENDING"}}
        )

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(
            handler,
            overall_timeout_seconds=1.0,
            clock=lambda: next(values),
        ).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == "WAN_TIMEOUT"
    assert post_calls == 1


def test_success_without_video_url_fails(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"output": {"task_id": "task-no-url", "task_status": "SUCCEEDED"}}
        )

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == "WAN_VIDEO_URL_MISSING"


def test_video_download_failure_is_explicit(tmp_path: Path) -> None:
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "POST":
            post_calls += 1
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "task-download",
                        "task_status": "SUCCEEDED",
                        "video_url": VIDEO_URL,
                    }
                },
            )
        return httpx.Response(503, text="unavailable")

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == "WAN_VIDEO_DOWNLOAD_FAILED"
    assert post_calls == 1


def test_download_error_does_not_leak_signed_url_in_traceback(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "task-download-secret",
                        "task_status": "SUCCEEDED",
                        "video_url": VIDEO_URL,
                    }
                },
            )
        raise httpx.ConnectError(
            f"synthetic download failure for {request.url}", request=request
        )

    request = _request(tmp_path)
    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(handler).generate(request=request)

    rendered = "".join(traceback.format_exception(caught.value))
    trace_text = next(request.output_dir.glob("*.video-trace.json")).read_text(
        encoding="utf-8"
    )
    assert caught.value.generation_error["code"] == "WAN_VIDEO_DOWNLOAD_FAILED"
    assert "signature=sensitive" not in rendered
    assert "signature=sensitive" not in trace_text


def test_non_video_download_fails_ffprobe_validation(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "task-invalid",
                        "task_status": "SUCCEEDED",
                        "video_url": VIDEO_URL,
                    }
                },
            )
        return httpx.Response(200, content=b"not-a-video")

    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(
            handler,
            probe=lambda _path: (_ for _ in ()).throw(
                MediaToolError("synthetic ffprobe decode failure")
            ),
        ).generate(request=_request(tmp_path))
    assert caught.value.generation_error["code"] == "WAN_VIDEO_INVALID"


def test_wan_duration_requires_integer_between_two_and_fifteen(tmp_path: Path) -> None:
    request = _request(tmp_path)
    invalid = VideoGenerationRequest(
        project_id=request.project_id,
        job_id=request.job_id,
        shot=request.shot,
        source_image_path=request.source_image_path,
        prompt=request.prompt,
        motion_description=request.motion_description,
        output_dir=request.output_dir,
        options=VideoGenerationOptions(duration_seconds=2.5),
    )
    with pytest.raises(CloudWanVideoProviderError) as caught:
        _provider(lambda _request: pytest.fail("HTTP must not be called")).generate(
            request=invalid
        )
    assert caught.value.generation_error["code"] == "WAN_INPUT_INVALID"


def test_failed_cloud_task_marks_worker_job_failed_without_mock_fallback(
    client, database: Database, settings: Settings
) -> None:
    _project_id, job_id = _queue_cloud_worker_job(database, settings)

    def handler(request: httpx.Request) -> httpx.Response:
        status = "PENDING" if request.method == "POST" else "FAILED"
        return httpx.Response(
            200, json={"output": {"task_id": "task-failed", "task_status": status}}
        )

    worker = Worker(
        settings=settings,
        database=database,
        video_provider_factory=lambda _settings: _provider(handler),
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["status"] == "FAILED"
    assert payload["result_json"]["generation_error"]["code"] == "WAN_TASK_FAILED"
    assert payload["result_json"].get("mock_video_fallback") is not True
    assert payload["result_json"].get("video_shots") in (None, [])


def test_succeeded_cloud_worker_job_persists_real_video_assets(
    client, database: Database, settings: Settings
) -> None:
    _project_id, job_id = _queue_cloud_worker_job(database, settings)
    task_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_count
        if request.method == "POST":
            task_count += 1
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": f"task-{task_count}",
                        "task_status": "SUCCEEDED",
                        "video_url": VIDEO_URL,
                    }
                },
            )
        return httpx.Response(200, content=b"valid-cloud-mp4")

    worker = Worker(
        settings=settings,
        database=database,
        video_provider_factory=lambda _settings: _provider(handler),
    )
    assert worker.run_once() is True
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["status"] == "SUCCEEDED"
    result = payload["result_json"]
    assert result["video_source_type"] == "REAL_CLOUD_MODEL"
    assert result["mock_video_fallback"] is False
    assert result["video_provider_calls"] == 3
    assert len(result["video_shots"]) == 3
    assert all(item["ai_video_generated"] is True for item in result["video_shots"])
    assert all(item["source_type"] == "REAL_CLOUD_MODEL" for item in result["video_shots"])
    assert all(item["video_url"] for item in result["video_shots"])

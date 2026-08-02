"""Black-box M3 test for a real local llama.cpp text provider.

Prerequisites:
  1. llama-server is running (``scripts/run_llm_server.ps1``).
  2. FastAPI is running against the same project data directory used by this
     process (``scripts/run_backend.ps1``).

The script never fabricates a model response.  It requires the completed Job's
``result_json.script_trace`` with positive HTTP timings and hashed raw response
evidence before accepting the pure persisted ScriptV1, then verifies the
existing Mock media fallback through FFprobe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.media.ffmpeg import (  # noqa: E402
    resolve_media_tools,
    sha256_file,
    verify_media,
)
from backend.app.script_schema import ScriptV1, analyze_script_usage  # noqa: E402


class M3TestFailure(RuntimeError):
    """Raised when an externally observable M3 requirement is not met."""


class HttpClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path_or_url: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any, bytes, str]:
        url = urllib.parse.urljoin(self.base_url, path_or_url)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                content_type = response.headers.get_content_type()
                parsed = _decode_json(raw) if content_type == "application/json" else None
                return response.status, parsed, raw, content_type
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, _decode_json(raw, required=False), raw, (
                exc.headers.get_content_type() if exc.headers else ""
            )
        except urllib.error.URLError as exc:
            raise M3TestFailure(f"无法访问 {url}：{exc.reason}") from exc

    def expect_json(
        self,
        method: str,
        path_or_url: str,
        expected_status: int | tuple[int, ...] = 200,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        status, parsed, raw, _ = self.request(method, path_or_url, payload)
        statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
        if status not in statuses:
            detail = raw.decode("utf-8", errors="replace")
            raise M3TestFailure(
                f"{method} {path_or_url} 返回 HTTP {status}，预期 {statuses}：{detail}"
            )
        if parsed is None:
            raise M3TestFailure(f"{method} {path_or_url} 未返回 JSON")
        return parsed


def _decode_json(raw: bytes, *, required: bool = True) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if required:
            raise M3TestFailure("HTTP 响应不是有效 UTF-8 JSON") from exc
        return None


def _require_object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise M3TestFailure(f"{description} 必须是 JSON 对象")
    return value


def _require_nonempty_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise M3TestFailure(f"{description} 必须是非空字符串")
    return value.strip()


def _validation_warnings(value: Any, description: str) -> dict[str, list[str]]:
    warnings = _require_object(value, description)
    normalized: dict[str, list[str]] = {}
    for key in ("unused_scene_ids", "unused_character_ids"):
        items = warnings.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item for item in items
        ):
            raise M3TestFailure(f"{description}.{key} 必须是字符串数组")
        normalized[key] = list(items)
    return normalized


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    temporary.replace(path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _safe_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _normalize_provider_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("providers")
        if raw_items is None:
            raw_items = payload.get("data")
    else:
        raw_items = None
    if not isinstance(raw_items, list):
        raise M3TestFailure("GET /api/providers 响应缺少 providers 数组")
    if not all(isinstance(item, dict) for item in raw_items):
        raise M3TestFailure("GET /api/providers 包含非对象条目")
    return raw_items


def _provider_identifier(item: dict[str, Any]) -> Any:
    return item.get("provider_id", item.get("id"))


def _check_provider_registry(api: HttpClient) -> dict[str, Any]:
    items = _normalize_provider_items(api.expect_json("GET", "providers"))
    provider = next(
        (item for item in items if _provider_identifier(item) == "llamacpp"),
        None,
    )
    if provider is None:
        raise M3TestFailure("GET /api/providers 未注册 llamacpp")
    for flag_name in ("available", "configured", "healthy"):
        if provider.get(flag_name) is False:
            raise M3TestFailure(f"llamacpp Provider 的 {flag_name}=false：{provider}")
    return provider


def _check_llama_server(llm: HttpClient) -> tuple[dict[str, Any], str]:
    health = _require_object(llm.expect_json("GET", "health"), "llama.cpp /health")
    if health.get("status") != "ok":
        raise M3TestFailure(f"llama.cpp /health 状态异常：{health}")
    models = _require_object(llm.expect_json("GET", "v1/models"), "llama.cpp /v1/models")
    items = models.get("data")
    if not isinstance(items, list) or not items:
        raise M3TestFailure("llama.cpp /v1/models 未返回模型")
    first = _require_object(items[0], "llama.cpp model entry")
    model_id = _require_nonempty_string(first.get("id"), "llama.cpp model id")
    return health, model_id


def _run_worker_once(
    *,
    llm_base: str,
    model_id: str,
    timeout_seconds: float,
) -> tuple[subprocess.CompletedProcess[str], float, str]:
    command = [sys.executable, "-m", "backend.app.worker", "--once"]
    environment = os.environ.copy()
    environment["SCRIPT_PROVIDER"] = "llamacpp"
    environment["LLAMA_SERVER_BASE_URL"] = llm_base.rstrip("/")
    environment.setdefault("LLAMA_MODEL_ID", model_id)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise M3TestFailure(
            f"Worker --once 超过 {timeout_seconds:.0f} 秒；命令：{_safe_command(command)}"
        ) from exc
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise M3TestFailure(
            f"Worker --once 退出码 {completed.returncode}：{detail}"
        )
    return completed, elapsed, _safe_command(command)


def _wait_for_job(
    api: HttpClient,
    job_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = _require_object(api.expect_json("GET", f"jobs/{job_id}"), "Job")
        status = job.get("status")
        if status == "SUCCEEDED":
            return job
        if status == "FAILED":
            raise M3TestFailure(f"真实文本生成 Job 失败：{job.get('error_message')}")
        if status not in {"QUEUED", "RUNNING"}:
            raise M3TestFailure(f"Job 状态不在四状态集合内：{status!r}")
        time.sleep(poll_seconds)
    raise M3TestFailure(f"Job {job_id} 在 {timeout_seconds:.0f} 秒内未结束")


def _validate_script(
    script_value: Any,
    desired_shot_count: int | None,
) -> dict[str, Any]:
    script = _require_object(script_value, "project.script_json")
    try:
        validated = ScriptV1.model_validate(script)
    except Exception as exc:
        raise M3TestFailure(f"project.script_json 未通过纯 ScriptV1 校验：{exc}") from exc
    normalized = validated.model_dump(mode="json")
    if not 3 <= len(normalized["shots"]) <= 5:
        raise M3TestFailure("真实 ScriptV1 必须包含 3—5 个镜头")
    if (
        desired_shot_count is not None
        and len(normalized["shots"]) != desired_shot_count
    ):
        raise M3TestFailure(
            f"固定模式要求 {desired_shot_count} 个镜头，"
            f"实际生成 {len(normalized['shots'])} 个"
        )
    return normalized


def _validate_script_trace(
    job_result_value: Any,
    *,
    desired_shot_count: int | None,
    story_char_count: int,
) -> tuple[dict[str, Any], float]:
    result = _require_object(job_result_value, "完成 Job.result_json")
    if result.get("script_provider") != "llamacpp":
        raise M3TestFailure(
            f"Job result script_provider 标记错误：{result.get('script_provider')!r}"
        )
    if result.get("script_source_type") != "LOCAL_MODEL":
        raise M3TestFailure(
            "Job result script_source_type 必须为 LOCAL_MODEL："
            f"{result.get('script_source_type')!r}"
        )
    trace = _require_object(result.get("script_trace"), "Job.result_json.script_trace")
    if trace.get("provider_id") != "llamacpp":
        raise M3TestFailure(
            f"script_trace.provider_id 标记错误：{trace.get('provider_id')!r}"
        )
    if trace.get("source_type") != "LOCAL_MODEL":
        raise M3TestFailure(
            f"script_trace.source_type 必须为 LOCAL_MODEL：{trace.get('source_type')!r}"
        )
    if result.get("desired_shot_count") != desired_shot_count:
        raise M3TestFailure("Job result 未保留 desired_shot_count 快照")
    if result.get("story_char_count") != story_char_count:
        raise M3TestFailure("Job result 未保留 story_char_count 快照")
    if trace.get("desired_shot_count") != desired_shot_count:
        raise M3TestFailure("Provider trace 未保留 desired_shot_count")
    if trace.get("story_char_count") != story_char_count:
        raise M3TestFailure("Provider trace 未保留 story_char_count")
    if trace.get("status") != "SUCCEEDED":
        raise M3TestFailure(
            f"script_trace.status 必须为 SUCCEEDED：{trace.get('status')!r}"
        )
    trace_warnings = _validation_warnings(
        trace.get("validation_warnings"),
        "Job.result_json.script_trace.validation_warnings",
    )
    result_warnings = _validation_warnings(
        result.get("script_validation_warnings"),
        "Job.result_json.script_validation_warnings",
    )
    if result_warnings != trace_warnings:
        raise M3TestFailure("Job result 与 Provider trace 的校验警告不一致")
    try:
        elapsed_ms = float(trace.get("elapsed_ms"))
    except (TypeError, ValueError) as exc:
        raise M3TestFailure("script_trace.elapsed_ms 不是数字") from exc
    if elapsed_ms <= 0:
        raise M3TestFailure("script_trace.elapsed_ms 必须大于 0")
    _require_nonempty_string(trace.get("model"), "script_trace.model")
    _require_nonempty_string(trace.get("endpoint"), "script_trace.endpoint")
    _require_nonempty_string(trace.get("schema_sha256"), "script_trace.schema_sha256")
    attempts = trace.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise M3TestFailure("script_trace.attempts 必须记录至少一次真实 HTTP 请求")
    for index, attempt_value in enumerate(attempts, start=1):
        attempt = _require_object(attempt_value, f"script_trace.attempts[{index}]")
        try:
            attempt_elapsed = float(attempt.get("elapsed_ms"))
        except (TypeError, ValueError) as exc:
            raise M3TestFailure(f"第 {index} 次模型请求缺少有效 elapsed_ms") from exc
        if attempt_elapsed <= 0:
            raise M3TestFailure(f"第 {index} 次模型请求 elapsed_ms 必须大于 0")
        try:
            http_status = int(attempt.get("http_status"))
            response_size = int(attempt.get("raw_response_size_bytes"))
        except (TypeError, ValueError) as exc:
            raise M3TestFailure(
                f"第 {index} 次模型请求缺少 HTTP 状态或原始响应大小"
            ) from exc
        if not 200 <= http_status < 300 or response_size <= 0:
            raise M3TestFailure(
                f"第 {index} 次模型请求没有成功的非空 HTTP 响应证据"
            )
        raw_path = Path(
            _require_nonempty_string(
                attempt.get("raw_response_path"),
                f"script_trace.attempts[{index}].raw_response_path",
            )
        )
        expected_digest = _require_nonempty_string(
            attempt.get("raw_response_sha256"),
            f"script_trace.attempts[{index}].raw_response_sha256",
        )
        if not raw_path.is_file() or raw_path.stat().st_size != response_size:
            raise M3TestFailure(f"第 {index} 次模型请求的原始响应文件不存在或大小不符")
        if sha256_file(raw_path) != expected_digest:
            raise M3TestFailure(f"第 {index} 次模型请求的原始响应 SHA-256 不符")
        expected_prefix = (
            "first_raw_response" if index == 1 else "repair_raw_response"
        )
        if not raw_path.name.startswith(expected_prefix):
            raise M3TestFailure(
                f"第 {index} 次模型请求的原始响应文件名不可辨识：{raw_path.name}"
            )
    report_path = Path(
        _require_nonempty_string(
            trace.get("validation_report_path"),
            "script_trace.validation_report_path",
        )
    )
    if not report_path.is_file():
        raise M3TestFailure("validation_report.json 不存在")
    try:
        report = _require_object(
            json.loads(report_path.read_text(encoding="utf-8")),
            "validation_report.json",
        )
    except json.JSONDecodeError as exc:
        raise M3TestFailure("validation_report.json 不是有效 JSON") from exc
    if report.get("final_result") != "SUCCEEDED":
        raise M3TestFailure("validation_report 最终结果不是 SUCCEEDED")
    if report.get("desired_shot_count") != desired_shot_count:
        raise M3TestFailure("validation_report 镜头数快照错误")
    return trace, elapsed_ms


def _manifest_marker(manifest: dict[str, Any], name: str) -> Any:
    if name in manifest:
        return manifest[name]
    pipeline = manifest.get("pipeline")
    if isinstance(pipeline, dict) and name in pipeline:
        return pipeline[name]
    raise M3TestFailure(f"Manifest 缺少 {name} 追溯标记")


def _absolute_media_url(api_base: str, value: Any) -> str:
    relative = _require_nonempty_string(value, "媒体 URL")
    if urllib.parse.urlparse(relative).scheme:
        return relative
    parsed = urllib.parse.urlparse(api_base)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    return urllib.parse.urljoin(origin, relative.lstrip("/"))


def _download(api: HttpClient, url: str, target: Path, expected_type: str) -> bytes:
    status, _, content, content_type = api.request("GET", url)
    if status != 200:
        raise M3TestFailure(f"下载 {url} 返回 HTTP {status}")
    if not content:
        raise M3TestFailure(f"下载 {url} 得到空文件")
    if expected_type and content_type != expected_type:
        raise M3TestFailure(
            f"下载 {url} Content-Type 为 {content_type!r}，预期 {expected_type!r}"
        )
    _atomic_write(target, content)
    return content


def run(args: argparse.Namespace) -> dict[str, Any]:
    api_base = args.api_base.rstrip("/") + "/"
    llm_base = args.llm_base.rstrip("/") + "/"
    api = HttpClient(api_base, args.http_timeout)
    llm = HttpClient(llm_base, args.http_timeout)

    backend_health = _require_object(api.expect_json("GET", "health"), "后端 /health")
    if backend_health.get("service") != "ok" or backend_health.get("database") != "ok":
        raise M3TestFailure(f"后端健康检查失败：{backend_health}")
    if not backend_health.get("ffmpeg_available") or not backend_health.get(
        "ffprobe_available"
    ):
        raise M3TestFailure(f"后端未检测到 FFmpeg/FFprobe：{backend_health}")

    llama_health, model_id = _check_llama_server(llm)
    provider_registry = _check_provider_registry(api)

    desired_shot_count = (
        None
        if args.desired_shot_count == "auto"
        else int(args.desired_shot_count)
    )
    create_payload = {
        "title": args.title,
        "story": args.story,
    }
    project = _require_object(
        api.expect_json("POST", "projects", (200, 201), create_payload),
        "创建项目响应",
    )
    project_id = _require_nonempty_string(project.get("id"), "project.id")
    if "纸鹤" in project.get("title", "") or "纸鹤" in project.get("story", ""):
        raise M3TestFailure("M3 测试故事意外命中了纸鹤 fixture")

    queued = _require_object(
        api.expect_json(
            "POST",
            f"projects/{project_id}/generate",
            (200, 202),
            {
                "script_provider": "llamacpp",
                "desired_shot_count": desired_shot_count,
            },
        ),
        "生成入队响应",
    )
    job_id = _require_nonempty_string(
        queued.get("job_id", queued.get("id")), "job_id"
    )
    if queued.get("status") != "QUEUED":
        raise M3TestFailure(f"显式 llamacpp 任务未进入 QUEUED：{queued}")
    immediate = _require_object(api.expect_json("GET", f"jobs/{job_id}"), "Job")
    if immediate.get("status") != "QUEUED":
        raise M3TestFailure("HTTP 请求线程执行了生成任务，未保持 QUEUED")
    if immediate.get("provider_id") != "llamacpp":
        raise M3TestFailure(f"Job provider_id 不是 llamacpp：{immediate}")
    request_snapshot = _require_object(
        immediate.get("request_json"),
        "Job.request_json",
    )
    story_char_count = len(args.story.strip())
    if request_snapshot.get("desired_shot_count") != desired_shot_count:
        raise M3TestFailure("Job.request_json 未冻结 desired_shot_count")
    if request_snapshot.get("story_char_count") != story_char_count:
        raise M3TestFailure("Job.request_json 未冻结 story_char_count")

    evidence_dir = (args.evidence_root / project_id).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=False)
    worker_result, worker_elapsed, worker_command = _run_worker_once(
        llm_base=llm_base,
        model_id=model_id,
        timeout_seconds=args.worker_timeout,
    )
    _atomic_write(evidence_dir / "worker.stdout.log", worker_result.stdout.encode("utf-8"))
    _atomic_write(evidence_dir / "worker.stderr.log", worker_result.stderr.encode("utf-8"))

    job = _wait_for_job(
        api,
        job_id,
        timeout_seconds=args.job_timeout,
        poll_seconds=args.poll_interval,
    )
    if job.get("progress") != 100 or job.get("provider_id") != "llamacpp":
        raise M3TestFailure(f"SUCCEEDED Job 的进度或 Provider 错误：{job}")
    script_trace, model_elapsed_ms = _validate_script_trace(
        job.get("result_json"),
        desired_shot_count=desired_shot_count,
        story_char_count=story_char_count,
    )

    detail = _require_object(
        api.expect_json("GET", f"projects/{project_id}"), "项目详情"
    )
    persisted_project = _require_object(detail.get("project"), "项目详情.project")
    script = _validate_script(
        persisted_project.get("script_json"),
        desired_shot_count,
    )
    if script_trace.get("validated_script") != script:
        raise M3TestFailure(
            "Job script_trace.validated_script 与 Project 中持久化的纯 ScriptV1 不一致"
        )
    expected_warnings = analyze_script_usage(ScriptV1.model_validate(script)).model_dump(
        mode="json"
    )
    trace_warnings = _validation_warnings(
        script_trace.get("validation_warnings"),
        "script_trace.validation_warnings",
    )
    if trace_warnings != expected_warnings:
        raise M3TestFailure("Provider trace 的未使用实体警告与持久化 ScriptV1 不一致")
    _atomic_json(evidence_dir / "script.v1.json", script)

    shots = detail.get("shots")
    if not isinstance(shots, list) or not 3 <= len(shots) <= 5:
        raise M3TestFailure("项目详情未持久化 3—5 个 Shot")
    if len(shots) != len(script["shots"]):
        raise M3TestFailure("项目详情 Shot 数量与 ScriptV1 不一致")
    if {shot.get("provider_id") for shot in shots if isinstance(shot, dict)} != {
        "llamacpp"
    }:
        raise M3TestFailure("持久化 Shot 未统一标记 script provider=llamacpp")

    export = _require_object(detail.get("latest_export"), "项目详情.latest_export")
    video_url = _absolute_media_url(api_base, export.get("video_url"))
    manifest_url = _absolute_media_url(api_base, export.get("manifest_url"))
    video_path = evidence_dir / "m3_real_text_mock_media.mp4"
    manifest_path = evidence_dir / "manifest.json"
    video_content = _download(api, video_url, video_path, "video/mp4")
    manifest_content = _download(api, manifest_url, manifest_path, "application/json")
    manifest = _require_object(_decode_json(manifest_content), "下载的 Manifest")

    expected_markers = {
        "script_provider": "llamacpp",
        "image_provider": "mock",
        "audio_provider": "mock",
        "video_source_type": "DETERMINISTIC_FALLBACK",
    }
    for marker, expected in expected_markers.items():
        actual = _manifest_marker(manifest, marker)
        if actual != expected:
            raise M3TestFailure(
                f"Manifest {marker} 标记错误：预期 {expected!r}，实际 {actual!r}"
            )
    pipeline = _require_object(manifest.get("pipeline"), "Manifest pipeline")
    if pipeline.get("provider_id") != "mock":
        raise M3TestFailure("Manifest 媒体 pipeline.provider_id 必须为 mock")
    manifest_shots = manifest.get("shots")
    if not isinstance(manifest_shots, list) or not manifest_shots:
        raise M3TestFailure("Manifest 缺少镜头级 Provider 证据")
    if any(
        not isinstance(item, dict)
        or item.get("provider_id") != "mock"
        or item.get("script_provider_id") != "llamacpp"
        or item.get("subtitle_rendering") != "burned_in"
        or not item.get("narration")
        or not item.get("subtitle_text_path")
        or not item.get("font_path")
        for item in manifest_shots
    ):
        raise M3TestFailure(
            "Manifest 镜头必须区分 Provider，并保留动态烧录字幕追溯"
        )
    manifest_context = _require_object(
        manifest.get("generation_context"), "Manifest generation_context"
    )
    manifest_providers = _require_object(
        manifest_context.get("providers"), "Manifest generation_context.providers"
    )
    if manifest_providers.get("script_source_type") != "LOCAL_MODEL":
        raise M3TestFailure(
            "Manifest generation_context.providers.script_source_type 必须为 LOCAL_MODEL"
        )
    manifest_request = _require_object(
        manifest_context.get("request"),
        "Manifest generation_context.request",
    )
    if manifest_request.get("desired_shot_count") != desired_shot_count:
        raise M3TestFailure("Manifest 未保留 desired_shot_count")
    manifest_warnings = _validation_warnings(
        manifest.get("script_validation_warnings"),
        "Manifest script_validation_warnings",
    )
    context_warnings = _validation_warnings(
        manifest_context.get("script_validation_warnings"),
        "Manifest generation_context.script_validation_warnings",
    )
    if manifest_warnings != expected_warnings or context_warnings != expected_warnings:
        raise M3TestFailure("Manifest 未准确保留 ScriptV1 的未使用实体警告")

    validation = verify_media(
        resolve_media_tools(),
        video_path,
        expected_width=1280,
        expected_height=720,
        expected_fps=24.0,
        planned_duration_seconds=sum(
            float(shot["duration_seconds"]) for shot in script["shots"]
        ),
    )
    digest = hashlib.sha256(video_content).hexdigest()
    if digest != sha256_file(video_path):
        raise M3TestFailure("下载内容与落盘 MP4 的 SHA-256 不一致")
    if export.get("sha256") != digest:
        raise M3TestFailure("下载 MP4 的 SHA-256 与 Export 不一致")
    manifest_output = manifest.get("output")
    if not isinstance(manifest_output, dict) or manifest_output.get("sha256") != digest:
        raise M3TestFailure("下载 MP4 的 SHA-256 与 Manifest 不一致")

    summary = {
        "status": "PASS",
        "project_id": project_id,
        "job_id": job_id,
        "script_provider": "llamacpp",
        "image_provider": "mock",
        "audio_provider": "mock",
        "video_source_type": "DETERMINISTIC_FALLBACK",
        "model_id_from_server": model_id,
        "desired_shot_count": desired_shot_count,
        "actual_shot_count": len(script["shots"]),
        "story_char_count": story_char_count,
        "model_elapsed_ms": model_elapsed_ms,
        "script_validation_warnings": expected_warnings,
        "repair_used": bool(script_trace.get("repair_used")),
        "duration_normalization": script_trace.get("duration_normalization"),
        "model_request_count": len(script_trace["attempts"]),
        "script_trace": script_trace,
        "worker_elapsed_seconds": round(worker_elapsed, 3),
        "worker_command": worker_command,
        "script_path": str(evidence_dir / "script.v1.json"),
        "video_path": str(video_path),
        "manifest_path": str(manifest_path),
        "video_sha256": digest,
        "ffprobe": validation,
        "backend_health": backend_health,
        "llama_health": llama_health,
        "provider_registry_entry": provider_registry,
    }
    _atomic_json(evidence_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证真实 llama.cpp 文本 + Mock 媒体的 M3 完整链路"
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api/")
    parser.add_argument("--llm-base", default="http://127.0.0.1:8081/")
    parser.add_argument("--title", default="画册里的蓝鲸")
    parser.add_argument(
        "--story",
        default=(
            "深夜，一名少女在旧书店里发现一本会发光的画册。她翻开画册，"
            "纸上的蓝色鲸鱼突然游出书页，在书店上空盘旋。少女跟随鲸鱼跑上屋顶，"
            "看见整座城市的灯光变成漂浮的星辰。蓝色鲸鱼带着她穿过星光，"
            "在钟楼上空发现一扇隐藏的金色门。黎明到来，鲸鱼重新回到画册里，"
            "少女抱着画册站在屋顶，远处城市恢复原样。"
        ),
    )
    parser.add_argument(
        "--desired-shot-count",
        choices=("auto", "3", "4", "5"),
        default="4",
    )
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--worker-timeout", type=float, default=600.0)
    parser.add_argument("--job-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "generated" / "m3" / "real-llm-test",
    )
    args = parser.parse_args()
    for name in ("http_timeout", "worker_timeout", "job_timeout", "poll_interval"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    return args


def main() -> int:
    try:
        summary = run(parse_args())
    except (M3TestFailure, OSError, ValueError) as exc:
        print(f"M3 REAL LLM TEST FAILED: {exc}", file=sys.stderr)
        return 1
    print("M3 REAL LLM TEST PASSED")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

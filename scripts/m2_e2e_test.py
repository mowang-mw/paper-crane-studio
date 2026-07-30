"""Run an M2 black-box smoke test against an already running FastAPI server.

The script uses only the Python standard library for HTTP.  Start the API first
and either start the worker separately or pass ``--worker-once`` so this script
can drain queued jobs by invoking the worker in one-job mode.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
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
    find_chinese_font,
    resolve_media_tools,
    sha256_file,
    verify_media,
)


class E2EFailure(RuntimeError):
    """Raised when the externally observable M2 contract is not met."""


class ApiClient:
    def __init__(self, api_base: str, timeout: float) -> None:
        self.api_base = api_base.rstrip("/") + "/"
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any, bytes]:
        url = urllib.parse.urljoin(self.api_base, path.lstrip("/"))
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get_content_type()
                parsed = json.loads(raw.decode("utf-8")) if content_type == "application/json" else None
                return response.status, parsed, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            return exc.code, parsed, raw
        except urllib.error.URLError as exc:
            raise E2EFailure(f"Cannot reach {url}: {exc.reason}") from exc

    def expect_json(
        self,
        method: str,
        path: str,
        expected_status: int | tuple[int, ...],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, parsed, raw = self.request(method, path, payload)
        statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
        if status not in statuses:
            raise E2EFailure(
                f"{method} {path} returned HTTP {status}, expected {statuses}: "
                f"{raw.decode('utf-8', errors='replace')}"
            )
        if not isinstance(parsed, dict):
            raise E2EFailure(f"{method} {path} did not return a JSON object")
        return parsed


def run_worker_once(timeout: float) -> None:
    command = [sys.executable, "-m", "backend.app.worker", "--once"]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise E2EFailure(f"Worker --once exceeded {timeout:.0f} seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise E2EFailure(f"Worker --once failed with code {completed.returncode}: {detail}")


def get_media_url(api_base: str, value: str) -> str:
    if urllib.parse.urlparse(value).scheme:
        return value
    server_root = urllib.parse.urljoin(api_base.rstrip("/") + "/", "../")
    return urllib.parse.urljoin(server_root, value.lstrip("/"))


def wait_for_job(
    client: ApiClient,
    job_id: str,
    *,
    timeout: float,
    poll_interval: float,
    worker_once: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.expect_json("GET", f"jobs/{job_id}", 200)
        status = job.get("status")
        if status == "SUCCEEDED":
            return job
        if status == "FAILED":
            raise E2EFailure(f"Generation job failed: {job.get('error_message')}")
        if status not in {"QUEUED", "RUNNING"}:
            raise E2EFailure(f"Unexpected job status: {status!r}")
        if worker_once and status == "QUEUED":
            # A previous queued job may be older, so bounded repeated one-job runs
            # are allowed until the target job is claimed.
            run_worker_once(max(30.0, timeout))
        else:
            time.sleep(poll_interval)
    raise E2EFailure(f"Job {job_id} did not finish within {timeout:.0f} seconds")


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = ApiClient(args.api_base, args.http_timeout)
    health = client.expect_json("GET", "health", 200)
    if health.get("service") != "ok" or health.get("database") != "ok":
        raise E2EFailure(f"Health response is not healthy: {health}")

    unique_suffix = int(time.time())
    project = client.expect_json(
        "POST",
        "projects",
        (200, 201),
        {
            "title": f"纸鹤的夜航-E2E-{unique_suffix}",
            "story": "少女折出一只纸鹤，纸鹤飞过夜空，在黎明时飞向远方。",
        },
    )
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise E2EFailure(f"Project response lacks an id: {project}")

    queued = client.expect_json("POST", f"projects/{project_id}/generate", (200, 202))
    job_id = queued.get("id") or queued.get("job_id")
    if not isinstance(job_id, str) or queued.get("status") != "QUEUED":
        raise E2EFailure(f"Generate did not return a QUEUED job: {queued}")

    # This immediate read is the observable guard against media generation in the
    # request handler.  The worker is not invoked until after this assertion.
    immediate = client.expect_json("GET", f"jobs/{job_id}", 200)
    if immediate.get("status") != "QUEUED":
        raise E2EFailure(f"Job was executed inside the HTTP request: {immediate}")

    finished = wait_for_job(
        client,
        job_id,
        timeout=args.job_timeout,
        poll_interval=args.poll_interval,
        worker_once=args.worker_once,
    )
    if finished.get("progress") != 100:
        raise E2EFailure(f"Succeeded job progress is not 100: {finished}")

    detail = client.expect_json("GET", f"projects/{project_id}", 200)
    shots = detail.get("shots")
    export = detail.get("latest_export")
    if not isinstance(shots, list) or len(shots) != 4:
        raise E2EFailure(f"Expected four shots, got: {shots}")
    if not isinstance(export, dict):
        raise E2EFailure(f"Project has no latest export: {detail}")

    video_url = export.get("video_url") or export.get("media_url")
    manifest_url = export.get("manifest_url")
    if not isinstance(video_url, str) or not isinstance(manifest_url, str):
        raise E2EFailure(f"Export lacks media URLs: {export}")

    video_status, _, video_bytes = client.request("GET", get_media_url(args.api_base, video_url))
    if video_status != 200 or not video_bytes:
        raise E2EFailure(f"Video download failed with HTTP {video_status}")
    manifest_status, manifest, _ = client.request("GET", get_media_url(args.api_base, manifest_url))
    if manifest_status != 200 or not isinstance(manifest, dict):
        raise E2EFailure(f"Manifest download failed with HTTP {manifest_status}")

    with tempfile.TemporaryDirectory(prefix="anime-m2-e2e-") as temporary_directory:
        downloaded_video = Path(temporary_directory) / "paper_crane_m2.mp4"
        downloaded_video.write_bytes(video_bytes)
        tools = resolve_media_tools()
        font = find_chinese_font()
        probe = verify_media(
            tools,
            downloaded_video,
            min_duration=20.0,
            max_duration=40.0,
            expected_width=1280,
            expected_height=720,
            expected_fps=24.0,
        )
        actual_sha256 = sha256_file(downloaded_video)

    manifest_output = manifest.get("output")
    manifest_sha256 = (
        manifest_output.get("sha256") if isinstance(manifest_output, dict) else None
    )
    expected_sha256 = export.get("sha256") or manifest_sha256
    if expected_sha256 and expected_sha256 != actual_sha256:
        raise E2EFailure(
            f"Downloaded video SHA-256 {actual_sha256} does not match {expected_sha256}"
        )

    # Persistence is checked from a fresh HTTP request, not from the create result.
    persisted = client.expect_json("GET", f"projects/{project_id}", 200)
    persisted_project = persisted.get("project")
    if (
        not isinstance(persisted_project, dict)
        or persisted_project.get("id") != project_id
        or not persisted.get("latest_export")
    ):
        raise E2EFailure("Project/export was not persisted across requests")

    return {
        "project_id": project_id,
        "job_id": job_id,
        "job_status": finished.get("status"),
        "shot_count": len(shots),
        "duration_seconds": probe["duration_seconds"],
        "video_codec": probe["video_codec"],
        "audio_codec": probe["audio_codec"],
        "resolution": f"{probe['width']}x{probe['height']}",
        "fps": probe["frame_rate"],
        "sha256": actual_sha256,
        "font": str(font),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the running M2 vertical slice")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--worker-once", action="store_true")
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--job-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    try:
        summary = run(parse_args())
    except (E2EFailure, OSError, ValueError) as exc:
        print(f"M2 E2E FAILED: {exc}", file=sys.stderr)
        return 1
    print("M2 E2E PASSED")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M2 独立单 Worker：SQLite 轮询、Provider 编排、直接媒体函数调用。"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import crud
from .config import Settings
from .database import Database
from .media.ffmpeg import sha256_file
from .models import JobStatus
from .providers.mock import MockAudioProvider, MockImageProvider, MockScriptProvider
from .services.generation import GenerationService


Renderer = Callable[..., dict[str, Any]]


class Worker:
    """M2 仅支持启动一个实例；崩溃恢复与租约留待增强阶段。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        database: Database | None = None,
        renderer: Renderer | None = None,
        generation_service: GenerationService | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.ensure_directories()
        self.database = database or Database(str(self.settings.database_url))
        self.database.create_schema()
        self.renderer = renderer
        self.generation_service = generation_service or GenerationService(
            script_provider=MockScriptProvider(self.settings.root_dir),
            image_provider=MockImageProvider(),
            audio_provider=MockAudioProvider(),
        )
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_once(self) -> bool:
        """处理至多一个任务；无任务返回 False，成功或失败处理均返回 True。"""

        with self.database.session() as session:
            job = crud.claim_next_queued_job(session)
            if job is None:
                return False
            job_id = job.id
            session.commit()

        print(f"[worker] claimed job={job_id}", flush=True)
        try:
            self._process(job_id)
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: {exc}"
            with self.database.session() as session:
                failed_job = crud.get_job(session, job_id)
                if failed_job is not None and failed_job.status in (
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                ):
                    crud.mark_job_failed(
                        session,
                        job=failed_job,
                        error_message=diagnostic,
                    )
                    session.commit()
            print(f"[worker] job={job_id} FAILED: {diagnostic}", file=sys.stderr)
            traceback.print_exc()
        return True

    def _process(self, job_id: str) -> None:
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"无法处理非 RUNNING 任务：{job_id}")
            project = crud.get_project(session, job.project_id)
            if project is None:
                raise RuntimeError(f"任务所属项目不存在：{job.project_id}")
            prepared = self.generation_service.prepare(session, project)
            crud.set_job_progress(session, job, 20)
            session.commit()
            project_id = project.id
            project_title = project.title
            media_shots = list(prepared.media_shots)
            generation_context = {
                "generation_job_id": job.id,
                "job_type": job.job_type,
                "request": dict(job.request_json or {}),
                "script": {
                    key: value
                    for key, value in dict(project.script_json or {}).items()
                    if key != "shots"
                },
            }

        output_dir = self.settings.project_dir(project_id) / "exports" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        renderer = self.renderer or self._default_renderer()
        rendered = renderer(
            root=self.settings.root_dir,
            project_id=project_id,
            project_title=project_title,
            shots=media_shots,
            output_dir=output_dir,
            output_filename=f"mock_short_{job_id}.mp4",
            generation_context=generation_context,
        )

        output_path = Path(str(rendered.get("output_path", ""))).resolve()
        manifest_path = Path(str(rendered.get("manifest_path", ""))).resolve()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("媒体函数未生成有效 MP4")
        if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
            raise RuntimeError("媒体函数未生成有效 manifest")
        relative_video = self._relative_project_path(output_path, project_id)
        relative_manifest = self._relative_project_path(manifest_path, project_id)
        video_sha256 = sha256_file(output_path)
        reported_sha256 = rendered.get("sha256")
        if reported_sha256 and reported_sha256 != video_sha256:
            raise RuntimeError("媒体返回的 SHA-256 与实际输出不一致")
        manifest_sha256 = sha256_file(manifest_path)
        validation = rendered.get("validation")
        if not isinstance(validation, dict):
            raise RuntimeError("媒体返回结果缺少 ffprobe validation")
        try:
            duration_seconds = float(validation["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("媒体 validation 缺少有效时长") from exc

        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"媒体完成时任务不再是 RUNNING：{job_id}")
            crud.set_job_progress(session, job, 90)
            video_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="EXPORT_VIDEO",
                file_path=relative_video,
                sha256=video_sha256,
                metadata_json={
                    "job_id": job_id,
                    "validation": validation,
                    "font_path": rendered.get("font_path"),
                },
            )
            manifest_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="MANIFEST",
                file_path=relative_manifest,
                sha256=manifest_sha256,
                metadata_json={"job_id": job_id},
            )
            export = crud.create_export(
                session,
                project_id=project_id,
                job_id=job_id,
                file_path=relative_video,
                manifest_path=relative_manifest,
                duration_seconds=duration_seconds,
                sha256=video_sha256,
            )
            result_json = {
                "export_id": export.id,
                "video_asset_id": video_asset.id,
                "manifest_asset_id": manifest_asset.id,
                "video_path": relative_video,
                "manifest_path": relative_manifest,
                "duration_seconds": duration_seconds,
                "sha256": video_sha256,
                "validation": validation,
                "provider_id": "mock",
                "source_type": "DETERMINISTIC_FALLBACK",
                "video_url": f"/api/projects/{project_id}/exports/{export.id}/video",
                "download_url": (
                    f"/api/projects/{project_id}/exports/{export.id}/video?download=true"
                ),
                "manifest_url": f"/api/projects/{project_id}/exports/{export.id}/manifest",
            }
            crud.mark_job_succeeded(session, job=job, result_json=result_json)
            session.commit()
        print(f"[worker] job={job_id} SUCCEEDED export={export.id}", flush=True)

    @staticmethod
    def _default_renderer() -> Renderer:
        # 延迟导入让 API 与轻量测试不因媒体环境缺失而无法启动。
        from .media import render_mock_project_short

        return render_mock_project_short

    def _relative_project_path(self, path: Path, project_id: str) -> str:
        data_root = Path(self.settings.data_dir).resolve()
        project_root = self.settings.project_dir(project_id).resolve()
        try:
            path.resolve().relative_to(project_root)
        except ValueError as exc:
            raise RuntimeError(f"媒体输出越过当前项目目录：{path}") from exc
        return path.resolve().relative_to(data_root).as_posix()

    def run_forever(self, *, poll_seconds: float | None = None) -> None:
        interval = self.settings.worker_poll_seconds if poll_seconds is None else poll_seconds
        if interval <= 0:
            raise ValueError("轮询间隔必须大于 0")
        print(
            json.dumps(
                {
                    "worker": "m2-single-worker",
                    "database": self.settings.database_url,
                    "poll_seconds": interval,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        while not self._stop_requested:
            if not self.run_once():
                time.sleep(interval)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AnimeFlow M2 SQLite 单 Worker")
    parser.add_argument("--once", action="store_true", help="最多处理一个任务后退出")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="覆盖 ANIME_PLATFORM_WORKER_POLL_SECONDS",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    worker = Worker()
    if args.once:
        processed = worker.run_once()
        print("[worker] processed=1" if processed else "[worker] queue empty")
        return 0

    def stop(_signum: int, _frame: object) -> None:
        worker.request_stop()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        worker.run_forever(poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        worker.request_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M2 独立单 Worker：SQLite 轮询、Provider 编排、直接媒体函数调用。"""

from __future__ import annotations

import argparse
import copy
import json
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import crud
from .config import Settings
from .database import Database
from .media.ffmpeg import (
    MediaToolError,
    ffprobe_json,
    resolve_media_tools,
    run_command,
    sha256_file,
)
from .models import JobStatus
from .providers.base import (
    AudioGenerationOptions,
    AudioGenerationRequest,
    AudioProvider,
    GeneratedAudioAsset,
    GeneratedImageAsset,
    ImageGenerationOptions,
    ImageGenerationRequest,
    ImageProvider,
)
from .providers.comfyui import ComfyUIImageProvider
from .providers.llama_cpp import LlamaCppScriptProvider
from .providers.llama_server import LlamaServerJobSession
from .providers.mock import MockAudioProvider, MockImageProvider, MockScriptProvider
from .providers.qwen3_tts import create_qwen3_tts_provider
from .providers.registry import check_llamacpp
from .script_schema import ScriptV1
from .services.generation import GenerationService, PreparedGeneration
from .services.image_jobs import (
    GpuMemoryMonitor,
    REAL_IMAGE_JOB_TYPE,
    REAL_IMAGE_PROVIDER_ID,
    RealImageJobError,
    gpu_handoff_status,
    load_script_snapshot,
)
from .services.audio_jobs import (
    REAL_AUDIO_JOB_TYPE,
    REAL_AUDIO_PROVIDER_ID,
    REAL_AUDIO_SOURCE_TYPE,
    RealAudioJobError,
    AudioValidationError,
    atomic_json,
    audio_gpu_handoff_status,
    build_media_timing_plan,
    load_audio_source_snapshot,
    inspect_pcm16_wav,
)
from .services.media_rerender import (
    MEDIA_RERENDER_JOB_TYPE,
    MEDIA_RERENDER_PROVIDER_ID,
    MediaRerenderJobError,
)


Renderer = Callable[..., dict[str, Any]]
ImageProviderFactory = Callable[[Settings], ImageProvider]
AudioProviderFactory = Callable[[Settings], AudioProvider]


class MediaResumeError(RuntimeError):
    """MEDIA_RENDER 恢复证据缺失或损坏；禁止静默重新生成剧本。"""

    def __init__(self, message: str, *, request_snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.generation_error = {
            "code": "MEDIA_RESUME_FAILED",
            "stage": "MEDIA_RENDER",
            "summary": message,
            "story_char_count": request_snapshot.get("story_char_count"),
            "story_length_valid": True,
            "desired_shot_count": request_snapshot.get("desired_shot_count"),
            "first_attempt_errors": [],
            "repair_attempt_errors": [],
            "suggestions": [
                "保留当前失败任务和追溯目录，检查 ScriptV1 或 MP4 是否缺失。",
                "恢复证据损坏时不会静默重新调用文本模型。",
            ],
            "provider_id": request_snapshot.get("script_provider"),
            "model_id": None,
            "raw_response_path": None,
            "repair_response_path": None,
            "validation_report_path": None,
        }


class Worker:
    """M2 仅支持启动一个实例；崩溃恢复与租约留待增强阶段。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        database: Database | None = None,
        renderer: Renderer | None = None,
        resume_renderer: Renderer | None = None,
        generation_service: GenerationService | None = None,
        real_image_renderer: Renderer | None = None,
        image_provider_factory: ImageProviderFactory | None = None,
        real_audio_renderer: Renderer | None = None,
        audio_provider_factory: AudioProviderFactory | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.ensure_directories()
        self.database = database or Database(str(self.settings.database_url))
        self.database.create_schema()
        self.renderer = renderer
        self.resume_renderer = resume_renderer
        self.generation_service = generation_service
        self.real_image_renderer = real_image_renderer
        self.image_provider_factory = image_provider_factory
        self.real_audio_renderer = real_audio_renderer
        self.audio_provider_factory = audio_provider_factory
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
            technical_diagnostic = f"{type(exc).__name__}: {exc}"
            with self.database.session() as session:
                failed_job = crud.get_job(session, job_id)
                if failed_job is not None and failed_job.status in (
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                ):
                    request_snapshot = dict(failed_job.request_json or {})
                    existing_result = dict(failed_job.result_json or {})
                    generation_error = getattr(exc, "generation_error", None)
                    if not isinstance(generation_error, dict):
                        if failed_job.progress >= 20:
                            stage = "MEDIA_RENDER"
                            code = "MEDIA_RENDER_FAILED"
                            summary = f"媒体渲染失败：{str(exc)[:500]}"
                            suggestions = [
                                "检查 FFmpeg/FFprobe 是否可用后手动重试。",
                                "查看 Worker 日志中的完整技术错误。",
                            ]
                        elif "不可用" in str(exc) or "模型文件" in str(exc):
                            stage = "PROVIDER_UNAVAILABLE"
                            code = "PROVIDER_UNAVAILABLE"
                            summary = str(exc)[:500]
                            suggestions = [
                                "检查 llama-server、GGUF 配置与 8081 端口后手动重试。",
                            ]
                        else:
                            stage = "SCRIPT_SCHEMA_VALIDATION"
                            code = "SCRIPT_GENERATION_FAILED"
                            summary = f"剧本生成失败：{str(exc)[:500]}"
                            suggestions = [
                                "查看详细原因和 Worker 日志后手动重试。",
                            ]
                        generation_error = {
                            "code": code,
                            "stage": stage,
                            "summary": summary,
                            "story_char_count": request_snapshot.get(
                                "story_char_count"
                            ),
                            "story_length_valid": (
                                isinstance(
                                    request_snapshot.get("story_char_count"),
                                    int,
                                )
                                and 10
                                <= request_snapshot["story_char_count"]
                                <= 3000
                            ),
                            "desired_shot_count": request_snapshot.get(
                                "desired_shot_count"
                            ),
                            "first_attempt_errors": [],
                            "repair_attempt_errors": [],
                            "suggestions": suggestions,
                            "provider_id": failed_job.provider_id,
                            "model_id": existing_result.get("script_model_id"),
                            "raw_response_path": None,
                            "repair_response_path": None,
                            "validation_report_path": None,
                        }
                    existing_result["generation_error"] = generation_error
                    existing_result["desired_shot_count"] = request_snapshot.get(
                        "desired_shot_count"
                    )
                    existing_result["story_char_count"] = request_snapshot.get(
                        "story_char_count"
                    )
                    failed_job.result_json = existing_result
                    display_error = (
                        f"{generation_error.get('code', 'GENERATION_FAILED')}: "
                        f"{generation_error.get('summary', str(exc))}"
                    )
                    crud.mark_job_failed(
                        session,
                        job=failed_job,
                        error_message=display_error,
                    )
                    session.commit()
            print(
                f"[worker] job={job_id} FAILED: {technical_diagnostic}",
                file=sys.stderr,
            )
            traceback.print_exc()
        return True

    def _process(self, job_id: str) -> None:
        # 第一个短事务只读取不可变输入快照。真实模型 HTTP 调用绝不持有 Session。
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"无法处理非 RUNNING 任务：{job_id}")
            project = crud.get_project(session, job.project_id)
            if project is None:
                raise RuntimeError(f"任务所属项目不存在：{job.project_id}")
            request_snapshot = dict(job.request_json or {})
            image_provider_id: str | None = None
            audio_provider_id: str | None = None
            media_only = job.job_type == MEDIA_RERENDER_JOB_TYPE
            if media_only:
                project_id = project.id
                project_title = project.title
                project_script_json = copy.deepcopy(project.script_json)
            elif job.job_type == REAL_AUDIO_JOB_TYPE:
                project_id = project.id
                project_title = project.title
                project_story = project.story
                audio_provider_id = job.provider_id
            elif job.job_type == REAL_IMAGE_JOB_TYPE:
                project_id = project.id
                project_title = project.title
                project_story = project.story
                image_provider_id = job.provider_id

            if audio_provider_id is not None:
                if request_snapshot.get("audio_provider") != audio_provider_id:
                    raise RealAudioJobError(
                        code="AUDIO_PROVIDER_SNAPSHOT_MISMATCH",
                        stage="AUDIO_GENERATION",
                        summary="Job provider_id 与真实旁白 Provider 快照不一致。",
                        retryable=False,
                    )
                if audio_provider_id != REAL_AUDIO_PROVIDER_ID:
                    raise RealAudioJobError(
                        code="AUDIO_PROVIDER_UNSUPPORTED",
                        stage="AUDIO_GENERATION",
                        summary=f"不支持的真实 AudioProvider：{audio_provider_id}",
                        retryable=False,
                    )
                project_script_json = copy.deepcopy(project.script_json)

            if image_provider_id is not None:
                # 真实图片 Job 的 provider_id 描述 ImageProvider；不能再按
                # ScriptProvider 校验，否则会误把合法任务拒绝为未知文本模型。
                if request_snapshot.get("image_provider") != image_provider_id:
                    raise RealImageJobError(
                        code="IMAGE_PROVIDER_SNAPSHOT_MISMATCH",
                        stage="IMAGE_GENERATION",
                        summary="Job provider_id 与真实图像 Provider 快照不一致。",
                        retryable=False,
                    )
                if image_provider_id != REAL_IMAGE_PROVIDER_ID:
                    raise RealImageJobError(
                        code="IMAGE_PROVIDER_UNSUPPORTED",
                        stage="IMAGE_GENERATION",
                        summary=f"不支持的真实 ImageProvider：{image_provider_id}",
                        retryable=False,
                    )
                project_script_json = copy.deepcopy(project.script_json)

        if media_only:
            self._process_media_rerender_job(
                job_id=job_id,
                project_id=project_id,
                project_title=project_title,
                project_script_json=project_script_json,
                request_snapshot=request_snapshot,
            )
            return

        if audio_provider_id is not None:
            self._process_real_audio_job(
                job_id=job_id,
                project_id=project_id,
                project_title=project_title,
                project_story=project_story,
                project_script_json=project_script_json,
                request_snapshot=request_snapshot,
            )
            return

        if image_provider_id is not None:
            self._process_real_image_job(
                job_id=job_id,
                project_id=project_id,
                project_title=project_title,
                project_story=project_story,
                project_script_json=project_script_json,
                request_snapshot=request_snapshot,
            )
            return

        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"无法处理非 RUNNING 任务：{job_id}")
            project = crud.get_project(session, job.project_id)
            if project is None:
                raise RuntimeError(f"任务所属项目不存在：{job.project_id}")
            request_snapshot = dict(job.request_json or {})
            provider_id = str(
                request_snapshot.get("script_provider") or job.provider_id
            ).strip().lower()
            if provider_id != job.provider_id:
                raise RuntimeError("Job provider_id 与 request_json 快照不一致")
            if provider_id not in {"mock", "llamacpp"}:
                raise RuntimeError(f"不支持的 ScriptProvider：{provider_id}")
            desired_value = request_snapshot.get("desired_shot_count", 4)
            if desired_value is None:
                desired_shot_count = None
            elif isinstance(desired_value, int) and not isinstance(desired_value, bool):
                desired_shot_count = desired_value
            else:
                raise RuntimeError("Job desired_shot_count 快照必须为 3、4、5 或 null")
            if desired_shot_count not in (None, 3, 4, 5):
                raise RuntimeError("Job desired_shot_count 快照必须为 3、4、5 或 null")
            story_char_count = len(project.story.strip())
            snapshotted_story_char_count = request_snapshot.get("story_char_count")
            if snapshotted_story_char_count != story_char_count:
                raise RuntimeError(
                    "Job story_char_count 与项目故事快照不一致，已拒绝继续生成"
                )
            project_id = project.id
            project_title = project.title
            project_story = project.story

        resumed_from_stage = request_snapshot.get("resumed_from_stage")
        if resumed_from_stage is not None:
            if resumed_from_stage != "MEDIA_RENDER":
                raise RuntimeError(
                    f"不支持从阶段 {resumed_from_stage!r} 恢复任务"
                )
            self._resume_media_job(
                job_id=job_id,
                project_id=project_id,
                project_title=project_title,
                provider_id=provider_id,
                desired_shot_count=desired_shot_count,
                story_char_count=story_char_count,
                request_snapshot=request_snapshot,
            )
            return

        generation_service = self._generation_service_for(
            provider_id=provider_id,
            project_id=project_id,
            job_id=job_id,
        )
        prepared = generation_service.prepare(
            title=project_title,
            story=project_story,
            desired_shot_count=desired_shot_count,
        )

        self._process_prepared(
            job_id=job_id,
            project_id=project_id,
            project_title=project_title,
            provider_id=provider_id,
            request_snapshot=request_snapshot,
            generation_service=generation_service,
            prepared=prepared,
        )
        return

    def _resume_media_job(
        self,
        *,
        job_id: str,
        project_id: str,
        project_title: str,
        provider_id: str,
        desired_shot_count: int | None,
        story_char_count: int,
        request_snapshot: dict[str, Any],
    ) -> None:
        source_job_id = request_snapshot.get("retry_of_job_id")
        if not isinstance(source_job_id, str) or not source_job_id:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复缺少 retry_of_job_id，已拒绝重新生成剧本。",
                request_snapshot=request_snapshot,
            )

        with self.database.session() as session:
            source_job = crud.get_job(session, source_job_id)
            if source_job is None or source_job.project_id != project_id:
                raise MediaResumeError(
                    "MEDIA_RENDER 恢复来源任务不存在或不属于当前项目。",
                    request_snapshot=request_snapshot,
                )
            source_result = dict(source_job.result_json or {})
            source_error = source_result.get("generation_error")
            if (
                source_job.status != JobStatus.FAILED
                or not isinstance(source_error, dict)
                or source_error.get("stage") != "MEDIA_RENDER"
            ):
                raise MediaResumeError(
                    "只有明确在 MEDIA_RENDER 阶段失败的任务可以媒体恢复。",
                    request_snapshot=request_snapshot,
                )

        source_trace = source_result.get("script_trace")
        if not isinstance(source_trace, dict):
            raise MediaResumeError(
                "MEDIA_RENDER 恢复缺少 ScriptProvider 追溯。",
                request_snapshot=request_snapshot,
            )
        validation_report_value = source_trace.get("validation_report_path")
        if not isinstance(validation_report_value, str) or not validation_report_value:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复缺少 validation_report_path。",
                request_snapshot=request_snapshot,
            )
        trace_path = (Path(validation_report_value).resolve().parent / "trace.json").resolve()
        source_job_root = (
            self.settings.project_dir(project_id) / "jobs" / source_job_id
        ).resolve()
        try:
            trace_path.relative_to(source_job_root)
        except ValueError as exc:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复的 ScriptV1 追溯路径越过来源 Job 目录。",
                request_snapshot=request_snapshot,
            ) from exc
        try:
            trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
            script = ScriptV1.model_validate(trace_payload["validated_script"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复的 ScriptV1 追溯文件缺失或损坏；"
                "已拒绝静默重新调用文本模型。",
                request_snapshot=request_snapshot,
            ) from exc
        if desired_shot_count is not None and len(script.shots) != desired_shot_count:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复的 ScriptV1 镜头数与原任务快照不一致。",
                request_snapshot=request_snapshot,
            )

        trace = copy.deepcopy(source_trace)
        trace["resumed_from_stage"] = "MEDIA_RENDER"
        trace["resumed_from_job_id"] = source_job_id
        trace["script_provider_calls_during_resume"] = 0
        source_type = str(source_result.get("script_source_type") or "LOCAL_MODEL")
        if self.generation_service is not None:
            generation_service = self.generation_service
        else:
            # 这里只复用 GenerationService 的 Image/Audio 规划逻辑；
            # prepare_validated_script 不会调用这个占位 ScriptProvider。
            generation_service = GenerationService(
                script_provider=MockScriptProvider(self.settings.root_dir),
                image_provider=MockImageProvider(),
                audio_provider=MockAudioProvider(),
            )
        prepared = generation_service.prepare_validated_script(
            script=script,
            provider_id=provider_id,
            source_type=source_type,
            trace=trace,
            desired_shot_count=desired_shot_count,
            story_char_count=story_char_count,
        )

        source_output_dir = (
            self.settings.project_dir(project_id) / "exports" / source_job_id
        ).resolve()
        source_final = source_output_dir / f"short_{source_job_id}.mp4"
        source_partial = source_output_dir / f"short_{source_job_id}.part.mp4"
        if source_final.is_file() and source_final.stat().st_size > 0:
            source_media_path = source_final
        elif source_partial.is_file() and source_partial.stat().st_size > 0:
            source_media_path = source_partial
        else:
            raise MediaResumeError(
                "MEDIA_RENDER 恢复未找到已编码的 MP4；"
                "已拒绝静默重新调用文本模型。",
                request_snapshot=request_snapshot,
            )

        self._process_prepared(
            job_id=job_id,
            project_id=project_id,
            project_title=project_title,
            provider_id=provider_id,
            request_snapshot=request_snapshot,
            generation_service=generation_service,
            prepared=prepared,
            source_media_path=source_media_path,
            resumed_from_job_id=source_job_id,
        )

    def _process_prepared(
        self,
        *,
        job_id: str,
        project_id: str,
        project_title: str,
        provider_id: str,
        request_snapshot: dict[str, Any],
        generation_service: GenerationService,
        prepared: PreparedGeneration,
        source_media_path: Path | None = None,
        resumed_from_job_id: str | None = None,
    ) -> None:
        recovery_trace = (
            {
                "resumed_from_stage": "MEDIA_RENDER",
                "resumed_from_job_id": resumed_from_job_id,
                "script_provider_calls_during_resume": 0,
            }
            if resumed_from_job_id is not None
            else {}
        )
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"剧本完成时任务不再是 RUNNING：{job_id}")
            project = crud.get_project(session, project_id)
            if project is None:
                raise RuntimeError(f"任务所属项目不存在：{project_id}")
            generation_service.persist(session, project=project, prepared=prepared)
            crud.set_job_progress(session, job, 20)
            job.result_json = {
                "script_provider": prepared.script_provider,
                "script_json": prepared.script_json,
                "script_source_type": prepared.script_source_type,
                "script_trace": prepared.script_trace,
                "script_validation_warnings": prepared.script_validation_warnings,
                "desired_shot_count": prepared.desired_shot_count,
                "actual_shot_count": prepared.actual_shot_count,
                "story_char_count": prepared.story_char_count,
                "repair_used": prepared.repair_used,
                "duration_normalization": prepared.duration_normalization,
                "planned_duration_seconds": prepared.planned_duration_seconds,
                **recovery_trace,
            }
            session.commit()
            media_shots = list(prepared.media_shots)
            generation_context = {
                "generation_job_id": job.id,
                "job_type": job.job_type,
                "request": request_snapshot,
                "script": {
                    "fixture_version": prepared.script_json["schema_version"],
                    "schema_version": prepared.script_json["schema_version"],
                    "provider_id": prepared.script_provider,
                },
                "providers": {
                    "script_provider": prepared.script_provider,
                    "script_source_type": prepared.script_source_type,
                    "script_model_id": prepared.script_trace.get(
                        "model", "mock-script.v1"
                    ),
                    "image_provider": "mock",
                    "audio_provider": "mock",
                    "video_source_type": "DETERMINISTIC_FALLBACK",
                },
                "script_trace": prepared.script_trace,
                "script_validation_warnings": prepared.script_validation_warnings,
                "desired_shot_count": prepared.desired_shot_count,
                "actual_shot_count": prepared.actual_shot_count,
                "story_char_count": prepared.story_char_count,
                "repair_used": prepared.repair_used,
                "duration_normalization": prepared.duration_normalization,
                "planned_duration_seconds": prepared.planned_duration_seconds,
                **recovery_trace,
            }

        output_dir = self.settings.project_dir(project_id) / "exports" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        renderer_arguments: dict[str, Any] = {
            "root": self.settings.root_dir,
            "project_id": project_id,
            "project_title": project_title,
            "shots": media_shots,
            "output_dir": output_dir,
            "output_filename": f"short_{job_id}.mp4",
            "provider_id": "mock",
            "generation_context": generation_context,
            **self._renderer_media_options(request_snapshot, project_id),
        }
        if source_media_path is None:
            renderer = self.renderer or self._default_renderer()
        else:
            renderer = self.resume_renderer or self._default_resume_renderer()
            renderer_arguments["source_media_path"] = source_media_path
        rendered = renderer(**renderer_arguments)

        output_path = Path(str(rendered.get("output_path", ""))).resolve()
        manifest_path = Path(str(rendered.get("manifest_path", ""))).resolve()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("媒体函数未生成有效 MP4")
        if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
            raise RuntimeError("媒体函数未生成有效 manifest")
        relative_video = self._relative_project_path(output_path, project_id)
        relative_manifest = self._relative_project_path(manifest_path, project_id)
        poster_value = rendered.get("poster_path")
        relative_poster = (
            self._relative_project_path(Path(str(poster_value)).resolve(), project_id)
            if poster_value and Path(str(poster_value)).is_file()
            else None
        )
        video_sha256 = sha256_file(output_path)
        reported_sha256 = rendered.get("sha256")
        if reported_sha256 and reported_sha256 != video_sha256:
            raise RuntimeError("媒体返回的 SHA-256 与实际输出不一致")
        manifest_sha256 = sha256_file(manifest_path)
        validation = rendered.get("validation")
        if not isinstance(validation, dict):
            raise RuntimeError("媒体返回结果缺少 ffprobe validation")
        try:
            encoded_value = validation.get("encoded_duration_seconds")
            if encoded_value is None:
                encoded_value = validation["duration_seconds"]
            encoded_duration_seconds = float(encoded_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("媒体 validation 缺少有效编码时长") from exc
        planned_duration_seconds = float(
            validation.get(
                "planned_duration_seconds", prepared.planned_duration_seconds
            )
        )
        duration_delta_seconds = float(
            validation.get(
                "duration_delta_seconds",
                encoded_duration_seconds - planned_duration_seconds,
            )
        )
        tolerance_value = validation.get("duration_tolerance_seconds")
        duration_tolerance_seconds = (
            float(tolerance_value) if tolerance_value is not None else None
        )
        duration_validation = str(
            validation.get("duration_validation", "not_reported")
        )

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
                    **recovery_trace,
                },
            )
            manifest_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="MANIFEST",
                file_path=relative_manifest,
                sha256=manifest_sha256,
                metadata_json={"job_id": job_id, **recovery_trace},
            )
            export = crud.create_export(
                session,
                project_id=project_id,
                job_id=job_id,
                file_path=relative_video,
                manifest_path=relative_manifest,
                duration_seconds=encoded_duration_seconds,
                sha256=video_sha256,
            )
            result_json = {
                "export_id": export.id,
                "video_asset_id": video_asset.id,
                "manifest_asset_id": manifest_asset.id,
                "video_path": relative_video,
                "manifest_path": relative_manifest,
                "duration_seconds": encoded_duration_seconds,
                "planned_duration_seconds": planned_duration_seconds,
                "encoded_duration_seconds": encoded_duration_seconds,
                "duration_delta_seconds": duration_delta_seconds,
                "duration_tolerance_seconds": duration_tolerance_seconds,
                "duration_validation": duration_validation,
                "sha256": video_sha256,
                "validation": validation,
                "provider_id": provider_id,
                "script_provider": prepared.script_provider,
                "script_json": prepared.script_json,
                "script_source_type": prepared.script_source_type,
                "script_model_id": prepared.script_trace.get(
                    "model", "mock-script.v1"
                ),
                "script_trace": prepared.script_trace,
                "script_validation_warnings": prepared.script_validation_warnings,
                "desired_shot_count": prepared.desired_shot_count,
                "actual_shot_count": prepared.actual_shot_count,
                "story_char_count": prepared.story_char_count,
                "repair_used": prepared.repair_used,
                "duration_normalization": prepared.duration_normalization,
                "image_provider": "mock",
                "audio_provider": "mock",
                "video_source_type": "DETERMINISTIC_FALLBACK",
                "source_type": "DETERMINISTIC_FALLBACK",
                "media_reused": bool(rendered.get("media_reused")),
                "reencoded": bool(rendered.get("reencoded", source_media_path is None)),
                "motion_preset": request_snapshot.get("motion_preset"),
                "background_audio": request_snapshot.get("background_audio"),
                "poster_path": relative_poster,
                **recovery_trace,
                "video_url": f"/api/projects/{project_id}/exports/{export.id}/video",
                "download_url": (
                    f"/api/projects/{project_id}/exports/{export.id}/video?download=true"
                ),
                "manifest_url": f"/api/projects/{project_id}/exports/{export.id}/manifest",
                "poster_url": f"/api/projects/{project_id}/exports/{export.id}/poster",
            }
            crud.mark_job_succeeded(session, job=job, result_json=result_json)
            session.commit()
        print(f"[worker] job={job_id} SUCCEEDED export={export.id}", flush=True)

    def _process_media_rerender_job(
        self,
        *,
        job_id: str,
        project_id: str,
        project_title: str,
        project_script_json: dict[str, Any] | None,
        request_snapshot: dict[str, Any],
    ) -> None:
        """Reuse validated ScriptV1, PNG and WAV assets; invoke FFmpeg only."""

        if request_snapshot.get("media_only") is not True:
            raise MediaRerenderJobError(
                "MEDIA_RENDER_FAILED", "媒体重合成 Job 缺少 media_only 快照标记。"
            )
        for provider_name in ("script", "image", "audio"):
            if (
                request_snapshot.get(f"{provider_name}_provider") != "reused"
                or request_snapshot.get(f"{provider_name}_provider_calls_expected") != 0
            ):
                raise MediaRerenderJobError(
                    "MEDIA_RENDER_FAILED",
                    f"媒体重合成禁止调用 {provider_name} Provider。",
                )
        try:
            script = ScriptV1.model_validate(project_script_json)
        except Exception as exc:
            raise MediaRerenderJobError(
                "SOURCE_SCRIPT_NOT_FOUND", "项目当前 ScriptV1 缺失或无法校验。"
            ) from exc

        source_script_job_id = str(request_snapshot.get("source_script_job_id") or "")
        source_image_job_id = str(request_snapshot.get("source_image_job_id") or "")
        source_audio_job_id = str(request_snapshot.get("source_audio_job_id") or "")
        with self.database.session() as session:
            source_script_job = crud.get_job(session, source_script_job_id)
            source_image_job = crud.get_job(session, source_image_job_id)
            source_audio_job = crud.get_job(session, source_audio_job_id)
            if source_script_job is None:
                raise MediaRerenderJobError(
                    "SOURCE_SCRIPT_NOT_FOUND", "来源 ScriptV1 Job 不存在。"
                )
            if source_image_job is None:
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_JOB_NOT_FOUND", "来源真实图片 Job 不存在。"
                )
            if source_audio_job is None:
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_JOB_NOT_FOUND", "来源真实旁白 Job 不存在。"
                )
            if any(
                item.project_id != project_id
                for item in (source_script_job, source_image_job, source_audio_job)
            ):
                raise MediaRerenderJobError(
                    "SOURCE_JOB_PROJECT_MISMATCH",
                    "剧本、图片和旁白来源必须属于当前项目。",
                )
            if source_script_job.status != JobStatus.SUCCEEDED:
                raise MediaRerenderJobError(
                    "SOURCE_SCRIPT_NOT_FOUND", "来源 ScriptV1 Job 尚未成功。"
                )
            if (
                source_image_job.status != JobStatus.SUCCEEDED
                or source_image_job.job_type != REAL_IMAGE_JOB_TYPE
                or source_image_job.provider_id != REAL_IMAGE_PROVIDER_ID
            ):
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_JOB_NOT_FOUND", "来源真实图片 Job 无效或尚未成功。"
                )
            if (
                source_audio_job.status != JobStatus.SUCCEEDED
                or source_audio_job.job_type != REAL_AUDIO_JOB_TYPE
                or source_audio_job.provider_id != REAL_AUDIO_PROVIDER_ID
            ):
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_JOB_NOT_FOUND", "来源真实旁白 Job 无效或尚未成功。"
                )
            image_result = copy.deepcopy(source_image_job.result_json or {})
            audio_result = copy.deepcopy(source_audio_job.result_json or {})

        if (
            audio_result.get("source_script_job_id") != source_script_job_id
            or audio_result.get("source_image_job_id") != source_image_job_id
        ):
            raise MediaRerenderJobError(
                "SOURCE_JOB_PROJECT_MISMATCH", "来源旁白 Job 的上游追溯与快照不一致。"
            )
        if (
            image_result.get("mock_image_fallback") is not False
            or audio_result.get("mock_audio_fallback") is not False
        ):
            raise MediaRerenderJobError(
                "MEDIA_RENDER_FAILED", "来源 Job 含 Mock 回退，已拒绝媒体重合成。"
            )

        raw_images = image_result.get("image_shots")
        raw_audio = audio_result.get("audio_shots")
        timing_plan = audio_result.get("timing_plan")
        if not isinstance(raw_images, list) or len(raw_images) != len(script.shots):
            raise MediaRerenderJobError(
                "SOURCE_IMAGE_MISSING", "来源 Job 缺少完整的逐镜头真实 PNG。"
            )
        if not isinstance(raw_audio, list) or len(raw_audio) != len(script.shots):
            raise MediaRerenderJobError(
                "SOURCE_AUDIO_MISSING", "来源 Job 缺少完整的逐镜头真实 WAV。"
            )
        if not isinstance(timing_plan, dict):
            raise MediaRerenderJobError(
                "MEDIA_RENDER_FAILED", "来源真实旁白 Job 缺少 TimingPlan。"
            )

        keyframes = self._media_rerender_keyframes(
            project_id=project_id,
            script=script,
            source_images=raw_images,
        )
        generated_assets = self._media_rerender_audio_assets(
            project_id=project_id,
            script=script,
            source_audio=raw_audio,
        )
        job_dir = self.settings.project_dir(project_id) / "jobs" / job_id
        timing_plan_path = job_dir / "timing_plan.json"
        atomic_json(timing_plan_path, timing_plan)

        initial_result = {
            "stage": "MEDIA_RENDER",
            "media_only": True,
            "parent_job_id": source_audio_job_id,
            "source_script_job_id": source_script_job_id,
            "source_image_job_id": source_image_job_id,
            "source_audio_job_id": source_audio_job_id,
            "script_provider": "reused",
            "image_provider": "reused",
            "audio_provider": "reused",
            "source_script_provider": request_snapshot.get("source_script_provider"),
            "source_image_provider": REAL_IMAGE_PROVIDER_ID,
            "source_audio_provider": REAL_AUDIO_PROVIDER_ID,
            "script_provider_calls": 0,
            "image_provider_calls": 0,
            "audio_provider_calls": 0,
            "mock_image_fallback": False,
            "mock_audio_fallback": False,
            "audio_shots": raw_audio,
            "audio_completed_count": len(generated_assets),
            "audio_total_count": len(generated_assets),
            "timing_plan": timing_plan,
            "motion_preset": request_snapshot.get("motion_preset"),
            "background_audio": request_snapshot.get("background_audio"),
            "retry_of_job_id": request_snapshot.get("retry_of_job_id"),
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"媒体重合成准备完成时 Job 不再是 RUNNING：{job_id}")
            job.result_json = initial_result
            crud.set_job_progress(session, job, 20)
            session.commit()

        generation_context = {
            "generation_job_id": job_id,
            "job_type": MEDIA_RERENDER_JOB_TYPE,
            "media_only": True,
            "parent_job_id": source_audio_job_id,
            "source_jobs": {
                "script": source_script_job_id,
                "image": source_image_job_id,
                "audio": source_audio_job_id,
            },
            "reused_providers": {
                "script_provider": "reused",
                "image_provider": "reused",
                "audio_provider": "reused",
            },
            "provider_calls": {"script": 0, "image": 0, "audio": 0},
            "providers": {
                "script_provider": "reused",
                "source_script_provider": request_snapshot.get(
                    "source_script_provider"
                ),
                "image_provider": REAL_IMAGE_PROVIDER_ID,
                "image_source_type": "REUSED_REAL_LOCAL_MODEL",
                "audio_provider": REAL_AUDIO_PROVIDER_ID,
                "audio_source_type": REAL_AUDIO_SOURCE_TYPE,
                "video_source_type": "MEDIA_ONLY_RERENDER_FFMPEG",
            },
            "timing_plan": timing_plan,
            "request": request_snapshot,
            "mock_image_fallback": False,
            "mock_audio_fallback": False,
        }
        output_dir = self.settings.project_dir(project_id) / "exports" / job_id
        renderer = self.real_audio_renderer or self._default_real_audio_renderer()
        timing_options = request_snapshot.get("timing_options")
        max_duration = (
            float(timing_options.get("max_total_duration_seconds", 60.0))
            if isinstance(timing_options, dict)
            else float(timing_plan.get("max_total_duration_seconds", 60.0))
        )
        try:
            rendered = renderer(
                root=self.settings.root_dir,
                project_id=project_id,
                project_title=project_title,
                shots=self._real_audio_media_shots(script),
                keyframes=keyframes,
                audio_assets=[asset.as_dict() for asset in generated_assets],
                timing_plan=timing_plan,
                timing_plan_path=timing_plan_path,
                output_dir=output_dir,
                output_filename=f"short_{job_id}.mp4",
                provider_id=REAL_AUDIO_PROVIDER_ID,
                generation_context=generation_context,
                max_total_duration_seconds=max_duration,
                **self._renderer_media_options(request_snapshot, project_id),
                progress_callback=lambda progress: self._set_running_job_progress(
                    job_id, max(21, min(95, int(progress)))
                ),
            )
        except MediaRerenderJobError:
            raise
        except Exception as exc:
            raise MediaRerenderJobError(
                "MEDIA_RENDER_FAILED",
                f"FFmpeg 媒体重合成失败：{str(exc)[:500]}",
                stage="MEDIA_RENDER",
                retryable=True,
            ) from exc

        self._finish_real_audio_job(
            job_id=job_id,
            project_id=project_id,
            request_snapshot=request_snapshot,
            generated_assets=generated_assets,
            rendered=rendered,
            timing_plan=timing_plan,
            timing_plan_path=timing_plan_path,
            source_script_job_id=source_script_job_id,
            source_script_provider=str(
                request_snapshot.get("source_script_provider") or "reused"
            ),
            source_image_job_id=source_image_job_id,
            source_image_provider=REAL_IMAGE_PROVIDER_ID,
            audio_generation_total_seconds=0.0,
            provider_report={
                "model_load_count": 0,
                "sequential_generation": True,
                "max_audio_concurrency": 0,
            },
            gpu_observation={},
        )

    def _media_rerender_keyframes(
        self,
        *,
        project_id: str,
        script: ScriptV1,
        source_images: list[Any],
    ) -> list[dict[str, Any]]:
        tools = resolve_media_tools()
        expected_ids = {shot.id for shot in script.shots}
        by_id: dict[str, dict[str, Any]] = {}
        for raw in source_images:
            if not isinstance(raw, dict) or not isinstance(raw.get("shot_id"), str):
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_MISSING", "来源 PNG 追溯字段无效。"
                )
            item = dict(raw)
            shot_id = item["shot_id"]
            try:
                path = self._stored_project_path(str(item["image_path"]), project_id)
                expected_sha = str(item["image_sha256"]).lower()
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_MISSING", f"来源 PNG {shot_id} 路径无效。"
                ) from exc
            if (
                shot_id not in expected_ids
                or path.suffix.lower() != ".png"
                or not path.is_file()
                or sha256_file(path) != expected_sha
            ):
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_MISSING", f"来源 PNG {shot_id} 缺失或哈希不匹配。"
                )
            try:
                probe = ffprobe_json(tools, path)
                streams = probe.get("streams")
                video = [
                    value
                    for value in streams
                    if isinstance(value, dict) and value.get("codec_type") == "video"
                ] if isinstance(streams, list) else []
                if len(video) != 1 or video[0].get("codec_name") != "png":
                    raise MediaToolError("不是单流 PNG")
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
            except MediaToolError as exc:
                raise MediaRerenderJobError(
                    "SOURCE_IMAGE_MISSING", f"来源 PNG {shot_id} 无法解码。"
                ) from exc
            item["image_path"] = str(path)
            for trace_field in ("workflow_path", "trace_path"):
                trace_value = item.get(trace_field)
                if isinstance(trace_value, str) and trace_value:
                    item[trace_field] = str(
                        self._stored_project_path(trace_value, project_id)
                    )
            by_id[shot_id] = item
        if set(by_id) != expected_ids:
            raise MediaRerenderJobError(
                "SOURCE_IMAGE_MISSING", "来源 PNG 镜头集合与 ScriptV1 不一致。"
            )
        return [by_id[shot.id] for shot in script.shots]

    def _media_rerender_audio_assets(
        self,
        *,
        project_id: str,
        script: ScriptV1,
        source_audio: list[Any],
    ) -> tuple[GeneratedAudioAsset, ...]:
        tools = resolve_media_tools()
        expected_ids = {shot.id for shot in script.shots}
        by_id: dict[str, GeneratedAudioAsset] = {}
        for raw in source_audio:
            if not isinstance(raw, dict) or not isinstance(raw.get("shot_id"), str):
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_MISSING", "来源 WAV 追溯字段无效。"
                )
            shot_id = raw["shot_id"]
            try:
                path = self._stored_project_path(str(raw["audio_path"]), project_id)
                trace_path = self._stored_project_path(
                    str(raw["trace_path"]), project_id
                )
                expected_sha = str(raw["audio_sha256"]).lower()
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_MISSING", f"来源 WAV {shot_id} 路径无效。"
                ) from exc
            if (
                shot_id not in expected_ids
                or path.suffix.lower() != ".wav"
                or not path.is_file()
                or not trace_path.is_file()
                or sha256_file(path) != expected_sha
            ):
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_MISSING", f"来源 WAV {shot_id} 缺失或哈希不匹配。"
                )
            try:
                inspected = inspect_pcm16_wav(path)
                run_command(
                    [
                        tools.ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        path,
                        "-map",
                        "0:a:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    timeout_seconds=120,
                )
            except (AudioValidationError, MediaToolError) as exc:
                raise MediaRerenderJobError(
                    "AUDIO_DECODE_FAILED", f"来源 WAV {shot_id} 无法完整解码。"
                ) from exc
            try:
                asset = GeneratedAudioAsset(
                    provider_id=REAL_AUDIO_PROVIDER_ID,
                    model_id=str(raw["model_id"]),
                    model_revision=str(raw["model_revision"]),
                    model_sha256=str(raw["model_sha256"]),
                    shot_id=shot_id,
                    audio_path=path,
                    trace_path=trace_path,
                    text=str(raw["text"]),
                    speaker=str(raw["speaker"]),
                    language=str(raw["language"]),
                    seed=int(raw["seed"]),
                    sample_rate=int(inspected["sample_rate"]),
                    channels=int(inspected["channels"]),
                    sample_width_bytes=int(inspected["sample_width_bytes"]),
                    duration_seconds=float(inspected["duration_seconds"]),
                    generation_seconds=float(raw["generation_seconds"]),
                    real_time_factor=float(raw["real_time_factor"]),
                    peak_amplitude=float(inspected["peak_amplitude"]),
                    rms=float(inspected["rms"]),
                    audio_sha256=str(inspected["sha256"]),
                    warnings=tuple(str(value) for value in raw.get("warnings", [])),
                    reused=True,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaRerenderJobError(
                    "SOURCE_AUDIO_MISSING", f"来源 WAV {shot_id} 追溯元数据无效。"
                ) from exc
            by_id[shot_id] = asset
        if set(by_id) != expected_ids:
            raise MediaRerenderJobError(
                "SOURCE_AUDIO_MISSING", "来源 WAV 镜头集合与 ScriptV1 不一致。"
            )
        return tuple(by_id[shot.id] for shot in script.shots)

    def _process_real_audio_job(
        self,
        *,
        job_id: str,
        project_id: str,
        project_title: str,
        project_story: str,
        project_script_json: dict[str, Any] | None,
        request_snapshot: dict[str, Any],
    ) -> None:
        """只复用 ScriptV1 与真实 PNG，顺序生成真实旁白后进入 FFmpeg。"""

        if request_snapshot.get("script_provider_calls_expected") != 0:
            raise RealAudioJobError(
                code="SOURCE_REUSE_INVALID",
                stage="SOURCE_REUSE",
                summary="真实旁白 Job 必须明确记录文本 Provider 调用次数为 0。",
                retryable=False,
            )
        if request_snapshot.get("image_provider_calls_expected") != 0:
            raise RealAudioJobError(
                code="SOURCE_REUSE_INVALID",
                stage="SOURCE_REUSE",
                summary="真实旁白 Job 必须明确记录图像 Provider 调用次数为 0。",
                retryable=False,
            )
        script, source_payload = load_audio_source_snapshot(
            self.settings,
            project_id=project_id,
            audio_job_id=job_id,
            request_snapshot=request_snapshot,
        )
        script_json = script.model_dump(mode="json")
        if project_script_json != script_json:
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_STALE",
                stage="SOURCE_REUSE",
                summary="项目当前剧本已变化，拒绝用旧 ScriptV1 生成旁白。",
                retryable=False,
            )
        if request_snapshot.get("story_char_count") != len(project_story.strip()):
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_STALE",
                stage="SOURCE_REUSE",
                summary="项目故事与真实旁白 Job 的请求快照不一致。",
                retryable=False,
            )
        if request_snapshot.get("actual_shot_count") != len(script.shots):
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_INVALID",
                stage="SOURCE_REUSE",
                summary="ScriptV1 镜头数与真实旁白 Job 快照不一致。",
                retryable=False,
            )

        source_script_job_id = str(request_snapshot.get("source_script_job_id") or "")
        source_image_job_id = str(request_snapshot.get("source_image_job_id") or "")
        source_script_provider = str(
            request_snapshot.get("source_script_provider") or "unknown"
        )
        source_image_provider = str(
            request_snapshot.get("source_image_provider") or ""
        )
        if source_payload.get("source_script_job_id") != source_script_job_id:
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_INVALID",
                stage="SOURCE_REUSE",
                summary="来源快照的 Script Job 与请求不一致。",
                retryable=False,
            )
        if source_payload.get("source_image_job_id") != source_image_job_id:
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_INVALID",
                stage="SOURCE_REUSE",
                summary="来源快照的 Image Job 与请求不一致。",
                retryable=False,
            )
        if source_payload.get("source_image_provider") != source_image_provider:
            raise RealAudioJobError(
                code="AUDIO_SOURCE_SNAPSHOT_INVALID",
                stage="SOURCE_REUSE",
                summary="来源快照的 ImageProvider 与请求不一致。",
                retryable=False,
            )
        source_images = self._real_audio_source_images(
            project_id=project_id,
            script=script,
            source_payload=source_payload,
        )

        with self.database.session() as session:
            source_image_job = crud.get_job(session, source_image_job_id)
            if (
                source_image_job is None
                or source_image_job.project_id != project_id
                or source_image_job.job_type != REAL_IMAGE_JOB_TYPE
                or source_image_job.provider_id != source_image_provider
                or source_image_job.status != JobStatus.SUCCEEDED
            ):
                raise RealAudioJobError(
                    code="AUDIO_SOURCE_JOB_INVALID",
                    stage="SOURCE_REUSE",
                    summary="来源 M4-B 真实图像 Job 不存在、未成功或归属不匹配。",
                    retryable=False,
                )
            database_shots = crud.list_shots(session, project_id)
            if len(database_shots) != len(script.shots):
                raise RealAudioJobError(
                    code="AUDIO_SOURCE_SNAPSHOT_STALE",
                    stage="SOURCE_REUSE",
                    summary="数据库镜头与复用 ScriptV1 数量不一致。",
                    retryable=False,
                )
            database_shot_ids: dict[int, str] = {}
            for script_shot, database_shot in zip(
                script.shots, database_shots, strict=True
            ):
                if (
                    database_shot.shot_index != script_shot.index
                    or database_shot.title != script_shot.title
                    or database_shot.visual_description
                    != script_shot.visual_description
                    or database_shot.narration != script_shot.narration
                    or abs(
                        float(database_shot.duration_seconds)
                        - float(script_shot.duration_seconds)
                    )
                    > 1e-6
                ):
                    raise RealAudioJobError(
                        code="AUDIO_SOURCE_SNAPSHOT_STALE",
                        stage="SOURCE_REUSE",
                        summary="数据库镜头内容与复用 ScriptV1 不一致。",
                        failed_shot_id=script_shot.id,
                        failed_shot_index=script_shot.index,
                        retryable=False,
                    )
                database_shot_ids[script_shot.index] = database_shot.id

        options = self._real_audio_options(request_snapshot)
        provider = self._real_audio_provider()
        if provider.provider_id != REAL_AUDIO_PROVIDER_ID:
            raise RealAudioJobError(
                code="AUDIO_PROVIDER_SNAPSHOT_MISMATCH",
                stage="AUDIO_GENERATION",
                summary="注入的 AudioProvider 与 Job 请求快照不一致。",
                retryable=False,
            )
        audio_dir = (
            self.settings.project_dir(project_id) / "jobs" / job_id / "audio"
        ).resolve()
        requests = tuple(
            AudioGenerationRequest(
                project_id=project_id,
                job_id=job_id,
                source_script_job_id=source_script_job_id,
                source_image_job_id=source_image_job_id,
                script=script,
                shot=shot,
                output_dir=audio_dir,
                options=options,
            )
            for shot in script.shots
        )
        reusable_assets = self._load_reusable_audio_assets(
            project_id=project_id,
            request_snapshot=request_snapshot,
        )
        initial_audio = [
            {
                "shot_id": shot.id,
                "shot_index": shot.index,
                "title": shot.title,
                "narration": shot.narration,
                "status": "PENDING",
                "provider_id": REAL_AUDIO_PROVIDER_ID,
                "model_id": getattr(
                    provider, "model_id", self.settings.qwen_tts_model_id
                ),
                "model_revision": getattr(
                    provider,
                    "model_revision",
                    self.settings.qwen_tts_model_revision,
                ),
                "speaker": options.speaker,
                "language": options.language,
            }
            for shot in script.shots
        ]
        initial_result = {
            "stage": "AUDIO_GENERATION",
            "parent_job_id": request_snapshot.get("parent_job_id"),
            "source_script_job_id": source_script_job_id,
            "source_image_job_id": source_image_job_id,
            "script_provider": "reused",
            "source_script_provider": source_script_provider,
            "script_provider_calls": 0,
            "image_provider": "reused",
            "source_image_provider": source_image_provider,
            "image_provider_calls": 0,
            "audio_provider": REAL_AUDIO_PROVIDER_ID,
            "audio_model_id": getattr(
                provider, "model_id", self.settings.qwen_tts_model_id
            ),
            "audio_model_revision": getattr(
                provider,
                "model_revision",
                self.settings.qwen_tts_model_revision,
            ),
            "audio_model_license": getattr(
                provider, "model_license", self.settings.qwen_tts_model_license
            ),
            "speaker": options.speaker,
            "language": options.language,
            "script_json": script_json,
            "actual_shot_count": len(script.shots),
            "story_char_count": len(project_story.strip()),
            "source_planned_duration_seconds": round(
                sum(float(shot.duration_seconds) for shot in script.shots), 6
            ),
            "audio_options": options.as_dict(),
            "audio_shots": initial_audio,
            "audio_completed_count": 0,
            "audio_total_count": len(script.shots),
            "retry_of_job_id": request_snapshot.get("retry_of_job_id"),
            "resume_audio_from_job_id": request_snapshot.get(
                "resume_audio_from_job_id"
            ),
            "mock_audio_fallback": False,
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"真实旁白准备完成时 Job 不再是 RUNNING：{job_id}")
            crud.set_job_progress(session, job, 5)
            job.result_json = initial_result
            session.commit()

        handoff = audio_gpu_handoff_status(self.settings)
        if handoff["conflict"]:
            raise RealAudioJobError(
                code="GPU_HANDOFF_REQUIRED",
                stage="GPU_HANDOFF_REQUIRED",
                summary="真实旁白生成前检测到模型服务或高显存占用，需要先完成 GPU 交接。",
                total_audio_count=len(script.shots),
                requires_qwen_shutdown=bool(
                    handoff.get("llama_port_listening")
                    or handoff.get("llama_process_detected")
                ),
                requires_comfyui_shutdown=bool(
                    handoff.get("comfyui_port_listening")
                    or handoff.get("comfyui_process_detected")
                ),
                suggestions=[
                    "确认 8081 与 8188 已释放后手动重试。",
                    (
                        "关闭其他高显存程序后重试。"
                        if handoff.get("gpu_memory_conflict")
                        else "确认没有其他高显存推理进程。"
                    ),
                    "平台不会终止用户启动的外部模型进程。",
                ],
            )

        monitor = GpuMemoryMonitor()
        monitor.start()
        audio_started = time.monotonic()
        try:
            generated_assets = provider.generate_batch(
                requests=requests,
                reusable_assets=reusable_assets,
                progress_callback=lambda completed, total, asset: (
                    self._record_real_audio_progress(
                        job_id=job_id,
                        project_id=project_id,
                        database_shot_id=database_shot_ids[
                            next(
                                shot.index
                                for shot in script.shots
                                if shot.id == asset.shot_id
                            )
                        ],
                        shot_index=next(
                            shot.index
                            for shot in script.shots
                            if shot.id == asset.shot_id
                        ),
                        total=total,
                        asset=asset,
                    )
                ),
            )
        except Exception:
            monitor.stop()
            self._record_gpu_observation(job_id, monitor.summary())
            raise
        monitor.stop()
        gpu_observation = monitor.summary()
        self._record_gpu_observation(job_id, gpu_observation)
        audio_generation_total_seconds = round(
            time.monotonic() - audio_started, 3
        )
        if (
            len(generated_assets) != len(script.shots)
            or [item.shot_id for item in generated_assets]
            != [shot.id for shot in script.shots]
        ):
            raise RealAudioJobError(
                code="AUDIO_OUTPUT_MISSING",
                stage="AUDIO_GENERATION",
                summary="AudioProvider 返回的旁白数量或顺序不完整。",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(script.shots),
            )

        timing_options = self._real_audio_timing_options(request_snapshot)
        timing_plan = build_media_timing_plan(
            script=script,
            audio_assets=generated_assets,
            fps=timing_options["fps"],
            lead_in_seconds=timing_options["lead_in_seconds"],
            lead_out_seconds=timing_options["lead_out_seconds"],
            max_total_duration_seconds=timing_options[
                "max_total_duration_seconds"
            ],
        )
        job_trace_dir = audio_dir.parent
        timing_plan_path = job_trace_dir / "timing_plan.json"
        atomic_json(timing_plan_path, timing_plan)
        report_path = job_trace_dir / "audio_generation_report.json"
        provider_report = getattr(provider, "last_run_report", None)
        if not isinstance(provider_report, dict):
            provider_report = {}
            if report_path.is_file():
                try:
                    loaded = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        provider_report = loaded
                except (OSError, json.JSONDecodeError):
                    provider_report = {}
        provider_gpu_allocator_observation = provider_report.get(
            "gpu_memory_observed"
        )
        model_load_count = provider_report.get("model_load_count")
        generated_count = sum(not item.reused for item in generated_assets)
        reused_count = sum(item.reused for item in generated_assets)

        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"旁白生成完成时 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            current.update(
                {
                    "stage": "MEDIA_RENDER",
                    "audio_generation_total_seconds": (
                        audio_generation_total_seconds
                    ),
                    "audio_generation_report_path": self._relative_project_path(
                        report_path, project_id
                    )
                    if report_path.is_file()
                    else None,
                    "audio_completed_count": len(generated_assets),
                    "audio_total_count": len(generated_assets),
                    "audio_generated_count": generated_count,
                    "audio_reused_count": reused_count,
                    "model_load_count": model_load_count,
                    "sequential_generation": provider_report.get(
                        "sequential_generation", True
                    ),
                    "max_audio_concurrency": provider_report.get(
                        "max_audio_concurrency", 1
                    ),
                    "gpu_memory_observed": gpu_observation,
                    "provider_gpu_allocator_observed": (
                        provider_gpu_allocator_observation
                    ),
                    "timing_plan_path": self._relative_project_path(
                        timing_plan_path, project_id
                    ),
                    "timing_plan": timing_plan,
                    "rendered_planned_duration_seconds": timing_plan[
                        "rendered_total_duration_seconds"
                    ],
                }
            )
            job.result_json = current
            crud.set_job_progress(session, job, 65)
            session.commit()

        source_trace = source_payload.get("source_trace")
        if not isinstance(source_trace, dict):
            source_trace = {}
        generation_context = {
            "generation_job_id": job_id,
            "job_type": REAL_AUDIO_JOB_TYPE,
            "request": request_snapshot,
            "parent_job_id": request_snapshot.get("parent_job_id"),
            "source_script_job_id": source_script_job_id,
            "source_image_job_id": source_image_job_id,
            "script": {
                "schema_version": script.schema_version,
                "provider_id": "reused",
                "source_script_provider": source_script_provider,
                "source_script_job_id": source_script_job_id,
                "script_provider_calls": 0,
            },
            "providers": {
                "script_provider": "reused",
                "source_script_provider": source_script_provider,
                "script_source_type": "REUSED_VALIDATED_SCRIPT",
                "image_provider": source_image_provider,
                "image_source_type": "REUSED_REAL_LOCAL_MODEL",
                "image_provider_calls": 0,
                "audio_provider": REAL_AUDIO_PROVIDER_ID,
                "audio_model_id": getattr(
                    provider, "model_id", self.settings.qwen_tts_model_id
                ),
                "audio_model_revision": getattr(
                    provider,
                    "model_revision",
                    self.settings.qwen_tts_model_revision,
                ),
                "audio_model_license": getattr(
                    provider,
                    "model_license",
                    self.settings.qwen_tts_model_license,
                ),
                "audio_source_type": REAL_AUDIO_SOURCE_TYPE,
                "video_source_type": "REAL_IMAGE_REAL_TTS_FFMPEG_MOTION",
            },
            "source_trace": source_trace,
            "audio_generation_report": self._relative_project_path(
                report_path, project_id
            )
            if report_path.is_file()
            else None,
            "timing_plan_path": self._relative_project_path(
                timing_plan_path, project_id
            ),
            "timing_plan": timing_plan,
            "audio_generation_total_seconds": audio_generation_total_seconds,
            "model_load_count": model_load_count,
            "sequential_generation": provider_report.get(
                "sequential_generation", True
            ),
            "max_audio_concurrency": provider_report.get(
                "max_audio_concurrency", 1
            ),
            "gpu_memory_observed": gpu_observation,
            "provider_gpu_allocator_observed": provider_gpu_allocator_observation,
            "mock_audio_fallback": False,
        }
        media_shots = self._real_audio_media_shots(script)
        output_dir = self.settings.project_dir(project_id) / "exports" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        renderer = self.real_audio_renderer or self._default_real_audio_renderer()
        try:
            rendered = renderer(
                root=self.settings.root_dir,
                project_id=project_id,
                project_title=project_title,
                shots=media_shots,
                keyframes=source_images,
                audio_assets=[asset.as_dict() for asset in generated_assets],
                timing_plan=timing_plan,
                timing_plan_path=timing_plan_path,
                output_dir=output_dir,
                output_filename=f"short_{job_id}.mp4",
                provider_id=REAL_AUDIO_PROVIDER_ID,
                generation_context=generation_context,
                max_total_duration_seconds=timing_options[
                    "max_total_duration_seconds"
                ],
                **self._renderer_media_options(request_snapshot, project_id),
                progress_callback=lambda progress: self._set_running_job_progress(
                    job_id, max(66, min(95, int(progress)))
                ),
            )
        except RealAudioJobError:
            raise
        except Exception as exc:
            message = str(exc)
            if "AUDIO_TIMING_EXCEEDS_LIMIT" in message:
                raise RealAudioJobError(
                    code="AUDIO_TIMING_EXCEEDS_LIMIT",
                    stage="AUDIO_TIMING",
                    summary=message[:500],
                    completed_audio_count=len(generated_assets),
                    total_audio_count=len(generated_assets),
                    retryable=False,
                ) from exc
            raise RealAudioJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary=f"真实旁白媒体渲染失败：{message[:500]}",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(generated_assets),
                log_paths={
                    "audio_generation_report": str(report_path),
                    "tts_stdout": str(job_trace_dir / "tts.stdout.log"),
                    "tts_stderr": str(job_trace_dir / "tts.stderr.log"),
                    "timing_plan": str(timing_plan_path),
                },
                suggestions=["检查 FFmpeg/FFprobe 与媒体追溯后手动重试。"],
            ) from exc

        self._finish_real_audio_job(
            job_id=job_id,
            project_id=project_id,
            request_snapshot=request_snapshot,
            generated_assets=generated_assets,
            rendered=rendered,
            timing_plan=timing_plan,
            timing_plan_path=timing_plan_path,
            source_script_job_id=source_script_job_id,
            source_script_provider=source_script_provider,
            source_image_job_id=source_image_job_id,
            source_image_provider=source_image_provider,
            audio_generation_total_seconds=audio_generation_total_seconds,
            provider_report=provider_report,
            gpu_observation=gpu_observation,
        )

    def _real_audio_options(
        self, request_snapshot: dict[str, Any]
    ) -> AudioGenerationOptions:
        payload = request_snapshot.get("audio_options")
        if not isinstance(payload, dict):
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary="真实旁白 Job 缺少 audio_options 快照。",
                retryable=False,
            )
        if payload.get("speaker") != request_snapshot.get("speaker"):
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary="audio_options.speaker 与 Job 顶层快照不一致。",
                retryable=False,
            )
        if payload.get("language") != request_snapshot.get("language"):
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary="audio_options.language 与 Job 顶层快照不一致。",
                retryable=False,
            )
        try:
            options = AudioGenerationOptions(
                speaker=str(payload["speaker"]),
                language=str(payload["language"]),
                base_seed=payload["base_seed"],
                model_load_timeout_seconds=float(
                    payload["model_load_timeout_seconds"]
                ),
                generation_timeout_seconds=float(
                    payload["generation_timeout_seconds"]
                ),
                job_timeout_seconds=float(payload["job_timeout_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary=f"真实旁白参数快照无法通过校验：{exc}",
                retryable=False,
            ) from exc
        if options.speaker not in {"Serena", "Vivian"}:
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary="M5-B 音色只允许 Serena 或 Vivian。",
                retryable=False,
            )
        if options.language != "Chinese":
            raise RealAudioJobError(
                code="AUDIO_OPTIONS_INVALID",
                stage="AUDIO_GENERATION",
                summary="M5-B language 必须为 Chinese。",
                retryable=False,
            )
        return options

    def _real_audio_timing_options(
        self, request_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        payload = request_snapshot.get("timing_options")
        if not isinstance(payload, dict):
            raise RealAudioJobError(
                code="AUDIO_TIMING_INVALID",
                stage="AUDIO_TIMING",
                summary="真实旁白 Job 缺少 timing_options 快照。",
                retryable=False,
            )
        try:
            fps = payload["fps"]
            lead_in = float(payload["lead_in_seconds"])
            lead_out = float(payload["lead_out_seconds"])
            maximum = float(payload["max_total_duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RealAudioJobError(
                code="AUDIO_TIMING_INVALID",
                stage="AUDIO_TIMING",
                summary=f"真实旁白时序参数无法读取：{exc}",
                retryable=False,
            ) from exc
        if type(fps) is not int or fps != 24:
            raise RealAudioJobError(
                code="AUDIO_TIMING_INVALID",
                stage="AUDIO_TIMING",
                summary="M5-B 媒体帧率快照必须为 24fps。",
                retryable=False,
            )
        if abs(lead_in - 0.20) > 1e-6 or abs(lead_out - 0.35) > 1e-6:
            raise RealAudioJobError(
                code="AUDIO_TIMING_INVALID",
                stage="AUDIO_TIMING",
                summary="M5-B 前导和尾部留白必须分别为 0.20 与 0.35 秒。",
                retryable=False,
            )
        if maximum <= 0:
            raise RealAudioJobError(
                code="AUDIO_TIMING_INVALID",
                stage="AUDIO_TIMING",
                summary="真实旁白渲染总时长上限必须大于 0。",
                retryable=False,
            )
        return {
            "fps": fps,
            "lead_in_seconds": lead_in,
            "lead_out_seconds": lead_out,
            "max_total_duration_seconds": maximum,
        }

    def _real_audio_provider(self) -> AudioProvider:
        if self.audio_provider_factory is not None:
            return self.audio_provider_factory(self.settings)
        return create_qwen3_tts_provider(self.settings)

    def _real_audio_source_images(
        self,
        *,
        project_id: str,
        script: ScriptV1,
        source_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_images = source_payload.get("source_images")
        if not isinstance(raw_images, list) or len(raw_images) != len(script.shots):
            raise RealAudioJobError(
                code="IMAGE_OUTPUT_MISSING",
                stage="SOURCE_REUSE",
                summary="真实旁白来源快照缺少完整真实 PNG。",
                retryable=False,
            )
        by_id: dict[str, dict[str, Any]] = {}
        for item in raw_images:
            if not isinstance(item, dict) or not isinstance(item.get("shot_id"), str):
                raise RealAudioJobError(
                    code="IMAGE_OUTPUT_MISSING",
                    stage="SOURCE_REUSE",
                    summary="来源真实 PNG 追溯格式无效。",
                    retryable=False,
                )
            payload = dict(item)
            shot_id = str(payload["shot_id"])
            if shot_id in by_id:
                raise RealAudioJobError(
                    code="IMAGE_OUTPUT_MISSING",
                    stage="SOURCE_REUSE",
                    summary=f"来源真实 PNG shot_id 重复：{shot_id}",
                    retryable=False,
                )
            try:
                image_path = self._stored_project_path(
                    str(payload["image_path"]), project_id
                )
                recorded_sha256 = str(payload["image_sha256"]).lower()
            except (KeyError, TypeError, ValueError) as exc:
                raise RealAudioJobError(
                    code="IMAGE_OUTPUT_MISSING",
                    stage="SOURCE_REUSE",
                    summary=f"来源真实 PNG {shot_id} 路径追溯无效。",
                    retryable=False,
                ) from exc
            if (
                not image_path.is_file()
                or image_path.stat().st_size <= 0
                or sha256_file(image_path) != recorded_sha256
            ):
                raise RealAudioJobError(
                    code="IMAGE_OUTPUT_MISSING",
                    stage="SOURCE_REUSE",
                    summary=f"来源真实 PNG {shot_id} 缺失或 SHA256 不匹配。",
                    failed_shot_id=shot_id,
                    retryable=False,
                )
            payload["image_path"] = str(image_path)
            for field in ("workflow_path", "trace_path"):
                value = payload.get(field)
                if isinstance(value, str) and value:
                    payload[field] = str(
                        self._stored_project_path(value, project_id)
                    )
            by_id[shot_id] = payload
        expected_ids = [shot.id for shot in script.shots]
        if set(by_id) != set(expected_ids):
            raise RealAudioJobError(
                code="IMAGE_OUTPUT_MISSING",
                stage="SOURCE_REUSE",
                summary="来源真实 PNG 镜头集合与 ScriptV1 不一致。",
                retryable=False,
            )
        return [by_id[shot_id] for shot_id in expected_ids]

    @staticmethod
    def _real_audio_media_shots(script: ScriptV1) -> list[dict[str, Any]]:
        return [
            {
                "shot_id": shot.id,
                "sequence_no": shot.index,
                "title": shot.title,
                "visual_description": shot.visual_description,
                "subtitle_text": shot.narration,
                "duration_seconds": float(shot.duration_seconds),
                "provider_id": "reused",
                "script_provider_id": "reused",
                "source_type": "REUSED_VALIDATED_SCRIPT",
                "generation_parameters": {
                    "scene_id": shot.scene_id,
                    "character_ids": list(shot.character_ids),
                    "camera": shot.camera,
                    "image_prompt": shot.image_prompt,
                    "negative_prompt": shot.negative_prompt,
                },
            }
            for shot in script.shots
        ]

    def _load_reusable_audio_assets(
        self,
        *,
        project_id: str,
        request_snapshot: dict[str, Any],
    ) -> tuple[GeneratedAudioAsset, ...]:
        source_job_id = request_snapshot.get("resume_audio_from_job_id")
        if not isinstance(source_job_id, str) or not source_job_id:
            return ()
        with self.database.session() as session:
            source_job = crud.get_job(session, source_job_id)
            if (
                source_job is None
                or source_job.project_id != project_id
                or source_job.job_type != REAL_AUDIO_JOB_TYPE
                or source_job.provider_id != REAL_AUDIO_PROVIDER_ID
            ):
                raise RealAudioJobError(
                    code="AUDIO_RESUME_INVALID",
                    stage="AUDIO_GENERATION",
                    summary="真实旁白重试来源 Job 不存在或来源不匹配。",
                    retryable=False,
                )
            source_result = dict(source_job.result_json or {})
        items = source_result.get("audio_shots")
        if not isinstance(items, list):
            return ()
        reusable: list[GeneratedAudioAsset] = []
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in {
                "SUCCEEDED",
                "REUSED",
            }:
                continue
            try:
                warnings = item.get("warnings", [])
                if not isinstance(warnings, list) or not all(
                    isinstance(value, str) for value in warnings
                ):
                    continue
                reusable.append(
                    GeneratedAudioAsset(
                        provider_id=str(item["provider_id"]),
                        model_id=str(item["model_id"]),
                        model_revision=str(item["model_revision"]),
                        model_sha256=str(item["model_sha256"]),
                        shot_id=str(item["shot_id"]),
                        audio_path=self._stored_project_path(
                            str(item["audio_path"]), project_id
                        ),
                        trace_path=self._stored_project_path(
                            str(item["trace_path"]), project_id
                        ),
                        text=str(item["text"]),
                        speaker=str(item["speaker"]),
                        language=str(item["language"]),
                        seed=int(item["seed"]),
                        sample_rate=int(item["sample_rate"]),
                        channels=int(item["channels"]),
                        sample_width_bytes=int(item["sample_width_bytes"]),
                        duration_seconds=float(item["duration_seconds"]),
                        generation_seconds=float(item["generation_seconds"]),
                        real_time_factor=float(item["real_time_factor"]),
                        peak_amplitude=float(item["peak_amplitude"]),
                        rms=float(item["rms"]),
                        audio_sha256=str(item["audio_sha256"]),
                        warnings=tuple(warnings),
                        reused=True,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(reusable)

    def _record_real_audio_progress(
        self,
        *,
        job_id: str,
        project_id: str,
        database_shot_id: str,
        shot_index: int,
        total: int,
        asset: GeneratedAudioAsset,
    ) -> None:
        relative_audio = self._relative_project_path(asset.audio_path, project_id)
        relative_trace = self._relative_project_path(asset.trace_path, project_id)
        serialized = {
            **asset.as_dict(),
            "shot_index": shot_index,
            "status": "REUSED" if asset.reused else "SUCCEEDED",
            "audio_path": relative_audio,
            "trace_path": relative_trace,
            "source_type": REAL_AUDIO_SOURCE_TYPE,
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"记录旁白时 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            audio_shots = current.get("audio_shots")
            if not isinstance(audio_shots, list):
                audio_shots = []
            existing = next(
                (
                    item
                    for item in audio_shots
                    if isinstance(item, dict)
                    and item.get("shot_id") == asset.shot_id
                    and item.get("audio_asset_id")
                ),
                None,
            )
            if existing is None:
                database_asset = crud.create_asset(
                    session,
                    project_id=project_id,
                    shot_id=database_shot_id,
                    asset_type="NARRATION_AUDIO",
                    provider_id=asset.provider_id,
                    source_type=REAL_AUDIO_SOURCE_TYPE,
                    file_path=relative_audio,
                    sha256=asset.audio_sha256,
                    metadata_json={
                        "job_id": job_id,
                        "shot_id": asset.shot_id,
                        "shot_index": shot_index,
                        "model_id": asset.model_id,
                        "model_revision": asset.model_revision,
                        "model_sha256": asset.model_sha256,
                        "speaker": asset.speaker,
                        "language": asset.language,
                        "seed": asset.seed,
                        "sample_rate": asset.sample_rate,
                        "channels": asset.channels,
                        "sample_width_bytes": asset.sample_width_bytes,
                        "duration_seconds": asset.duration_seconds,
                        "generation_seconds": asset.generation_seconds,
                        "real_time_factor": asset.real_time_factor,
                        "peak_amplitude": asset.peak_amplitude,
                        "rms": asset.rms,
                        "trace_path": relative_trace,
                        "reused": asset.reused,
                    },
                )
                serialized["audio_asset_id"] = database_asset.id
                serialized["audio_url"] = (
                    f"/api/projects/{project_id}/assets/"
                    f"{database_asset.id}/content"
                )
            else:
                serialized["audio_asset_id"] = existing.get("audio_asset_id")
                serialized["audio_url"] = existing.get("audio_url")
            by_id = {
                str(item.get("shot_id")): dict(item)
                for item in audio_shots
                if isinstance(item, dict) and item.get("shot_id")
            }
            by_id[asset.shot_id] = serialized
            ordered = sorted(
                by_id.values(), key=lambda item: int(item.get("shot_index", 999))
            )
            completed_count = sum(
                item.get("status") in {"SUCCEEDED", "REUSED"} for item in ordered
            )
            current.update(
                {
                    "stage": "AUDIO_GENERATION",
                    "audio_shots": ordered,
                    "audio_completed_count": completed_count,
                    "audio_total_count": total,
                    "current_shot_id": asset.shot_id,
                    "current_shot_index": shot_index,
                }
            )
            job.result_json = current
            crud.set_job_progress(session, job, 5 + int(55 * completed_count / total))
            session.commit()

    def _finish_real_audio_job(
        self,
        *,
        job_id: str,
        project_id: str,
        request_snapshot: dict[str, Any],
        generated_assets: tuple[GeneratedAudioAsset, ...],
        rendered: dict[str, Any],
        timing_plan: dict[str, Any],
        timing_plan_path: Path,
        source_script_job_id: str,
        source_script_provider: str,
        source_image_job_id: str,
        source_image_provider: str,
        audio_generation_total_seconds: float,
        provider_report: dict[str, Any],
        gpu_observation: dict[str, Any],
    ) -> None:
        output_path = Path(str(rendered.get("output_path", ""))).resolve()
        manifest_path = Path(str(rendered.get("manifest_path", ""))).resolve()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RealAudioJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层未生成有效真实旁白 MP4。",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(generated_assets),
            )
        if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
            raise RealAudioJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层未生成有效真实旁白 Manifest。",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(generated_assets),
            )
        validation = rendered.get("validation")
        if not isinstance(validation, dict):
            raise RealAudioJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层返回结果缺少 ffprobe 验证。",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(generated_assets),
            )
        relative_video = self._relative_project_path(output_path, project_id)
        relative_manifest = self._relative_project_path(manifest_path, project_id)
        relative_timing = self._relative_project_path(timing_plan_path, project_id)
        poster_value = rendered.get("poster_path")
        relative_poster = (
            self._relative_project_path(Path(str(poster_value)).resolve(), project_id)
            if poster_value and Path(str(poster_value)).is_file()
            else None
        )
        video_sha256 = sha256_file(output_path)
        if rendered.get("sha256") not in (None, video_sha256):
            raise RealAudioJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层返回的 MP4 SHA256 与文件实算值不一致。",
                completed_audio_count=len(generated_assets),
                total_audio_count=len(generated_assets),
            )
        manifest_sha256 = sha256_file(manifest_path)
        source_duration = float(timing_plan["source_total_duration_seconds"])
        rendered_duration = float(timing_plan["rendered_total_duration_seconds"])
        encoded_duration = float(
            validation.get(
                "encoded_duration_seconds", validation.get("duration_seconds")
            )
        )
        extension = round(rendered_duration - source_duration, 6)
        generated_count = sum(not item.reused for item in generated_assets)
        reused_count = sum(item.reused for item in generated_assets)

        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"媒体完成时真实旁白 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            video_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="EXPORT_VIDEO",
                provider_id=REAL_AUDIO_PROVIDER_ID,
                source_type="REAL_IMAGE_REAL_TTS_FFMPEG_MOTION",
                file_path=relative_video,
                sha256=video_sha256,
                metadata_json={"job_id": job_id, "validation": validation},
            )
            manifest_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="MANIFEST",
                provider_id=REAL_AUDIO_PROVIDER_ID,
                source_type="REAL_IMAGE_REAL_TTS_FFMPEG_MOTION",
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
                duration_seconds=encoded_duration,
                sha256=video_sha256,
            )
            current.update(
                {
                    "stage": "SUCCEEDED",
                    "export_id": export.id,
                    "video_asset_id": video_asset.id,
                    "manifest_asset_id": manifest_asset.id,
                    "video_path": relative_video,
                    "manifest_path": relative_manifest,
                    "timing_plan_path": relative_timing,
                    "sha256": video_sha256,
                    "validation": validation,
                    "source_planned_duration_seconds": source_duration,
                    "rendered_planned_duration_seconds": rendered_duration,
                    "planned_duration_seconds": rendered_duration,
                    "encoded_duration_seconds": encoded_duration,
                    "duration_seconds": encoded_duration,
                    "duration_delta_seconds": float(
                        validation.get(
                            "duration_delta_seconds",
                            encoded_duration - rendered_duration,
                        )
                    ),
                    "duration_tolerance_seconds": validation.get(
                        "duration_tolerance_seconds"
                    ),
                    "duration_validation": validation.get("duration_validation"),
                    "extended_by_seconds": extension,
                    "timing_plan": timing_plan,
                    "provider_id": REAL_AUDIO_PROVIDER_ID,
                    "script_provider": "reused",
                    "source_script_provider": source_script_provider,
                    "source_script_job_id": source_script_job_id,
                    "script_provider_calls": 0,
                    "image_provider": "reused",
                    "source_image_provider": source_image_provider,
                    "source_image_job_id": source_image_job_id,
                    "image_provider_calls": 0,
                    "audio_provider": REAL_AUDIO_PROVIDER_ID,
                    "audio_model_id": generated_assets[0].model_id,
                    "audio_model_revision": generated_assets[0].model_revision,
                    "audio_model_sha256": generated_assets[0].model_sha256,
                    "audio_model_license": self.settings.qwen_tts_model_license,
                    "speaker": generated_assets[0].speaker,
                    "language": generated_assets[0].language,
                    "source_type": "REAL_IMAGE_REAL_TTS_FFMPEG_MOTION",
                    "video_source_type": "REAL_IMAGE_REAL_TTS_FFMPEG_MOTION",
                    "audio_generation_total_seconds": (
                        audio_generation_total_seconds
                    ),
                    "audio_generated_count": generated_count,
                    "audio_reused_count": reused_count,
                    "model_load_count": provider_report.get("model_load_count"),
                    "sequential_generation": provider_report.get(
                        "sequential_generation", True
                    ),
                    "max_audio_concurrency": provider_report.get(
                        "max_audio_concurrency", 1
                    ),
                    "gpu_memory_observed": gpu_observation,
                    "provider_gpu_allocator_observed": provider_report.get(
                        "gpu_memory_observed"
                    ),
                    "mock_audio_fallback": False,
                    "mock_audio_used": False,
                    "cloud_api_used": False,
                    "voice_cloning_used": False,
                    "motion_preset": request_snapshot.get("motion_preset"),
                    "background_audio": request_snapshot.get("background_audio"),
                    "poster_path": relative_poster,
                    "video_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/video"
                    ),
                    "download_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/video"
                        "?download=true"
                    ),
                    "manifest_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/manifest"
                    ),
                    "poster_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/poster"
                    ),
                    "request_snapshot": request_snapshot,
                    "media_only": request_snapshot.get("media_only") is True,
                    "parent_job_id": request_snapshot.get("parent_job_id"),
                    "source_audio_job_id": request_snapshot.get(
                        "source_audio_job_id"
                    ),
                    "source_audio_provider": request_snapshot.get(
                        "source_audio_provider"
                    ),
                    "audio_provider_calls": (
                        0 if request_snapshot.get("media_only") is True else None
                    ),
                    "warnings": rendered.get("warnings", []),
                }
            )
            if request_snapshot.get("media_only") is True:
                current.update(
                    {
                        "script_provider": "reused",
                        "image_provider": "reused",
                        "audio_provider": "reused",
                        "script_provider_calls": 0,
                        "image_provider_calls": 0,
                        "audio_provider_calls": 0,
                        "source_type": "MEDIA_ONLY_RERENDER_FFMPEG",
                        "video_source_type": "MEDIA_ONLY_RERENDER_FFMPEG",
                        "mock_image_fallback": False,
                        "mock_audio_fallback": False,
                    }
                )
            crud.mark_job_succeeded(session, job=job, result_json=current)
            session.commit()
        print(
            f"[worker] real-audio job={job_id} SUCCEEDED export={export.id}",
            flush=True,
        )

    def _process_real_image_job(
        self,
        *,
        job_id: str,
        project_id: str,
        project_title: str,
        project_story: str,
        project_script_json: dict[str, Any] | None,
        request_snapshot: dict[str, Any],
    ) -> None:
        """复用不可变 ScriptV1，顺序生成真实 PNG，再进入公共 FFmpeg 媒体层。"""

        if request_snapshot.get("script_provider_calls_expected") != 0:
            raise RealImageJobError(
                code="SCRIPT_REUSE_INVALID",
                stage="SCRIPT_REUSE",
                summary="真实图像 Job 必须明确记录文本 Provider 调用次数为 0。",
                retryable=False,
            )
        script, snapshot_payload = load_script_snapshot(
            self.settings,
            project_id=project_id,
            image_job_id=job_id,
            request_snapshot=request_snapshot,
        )
        script_json = script.model_dump(mode="json")
        if project_script_json != script_json:
            raise RealImageJobError(
                code="SCRIPT_SNAPSHOT_STALE",
                stage="SCRIPT_REUSE",
                summary="项目当前剧本已变化，拒绝用旧 ScriptV1 生成真实画面。",
                retryable=False,
            )
        if request_snapshot.get("story_char_count") != len(project_story.strip()):
            raise RealImageJobError(
                code="SCRIPT_SNAPSHOT_STALE",
                stage="SCRIPT_REUSE",
                summary="项目故事与真实图像 Job 的请求快照不一致。",
                retryable=False,
            )
        if request_snapshot.get("actual_shot_count") != len(script.shots):
            raise RealImageJobError(
                code="SCRIPT_SNAPSHOT_INVALID",
                stage="SCRIPT_REUSE",
                summary="ScriptV1 镜头数与 Job 请求快照不一致。",
                retryable=False,
            )

        options = self._real_image_options(request_snapshot)
        provider = self._real_image_provider()
        if provider.provider_id != REAL_IMAGE_PROVIDER_ID:
            raise RealImageJobError(
                code="IMAGE_PROVIDER_SNAPSHOT_MISMATCH",
                stage="IMAGE_GENERATION",
                summary="注入的 ImageProvider 与 Job 请求快照不一致。",
                retryable=False,
            )

        source_script_job_id = str(request_snapshot.get("source_script_job_id") or "")
        source_script_provider = str(
            request_snapshot.get("source_script_provider") or "unknown"
        )
        source_script_source_type = str(
            request_snapshot.get("source_script_source_type") or "UNKNOWN"
        )
        source_trace = snapshot_payload.get("source_trace")
        if not isinstance(source_trace, dict):
            source_trace = {}
        script_trace = {
            "reuse_mode": "VALIDATED_SCRIPT_SNAPSHOT",
            "source_script_job_id": source_script_job_id,
            "source_script_provider": source_script_provider,
            "source_script_source_type": source_script_source_type,
            "script_provider_calls": 0,
            "snapshot_path": request_snapshot.get("script_snapshot_path"),
            "snapshot_sha256": request_snapshot.get("script_snapshot_sha256"),
            "source_trace": source_trace,
        }
        planning_service = GenerationService(
            # 该占位对象仅满足类型契约；prepare_validated_script 不调用 generate。
            script_provider=MockScriptProvider(self.settings.root_dir),
            image_provider=provider,
            audio_provider=MockAudioProvider(),
        )
        prepared = planning_service.prepare_validated_script(
            script=script,
            provider_id="reused",
            source_type="REUSED_VALIDATED_SCRIPT",
            trace=script_trace,
            desired_shot_count=len(script.shots),
            story_char_count=len(project_story.strip()),
        )

        with self.database.session() as session:
            database_shots = crud.list_shots(session, project_id)
            if len(database_shots) != len(script.shots):
                raise RealImageJobError(
                    code="SCRIPT_SNAPSHOT_STALE",
                    stage="SCRIPT_REUSE",
                    summary="数据库镜头与复用 ScriptV1 数量不一致。",
                    retryable=False,
                )
            database_shot_ids: dict[int, str] = {}
            for script_shot, database_shot in zip(script.shots, database_shots):
                if (
                    database_shot.shot_index != script_shot.index
                    or database_shot.title != script_shot.title
                    or database_shot.visual_description
                    != script_shot.visual_description
                    or database_shot.narration != script_shot.narration
                    or abs(
                        float(database_shot.duration_seconds)
                        - float(script_shot.duration_seconds)
                    )
                    > 1e-6
                ):
                    raise RealImageJobError(
                        code="SCRIPT_SNAPSHOT_STALE",
                        stage="SCRIPT_REUSE",
                        summary="数据库镜头内容与复用 ScriptV1 不一致。",
                        failed_shot_id=script_shot.id,
                        failed_shot_index=script_shot.index,
                        retryable=False,
                    )
                database_shot_ids[script_shot.index] = database_shot.id

        image_dir = (
            self.settings.project_dir(project_id) / "jobs" / job_id / "images"
        ).resolve()
        character_by_id = {item.id: item for item in script.characters}
        scene_by_id = {item.id: item for item in script.scenes}
        image_requests = tuple(
            ImageGenerationRequest(
                project_id=project_id,
                job_id=job_id,
                script=script,
                shot=shot,
                characters=tuple(character_by_id[item] for item in shot.character_ids),
                scene=scene_by_id[shot.scene_id],
                output_dir=image_dir,
                options=options,
            )
            for shot in script.shots
        )
        reusable_assets = self._load_reusable_image_assets(
            project_id=project_id,
            request_snapshot=request_snapshot,
        )
        initial_images = [
            {
                "shot_id": shot.id,
                "shot_index": shot.index,
                "title": shot.title,
                "status": "PENDING",
                "provider_id": REAL_IMAGE_PROVIDER_ID,
                "model_id": getattr(provider, "model_id", self.settings.comfyui_model_id),
                "seed": options.base_seed + shot.index,
            }
            for shot in script.shots
        ]
        initial_result = {
            "stage": "IMAGE_GENERATION",
            "script_provider": "reused",
            "source_script_provider": source_script_provider,
            "script_source_type": "REUSED_VALIDATED_SCRIPT",
            "source_script_source_type": source_script_source_type,
            "source_script_job_id": source_script_job_id,
            "script_provider_calls": 0,
            "script_json": script_json,
            "script_trace": prepared.script_trace,
            "script_validation_warnings": prepared.script_validation_warnings,
            "actual_shot_count": len(script.shots),
            "story_char_count": len(project_story.strip()),
            "planned_duration_seconds": prepared.planned_duration_seconds,
            "image_provider": REAL_IMAGE_PROVIDER_ID,
            "image_model_id": getattr(
                provider, "model_id", self.settings.comfyui_model_id
            ),
            "image_model_license": self.settings.comfyui_model_license,
            "audio_provider": "mock",
            "base_seed": options.base_seed,
            "image_options": options.as_dict(),
            "image_shots": initial_images,
            "image_completed_count": 0,
            "image_total_count": len(script.shots),
            "retry_of_job_id": request_snapshot.get("retry_of_job_id"),
            "resume_image_from_job_id": request_snapshot.get(
                "resume_image_from_job_id"
            ),
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"真实图像准备完成时 Job 不再是 RUNNING：{job_id}")
            crud.set_job_progress(session, job, 5)
            job.result_json = initial_result
            session.commit()

        parsed_llama = urlparse(self.settings.llama_server_base_url)
        handoff = gpu_handoff_status(
            llama_host=parsed_llama.hostname or "127.0.0.1",
            llama_port=parsed_llama.port or 8081,
        )
        if handoff["conflict"]:
            raise RealImageJobError(
                code="GPU_HANDOFF_REQUIRED",
                stage="GPU_HANDOFF_REQUIRED",
                summary="本机8GB显存模式需要先停止Qwen服务，再开始真实图像生成。",
                total_image_count=len(script.shots),
                requires_qwen_shutdown=True,
                suggestions=[
                    "停止 llama-server 并确认 8081 已释放。",
                    "释放显存后手动重试；平台不会终止用户进程。",
                ],
            )

        monitor = GpuMemoryMonitor()
        monitor.start()
        image_started = time.monotonic()
        try:
            generated_assets = provider.generate_batch(
                requests=image_requests,
                reusable_assets=reusable_assets,
                progress_callback=lambda completed, total, asset: (
                    self._record_real_image_progress(
                        job_id=job_id,
                        project_id=project_id,
                        database_shot_id=database_shot_ids[
                            next(
                                shot.index
                                for shot in script.shots
                                if shot.id == asset.shot_id
                            )
                        ],
                        shot_index=next(
                            shot.index
                            for shot in script.shots
                            if shot.id == asset.shot_id
                        ),
                        total=total,
                        asset=asset,
                    )
                ),
            )
        except Exception:
            monitor.stop()
            self._record_gpu_observation(job_id, monitor.summary())
            raise
        monitor.stop()
        gpu_observation = monitor.summary()
        image_generation_total_seconds = round(
            time.monotonic() - image_started, 3
        )
        self._record_gpu_observation(job_id, gpu_observation)

        if len(generated_assets) != len(script.shots):
            raise RealImageJobError(
                code="IMAGE_OUTPUT_MISSING",
                stage="IMAGE_GENERATION",
                summary="ImageProvider 返回的关键帧数量不完整。",
                completed_image_count=len(generated_assets),
                total_image_count=len(script.shots),
            )

        job_trace_dir = image_dir.parent
        report_path = job_trace_dir / "image_generation_report.json"
        report: dict[str, Any] = {}
        if report_path.is_file():
            try:
                loaded_report = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(loaded_report, dict):
                    report = loaded_report
            except (OSError, json.JSONDecodeError):
                report = {}
        generation_context = {
            "generation_job_id": job_id,
            "job_type": REAL_IMAGE_JOB_TYPE,
            "request": request_snapshot,
            "script": {
                "schema_version": script.schema_version,
                "provider_id": "reused",
                "source_script_provider": source_script_provider,
                "source_script_job_id": source_script_job_id,
                "script_provider_calls": 0,
            },
            "providers": {
                "script_provider": "reused",
                "source_script_provider": source_script_provider,
                "script_source_type": "REUSED_VALIDATED_SCRIPT",
                "image_provider": REAL_IMAGE_PROVIDER_ID,
                "image_model_id": getattr(
                    provider, "model_id", self.settings.comfyui_model_id
                ),
                "image_model_license": self.settings.comfyui_model_license,
                "image_source_type": "REAL_LOCAL_MODEL",
                "audio_provider": "mock",
                "video_source_type": "FFMPEG_KEYFRAME_MOTION",
            },
            "script_trace": prepared.script_trace,
            "script_validation_warnings": prepared.script_validation_warnings,
            "source_script_job_id": source_script_job_id,
            "actual_shot_count": len(script.shots),
            "planned_duration_seconds": prepared.planned_duration_seconds,
            "base_seed": options.base_seed,
            "image_options": options.as_dict(),
            "image_generation_total_seconds": image_generation_total_seconds,
            "image_generation_report": self._relative_project_path(
                report_path, project_id
            )
            if report_path.is_file()
            else None,
            "comfyui_start_count": report.get("comfyui_start_count"),
            "sequential_generation": True,
            "max_image_concurrency": 1,
            "gpu_memory_observed": gpu_observation,
            "mock_image_fallback": False,
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"图片生成完成时 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            current.update(
                {
                    "stage": "MEDIA_RENDER",
                    "image_generation_total_seconds": image_generation_total_seconds,
                    "gpu_memory_observed": gpu_observation,
                    "comfyui_start_count": report.get("comfyui_start_count"),
                    "image_completed_count": len(generated_assets),
                    "image_total_count": len(generated_assets),
                }
            )
            job.result_json = current
            crud.set_job_progress(session, job, 65)
            session.commit()

        output_dir = self.settings.project_dir(project_id) / "exports" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        renderer = self.real_image_renderer or self._default_real_image_renderer()
        try:
            rendered = renderer(
                root=self.settings.root_dir,
                project_id=project_id,
                project_title=project_title,
                shots=list(prepared.media_shots),
                keyframes=[asset.as_dict() for asset in generated_assets],
                output_dir=output_dir,
                output_filename=f"short_{job_id}.mp4",
                provider_id=REAL_IMAGE_PROVIDER_ID,
                generation_context=generation_context,
                **self._renderer_media_options(request_snapshot, project_id),
                progress_callback=lambda progress: self._set_running_job_progress(
                    job_id, max(66, min(95, int(progress)))
                ),
            )
        except Exception as exc:
            raise RealImageJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary=f"真实关键帧媒体渲染失败：{str(exc)[:500]}",
                completed_image_count=len(generated_assets),
                total_image_count=len(generated_assets),
                log_paths={
                    "image_generation_report": str(report_path),
                    "comfyui_stdout": str(job_trace_dir / "comfyui.stdout.log"),
                    "comfyui_stderr": str(job_trace_dir / "comfyui.stderr.log"),
                },
                suggestions=["检查 FFmpeg/FFprobe 和媒体日志后手动重试。"],
            ) from exc

        self._finish_real_image_job(
            job_id=job_id,
            project_id=project_id,
            request_snapshot=request_snapshot,
            prepared=prepared,
            generated_assets=generated_assets,
            rendered=rendered,
            source_script_job_id=source_script_job_id,
            source_script_provider=source_script_provider,
            image_generation_total_seconds=image_generation_total_seconds,
            gpu_observation=gpu_observation,
            comfyui_start_count=report.get("comfyui_start_count"),
        )

    def _real_image_options(
        self, request_snapshot: dict[str, Any]
    ) -> ImageGenerationOptions:
        payload = request_snapshot.get("image_options")
        base_seed = request_snapshot.get("base_seed")
        if not isinstance(payload, dict) or type(base_seed) is not int:
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary="真实图像 Job 缺少合法的参数或 base seed 快照。",
                retryable=False,
            )
        required_ints = ("width", "height", "batch_size", "steps")
        required_numbers = (
            "cfg",
            "denoise",
            "startup_timeout_seconds",
            "generation_timeout_seconds",
            "job_timeout_seconds",
            "http_timeout_seconds",
        )
        if any(type(payload.get(name)) is not int for name in required_ints):
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary="真实图像整数参数快照无效。",
                retryable=False,
            )
        if any(
            type(payload.get(name)) not in (int, float) for name in required_numbers
        ):
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary="真实图像数值参数快照无效。",
                retryable=False,
            )
        if payload.get("lowvram") is not True:
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary="M4-B 在 8GB 显存设备上必须使用 lowvram=true。",
                retryable=False,
            )
        if not isinstance(payload.get("sampler"), str) or not isinstance(
            payload.get("scheduler"), str
        ):
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary="sampler 和 scheduler 参数快照无效。",
                retryable=False,
            )
        try:
            return ImageGenerationOptions(
                width=payload["width"],
                height=payload["height"],
                steps=payload["steps"],
                cfg=float(payload["cfg"]),
                sampler=str(payload["sampler"]),
                scheduler=str(payload["scheduler"]),
                denoise=float(payload["denoise"]),
                batch_size=payload["batch_size"],
                base_seed=base_seed,
                lowvram=True,
                startup_timeout_seconds=float(payload["startup_timeout_seconds"]),
                generation_timeout_seconds=float(
                    payload["generation_timeout_seconds"]
                ),
                job_timeout_seconds=float(payload["job_timeout_seconds"]),
                http_timeout_seconds=float(payload["http_timeout_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RealImageJobError(
                code="IMAGE_OPTIONS_INVALID",
                stage="IMAGE_GENERATION",
                summary=f"真实图像参数快照无法通过校验：{exc}",
                retryable=False,
            ) from exc

    def _real_image_provider(self) -> ImageProvider:
        if self.image_provider_factory is not None:
            return self.image_provider_factory(self.settings)
        return ComfyUIImageProvider(
            comfy_python=Path(self.settings.comfyui_python),
            comfy_root=Path(self.settings.comfyui_root),
            model_path=Path(self.settings.comfyui_model_path),
            model_sha256=self.settings.comfyui_model_sha256,
            host=self.settings.comfyui_host,
            port=self.settings.comfyui_port,
        )

    def _load_reusable_image_assets(
        self,
        *,
        project_id: str,
        request_snapshot: dict[str, Any],
    ) -> tuple[GeneratedImageAsset, ...]:
        source_job_id = request_snapshot.get("resume_image_from_job_id")
        if not isinstance(source_job_id, str) or not source_job_id:
            return ()
        with self.database.session() as session:
            source_job = crud.get_job(session, source_job_id)
            if (
                source_job is None
                or source_job.project_id != project_id
                or source_job.job_type != REAL_IMAGE_JOB_TYPE
                or source_job.provider_id != REAL_IMAGE_PROVIDER_ID
            ):
                raise RealImageJobError(
                    code="IMAGE_RESUME_INVALID",
                    stage="IMAGE_GENERATION",
                    summary="真实图像重试来源 Job 不存在或来源不匹配。",
                    retryable=False,
                )
            source_result = dict(source_job.result_json or {})
        items = source_result.get("image_shots")
        if not isinstance(items, list):
            return ()
        reusable: list[GeneratedImageAsset] = []
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in {
                "SUCCEEDED",
                "REUSED",
            }:
                continue
            try:
                warnings = item.get("warnings", [])
                if not isinstance(warnings, list) or not all(
                    isinstance(value, str) for value in warnings
                ):
                    continue
                reusable.append(
                    GeneratedImageAsset(
                        provider_id=str(item["provider_id"]),
                        model_id=str(item["model_id"]),
                        shot_id=str(item["shot_id"]),
                        image_path=self._stored_project_path(
                            str(item["image_path"]), project_id
                        ),
                        width=int(item["width"]),
                        height=int(item["height"]),
                        seed=int(item["seed"]),
                        positive_prompt=str(item["positive_prompt"]),
                        negative_prompt=str(item["negative_prompt"]),
                        generation_seconds=float(item["generation_seconds"]),
                        image_sha256=str(item["image_sha256"]),
                        model_sha256=str(item["model_sha256"]),
                        workflow_path=self._stored_project_path(
                            str(item["workflow_path"]), project_id
                        ),
                        trace_path=self._stored_project_path(
                            str(item["trace_path"]), project_id
                        ),
                        warnings=tuple(warnings),
                        reused=True,
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Provider 会从第一张缺失或损坏的图片继续；坏追溯不能被复用。
                continue
        return tuple(reusable)

    def _record_real_image_progress(
        self,
        *,
        job_id: str,
        project_id: str,
        database_shot_id: str,
        shot_index: int,
        total: int,
        asset: GeneratedImageAsset,
    ) -> None:
        relative_image = self._relative_project_path(asset.image_path, project_id)
        relative_workflow = self._relative_project_path(
            asset.workflow_path, project_id
        )
        relative_trace = self._relative_project_path(asset.trace_path, project_id)
        serialized = {
            **asset.as_dict(),
            "shot_index": shot_index,
            "status": "REUSED" if asset.reused else "SUCCEEDED",
            "image_path": relative_image,
            "workflow_path": relative_workflow,
            "trace_path": relative_trace,
            "source_type": "REAL_LOCAL_MODEL",
        }
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"记录图片时 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            image_shots = current.get("image_shots")
            if not isinstance(image_shots, list):
                image_shots = []
            existing = next(
                (
                    item
                    for item in image_shots
                    if isinstance(item, dict)
                    and item.get("shot_id") == asset.shot_id
                    and item.get("image_asset_id")
                ),
                None,
            )
            if existing is None:
                database_asset = crud.create_asset(
                    session,
                    project_id=project_id,
                    shot_id=database_shot_id,
                    asset_type="KEYFRAME_IMAGE",
                    provider_id=asset.provider_id,
                    source_type="REAL_LOCAL_MODEL",
                    file_path=relative_image,
                    sha256=asset.image_sha256,
                    metadata_json={
                        "job_id": job_id,
                        "shot_id": asset.shot_id,
                        "shot_index": shot_index,
                        "model_id": asset.model_id,
                        "model_sha256": asset.model_sha256,
                        "seed": asset.seed,
                        "width": asset.width,
                        "height": asset.height,
                        "generation_seconds": asset.generation_seconds,
                        "workflow_path": relative_workflow,
                        "trace_path": relative_trace,
                        "reused": asset.reused,
                    },
                )
                serialized["image_asset_id"] = database_asset.id
                serialized["image_url"] = (
                    f"/api/projects/{project_id}/assets/{database_asset.id}/content"
                )
            else:
                serialized["image_asset_id"] = existing.get("image_asset_id")
                serialized["image_url"] = existing.get("image_url")
            by_id = {
                str(item.get("shot_id")): dict(item)
                for item in image_shots
                if isinstance(item, dict) and item.get("shot_id")
            }
            by_id[asset.shot_id] = serialized
            ordered = sorted(
                by_id.values(), key=lambda item: int(item.get("shot_index", 999))
            )
            completed_count = sum(
                item.get("status") in {"SUCCEEDED", "REUSED"} for item in ordered
            )
            current.update(
                {
                    "stage": "IMAGE_GENERATION",
                    "image_shots": ordered,
                    "image_completed_count": completed_count,
                    "image_total_count": total,
                    "current_shot_id": asset.shot_id,
                    "current_shot_index": shot_index,
                }
            )
            job.result_json = current
            crud.set_job_progress(session, job, 5 + int(55 * completed_count / total))
            session.commit()

    def _record_gpu_observation(
        self, job_id: str, observation: dict[str, Any]
    ) -> None:
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return
            result = dict(job.result_json or {})
            result["gpu_memory_observed"] = observation
            job.result_json = result
            session.commit()

    def _set_running_job_progress(self, job_id: str, progress: int) -> None:
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return
            crud.set_job_progress(session, job, progress)
            session.commit()

    def _finish_real_image_job(
        self,
        *,
        job_id: str,
        project_id: str,
        request_snapshot: dict[str, Any],
        prepared: PreparedGeneration,
        generated_assets: tuple[GeneratedImageAsset, ...],
        rendered: dict[str, Any],
        source_script_job_id: str,
        source_script_provider: str,
        image_generation_total_seconds: float,
        gpu_observation: dict[str, Any],
        comfyui_start_count: Any,
    ) -> None:
        output_path = Path(str(rendered.get("output_path", ""))).resolve()
        manifest_path = Path(str(rendered.get("manifest_path", ""))).resolve()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RealImageJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层未生成有效 MP4。",
                completed_image_count=len(generated_assets),
                total_image_count=len(generated_assets),
            )
        if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
            raise RealImageJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层未生成有效 Manifest。",
                completed_image_count=len(generated_assets),
                total_image_count=len(generated_assets),
            )
        validation = rendered.get("validation")
        if not isinstance(validation, dict):
            raise RealImageJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层返回结果缺少 ffprobe 验证。",
                completed_image_count=len(generated_assets),
                total_image_count=len(generated_assets),
            )
        relative_video = self._relative_project_path(output_path, project_id)
        relative_manifest = self._relative_project_path(manifest_path, project_id)
        poster_value = rendered.get("poster_path")
        relative_poster = (
            self._relative_project_path(Path(str(poster_value)).resolve(), project_id)
            if poster_value and Path(str(poster_value)).is_file()
            else None
        )
        video_sha256 = sha256_file(output_path)
        if rendered.get("sha256") not in (None, video_sha256):
            raise RealImageJobError(
                code="MEDIA_RENDER",
                stage="MEDIA_RENDER",
                summary="媒体层返回的 MP4 SHA256 与文件实算值不一致。",
                completed_image_count=len(generated_assets),
                total_image_count=len(generated_assets),
            )
        manifest_sha256 = sha256_file(manifest_path)
        encoded_duration = float(
            validation.get(
                "encoded_duration_seconds", validation.get("duration_seconds")
            )
        )
        planned_duration = float(
            validation.get(
                "planned_duration_seconds", prepared.planned_duration_seconds
            )
        )
        with self.database.session() as session:
            job = crud.get_job(session, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                raise RuntimeError(f"媒体完成时真实图像 Job 不再是 RUNNING：{job_id}")
            current = dict(job.result_json or {})
            video_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="EXPORT_VIDEO",
                provider_id=REAL_IMAGE_PROVIDER_ID,
                source_type="REAL_IMAGE_KEYFRAME_FFMPEG_MOTION",
                file_path=relative_video,
                sha256=video_sha256,
                metadata_json={"job_id": job_id, "validation": validation},
            )
            manifest_asset = crud.create_asset(
                session,
                project_id=project_id,
                asset_type="MANIFEST",
                provider_id=REAL_IMAGE_PROVIDER_ID,
                source_type="REAL_IMAGE_KEYFRAME_FFMPEG_MOTION",
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
                duration_seconds=encoded_duration,
                sha256=video_sha256,
            )
            current.update(
                {
                    "stage": "SUCCEEDED",
                    "export_id": export.id,
                    "video_asset_id": video_asset.id,
                    "manifest_asset_id": manifest_asset.id,
                    "video_path": relative_video,
                    "manifest_path": relative_manifest,
                    "sha256": video_sha256,
                    "validation": validation,
                    "planned_duration_seconds": planned_duration,
                    "encoded_duration_seconds": encoded_duration,
                    "duration_seconds": encoded_duration,
                    "duration_delta_seconds": float(
                        validation.get(
                            "duration_delta_seconds",
                            encoded_duration - planned_duration,
                        )
                    ),
                    "duration_tolerance_seconds": validation.get(
                        "duration_tolerance_seconds"
                    ),
                    "duration_validation": validation.get("duration_validation"),
                    "provider_id": REAL_IMAGE_PROVIDER_ID,
                    "script_provider": "reused",
                    "source_script_provider": source_script_provider,
                    "source_script_job_id": source_script_job_id,
                    "script_provider_calls": 0,
                    "image_provider": REAL_IMAGE_PROVIDER_ID,
                    "image_model_id": generated_assets[0].model_id,
                    "image_model_license": self.settings.comfyui_model_license,
                    "audio_provider": "mock",
                    "video_source_type": "FFMPEG_KEYFRAME_MOTION",
                    "source_type": "REAL_IMAGE_KEYFRAME_FFMPEG_MOTION",
                    "image_generation_total_seconds": image_generation_total_seconds,
                    "gpu_memory_observed": gpu_observation,
                    "comfyui_start_count": comfyui_start_count,
                    "mock_image_fallback": False,
                    "motion_preset": request_snapshot.get("motion_preset"),
                    "background_audio": request_snapshot.get("background_audio"),
                    "poster_path": relative_poster,
                    "video_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/video"
                    ),
                    "download_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/video?download=true"
                    ),
                    "manifest_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/manifest"
                    ),
                    "poster_url": (
                        f"/api/projects/{project_id}/exports/{export.id}/poster"
                    ),
                    "request_snapshot": request_snapshot,
                }
            )
            crud.mark_job_succeeded(session, job=job, result_json=current)
            session.commit()
        print(
            f"[worker] real-image job={job_id} SUCCEEDED export={export.id}",
            flush=True,
        )

    def _stored_project_path(self, stored_path: str, project_id: str) -> Path:
        raw = Path(stored_path)
        candidate = raw.resolve() if raw.is_absolute() else (
            Path(self.settings.data_dir) / raw
        ).resolve()
        project_root = self.settings.project_dir(project_id).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise ValueError("复用图片路径越过当前项目目录") from exc
        return candidate

    def _generation_service_for(
        self,
        *,
        provider_id: str,
        project_id: str,
        job_id: str,
    ) -> GenerationService:
        if self.generation_service is not None:
            configured = self.generation_service.script_provider.provider_id
            if configured != provider_id:
                raise RuntimeError(
                    f"注入的 GenerationService={configured} 与 Job={provider_id} 不一致"
                )
            return self.generation_service

        if provider_id == "mock":
            script_provider = MockScriptProvider(self.settings.root_dir)
        elif provider_id == "llamacpp":
            availability = check_llamacpp(self.settings)
            if not availability["available"]:
                raise RuntimeError(
                    f"本地 Qwen Provider 不可用：{availability['detail']}"
                )
            model_path = Path(self.settings.llama_model_path).resolve()
            if not model_path.is_file():
                raise RuntimeError(f"真实文本模型文件不存在：{model_path}")
            actual_model_sha256 = sha256_file(model_path)
            configured_sha256 = self.settings.llama_model_sha256
            if configured_sha256 and configured_sha256.lower() != actual_model_sha256:
                raise RuntimeError(
                    "GGUF SHA-256 与 LLAMA_MODEL_SHA256 配置不一致，已拒绝调用"
                )
            script_provider = LlamaCppScriptProvider(
                base_url=self.settings.llama_server_base_url,
                model=self.settings.llama_model_id,
                response_dir=(
                    self.settings.project_dir(project_id)
                    / "jobs"
                    / job_id
                    / "llm-responses"
                ),
                timeout_seconds=self.settings.llama_timeout_seconds,
                temperature=self.settings.llama_temperature,
                top_p=self.settings.llama_top_p,
                max_tokens=self.settings.llama_max_tokens,
                prompt_version=self.settings.llama_prompt_version,
                context_size=self.settings.llama_context_size,
                model_file_sha256=actual_model_sha256,
                llama_server_version=str(
                    availability.get("server_version")
                    or self.settings.llama_server_version
                ),
                server_session_factory=lambda: LlamaServerJobSession(
                    executable=Path(self.settings.llama_server_executable),
                    model_path=model_path,
                    model_id=self.settings.llama_model_id,
                    base_url=self.settings.llama_server_base_url,
                    run_dir=(
                        self.settings.project_dir(project_id)
                        / "jobs"
                        / job_id
                        / "llama-server"
                    ),
                    context_size=self.settings.llama_context_size,
                    gpu_layers=self.settings.llama_gpu_layers,
                    startup_timeout_seconds=(
                        self.settings.llama_startup_timeout_seconds
                    ),
                    health_timeout_seconds=self.settings.llama_health_timeout_seconds,
                    blocked_gpu_ports=(self.settings.comfyui_port,),
                ),
            )
        else:
            raise RuntimeError(f"不支持的 ScriptProvider：{provider_id}")
        return GenerationService(
            script_provider=script_provider,
            image_provider=MockImageProvider(),
            audio_provider=MockAudioProvider(),
        )

    @staticmethod
    def _default_renderer() -> Renderer:
        # 延迟导入让 API 与轻量测试不因媒体环境缺失而无法启动。
        from .media import render_mock_project_short

        return render_mock_project_short

    @staticmethod
    def _default_resume_renderer() -> Renderer:
        # 恢复函数只复用既有 MP4 并重新解码、ffprobe 与生成 Manifest。
        from .media import resume_mock_project_short

        return resume_mock_project_short

    @staticmethod
    def _default_real_image_renderer() -> Renderer:
        from .media import render_image_project_short

        return render_image_project_short

    @staticmethod
    def _default_real_audio_renderer() -> Renderer:
        from .media import render_real_audio_project_short

        return render_real_audio_project_short

    def _relative_project_path(self, path: Path, project_id: str) -> str:
        data_root = Path(self.settings.data_dir).resolve()
        project_root = self.settings.project_dir(project_id).resolve()
        try:
            path.resolve().relative_to(project_root)
        except ValueError as exc:
            raise RuntimeError(f"媒体输出越过当前项目目录：{path}") from exc
        return path.resolve().relative_to(data_root).as_posix()

    def _renderer_media_options(
        self, request_snapshot: dict[str, Any], project_id: str
    ) -> dict[str, Any]:
        """解析新任务的媒体快照；缺字段的旧 Job 保持历史渲染行为。"""

        preset = request_snapshot.get("motion_preset")
        if preset is not None and preset not in {
            "static",
            "gentle_zoom",
            "cinematic_pan",
        }:
            raise RuntimeError(f"Job motion_preset 快照无效：{preset}")
        raw_background = request_snapshot.get("background_audio")
        background: dict[str, Any] | None = None
        if isinstance(raw_background, dict):
            background = dict(raw_background)
            if background.get("enabled") is True:
                stored_path = background.get("storage_path")
                if not isinstance(stored_path, str) or not stored_path:
                    raise RuntimeError("启用背景音的 Job 快照缺少 storage_path")
                background["resolved_path"] = str(
                    self._stored_project_path(stored_path, project_id)
                )
        return {"motion_preset": preset, "background_audio": background}

    def run_forever(self, *, poll_seconds: float | None = None) -> None:
        interval = self.settings.worker_poll_seconds if poll_seconds is None else poll_seconds
        if interval <= 0:
            raise ValueError("轮询间隔必须大于 0")
        print(
            json.dumps(
                {
                    "worker": "m3-single-worker",
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
    parser = argparse.ArgumentParser(description="AnimeFlow M3 SQLite 单 Worker")
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

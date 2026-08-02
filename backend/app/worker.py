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

from . import crud
from .config import Settings
from .database import Database
from .media.ffmpeg import sha256_file
from .models import JobStatus
from .providers.llama_cpp import LlamaCppScriptProvider
from .providers.mock import MockAudioProvider, MockImageProvider, MockScriptProvider
from .providers.registry import check_llamacpp
from .script_schema import ScriptV1
from .services.generation import GenerationService, PreparedGeneration


Renderer = Callable[..., dict[str, Any]]


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
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.ensure_directories()
        self.database = database or Database(str(self.settings.database_url))
        self.database.create_schema()
        self.renderer = renderer
        self.resume_renderer = resume_renderer
        self.generation_service = generation_service
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
                                "启动并检查 llama-server 后手动重试。",
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
                **recovery_trace,
                "video_url": f"/api/projects/{project_id}/exports/{export.id}/video",
                "download_url": (
                    f"/api/projects/{project_id}/exports/{export.id}/video?download=true"
                ),
                "manifest_url": f"/api/projects/{project_id}/exports/{export.id}/manifest",
            }
            crud.mark_job_succeeded(session, job=job, result_json=result_json)
            session.commit()
        print(f"[worker] job={job_id} SUCCEEDED export={export.id}", flush=True)

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

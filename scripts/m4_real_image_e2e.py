"""有界执行一次 M4-B 三镜头真实图像纵向链路；不启动或调用 Qwen。"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.media.ffmpeg import (
    resolve_media_tools,
    run_command,
    sha256_file,
    verify_media,
)
from backend.app.models import GenerationJob, JobStatus, Project
from backend.app.script_schema import ScriptV1
from backend.app.services.image_jobs import gpu_handoff_status, script_from_source_job
from backend.app.worker import Worker


class E2EError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _gpu_memory_used_mib() -> int | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            shell=False,
        )
        if completed.returncode != 0:
            return None
        return int(completed.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _select_source(
    settings: Settings,
    database: Database,
    *,
    source_job_id: str | None,
) -> tuple[Project, GenerationJob, ScriptV1]:
    with database.session() as session:
        active = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING))
                )
            ).all()
        )
        if active:
            raise E2EError(
                "数据库中已有 QUEUED/RUNNING Job；为保证 run_once 不误领任务，"
                "请先处理这些任务：" + ", ".join(item.id for item in active)
            )
        statement = (
            select(GenerationJob)
            .where(
                GenerationJob.status == JobStatus.SUCCEEDED,
                GenerationJob.job_type == "GENERATE_SHORT_VIDEO",
            )
            .order_by(GenerationJob.created_at.desc())
        )
        if source_job_id:
            statement = statement.where(GenerationJob.id == source_job_id)
        candidates = list(session.scalars(statement).all())
        eligible: list[tuple[int, GenerationJob, Project, ScriptV1]] = []
        for job in candidates:
            project = session.get(Project, job.project_id)
            if project is None:
                continue
            try:
                script, _trace = script_from_source_job(
                    settings,
                    project=project,
                    source_job=job,
                )
            except RuntimeError:
                continue
            if len(script.shots) != 3:
                continue
            if project.script_json != script.model_dump(mode="json"):
                continue
            source_provider = str(
                (job.result_json or {}).get("script_provider") or job.provider_id
            )
            provider_priority = 0 if source_provider == "llamacpp" else 1
            eligible.append((provider_priority, job, project, script))
        if not eligible:
            requested = f" {source_job_id}" if source_job_id else ""
            raise E2EError(
                "没有找到可复用的成功三镜头 ScriptV1 Job" + requested + "。"
            )
        eligible.sort(key=lambda item: (item[0], -item[1].created_at.timestamp()))
        _priority, selected_job, selected_project, selected_script = eligible[0]
        # Session 关闭后仍可读取这些对象，Database 使用 expire_on_commit=False。
        return selected_project, selected_job, selected_script


def _extract_frames(
    *,
    tools: Any,
    video_path: Path,
    shot_durations: list[float],
    output_dir: Path,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[str] = []
    offset = 0.0
    for index, duration in enumerate(shot_durations, start=1):
        timestamp = offset + duration / 2.0
        target = output_dir / f"shot-{index:02d}-midpoint.png"
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-y",
                target,
            ],
            timeout_seconds=120,
        )
        if not target.is_file() or target.stat().st_size <= 0:
            raise E2EError(f"无法抽取第 {index} 镜头中点帧")
        frames.append(str(target))
        offset += duration
    return frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-job-id", help="显式指定成功的三镜头来源 Job")
    parser.add_argument("--base-seed", type=int, default=20_260_802)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(str(settings.database_url))
    database.create_schema()
    started = time.monotonic()
    created_job_id: str | None = None
    try:
        if args.base_seed < 0:
            raise E2EError("base seed 不得为负数")
        handoff = gpu_handoff_status()
        if handoff["conflict"]:
            raise E2EError(
                "本机8GB显存模式需要先停止Qwen服务，再开始真实图像生成。"
            )
        if not _port_is_free(settings.comfyui_host, settings.comfyui_port):
            raise E2EError(f"端口 {settings.comfyui_port} 已被占用")
        project, source_job, script = _select_source(
            settings,
            database,
            source_job_id=args.source_job_id,
        )
        print(
            "[m4-e2e] source="
            f"{source_job.id} project={project.id} "
            f"script_provider={(source_job.result_json or {}).get('script_provider')} "
            "shots=3 script_provider_calls_expected=0",
            flush=True,
        )
        app = create_app(settings, database=database)
        with TestClient(app) as client:
            response = client.post(
                f"/api/projects/{project.id}/render-real-images",
                json={
                    "source_script_job_id": source_job.id,
                    "image_provider": "comfyui-animagine-xl-4",
                    "base_seed": args.base_seed,
                },
            )
            if response.status_code != 202:
                raise E2EError(
                    f"真实图像 Job 入队失败：HTTP {response.status_code} {response.text}"
                )
            created_job_id = str(response.json()["job_id"])
            print(f"[m4-e2e] queued job={created_job_id}", flush=True)

        worker = Worker(settings=settings, database=database)
        if not worker.run_once():
            raise E2EError("Worker 未领取刚创建的真实图像 Job")
        with database.session() as session:
            job = session.get(GenerationJob, created_job_id)
            if job is None:
                raise E2EError("真实图像 Job 处理后不存在")
            result = dict(job.result_json or {})
            if job.status != JobStatus.SUCCEEDED:
                raise E2EError(
                    "真实图像 Job 未成功："
                    + json.dumps(
                        result.get("generation_error") or job.error_message,
                        ensure_ascii=False,
                    )
                )
        images = result.get("image_shots")
        if not isinstance(images, list) or len(images) != 3:
            raise E2EError("真实图像结果必须恰好包含 3 张关键帧")
        for expected_index, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                raise E2EError("关键帧追溯结构无效")
            path = Path(settings.data_dir) / str(image["image_path"])
            if not path.is_file() or path.stat().st_size <= 0:
                raise E2EError(f"关键帧文件缺失：{path}")
            if sha256_file(path) != image["image_sha256"]:
                raise E2EError(f"关键帧 SHA256 不一致：{path}")
            if image["width"] != 1024 or image["height"] != 576:
                raise E2EError(f"关键帧分辨率不符：{path}")
            if image["seed"] != args.base_seed + expected_index:
                raise E2EError(f"关键帧 seed 不符：{path}")

        video_path = Path(settings.data_dir) / str(result["video_path"])
        manifest_path = Path(settings.data_dir) / str(result["manifest_path"])
        tools = resolve_media_tools()
        verification = verify_media(
            tools,
            video_path,
            expected_width=1280,
            expected_height=720,
            expected_fps=24.0,
            planned_duration_seconds=sum(
                float(item.duration_seconds) for item in script.shots
            ),
        )
        run_command(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-f",
                "null",
                "NUL" if sys.platform == "win32" else "/dev/null",
            ],
            timeout_seconds=300,
        )
        frame_paths = _extract_frames(
            tools=tools,
            video_path=video_path,
            shot_durations=[float(item.duration_seconds) for item in script.shots],
            output_dir=video_path.parent / "e2e-frames",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("script_provider") != "reused":
            raise E2EError("Manifest 未记录 script_provider=reused")
        if manifest.get("image_provider") != "comfyui-animagine-xl-4":
            raise E2EError("Manifest 未记录真实 ImageProvider")
        if manifest.get("audio_provider") != "mock":
            raise E2EError("Manifest 未记录 audio_provider=mock")
        if result.get("script_provider_calls") != 0:
            raise E2EError("真实图像 Job 错误地调用了 ScriptProvider")
        if result.get("comfyui_start_count") != 1:
            raise E2EError("三镜头 Job 必须只启动一次 ComfyUI")
        if not _port_is_free(settings.comfyui_host, settings.comfyui_port):
            raise E2EError("真实图像 Job 结束后 8188 端口未释放")

        summary = {
            "e2e_version": "m4b.real-image-e2e.v1",
            "success": True,
            "finished_at": _utc_now(),
            "project_id": project.id,
            "source_script_job_id": source_job.id,
            "source_script_provider": result.get("source_script_provider"),
            "script_provider_calls": result.get("script_provider_calls"),
            "job_id": created_job_id,
            "image_provider": result.get("image_provider"),
            "image_model_id": result.get("image_model_id"),
            "base_seed": args.base_seed,
            "images": images,
            "image_generation_total_seconds": result.get(
                "image_generation_total_seconds"
            ),
            "gpu_memory_observed": result.get("gpu_memory_observed"),
            "comfyui_start_count": result.get("comfyui_start_count"),
            "oom": False,
            "mock_images_used": False,
            "video_path": str(video_path),
            "video_sha256": sha256_file(video_path),
            "manifest_path": str(manifest_path),
            "ffprobe_validation": verification,
            "full_decode": True,
            "extracted_frames": frame_paths,
            "port_8188_released": True,
            "post_run_gpu_memory_used_mib": _gpu_memory_used_mib(),
            "total_wall_seconds": round(time.monotonic() - started, 3),
        }
        summary_path = video_path.parent / "m4b-e2e-summary.json"
        _atomic_json(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        print(f"[m4-e2e] summary={summary_path}", flush=True)
        return 0
    except Exception as exc:
        print(f"[m4-e2e] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        if created_job_id:
            print(f"[m4-e2e] job={created_job_id}", file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

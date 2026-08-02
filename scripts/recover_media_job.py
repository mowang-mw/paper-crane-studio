from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import Settings  # noqa: E402
from backend.app.database import Database  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.models import GenerationJob, JobStatus  # noqa: E402
from backend.app.worker import Worker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过现有重试 API 从 MEDIA_RENDER 阶段恢复一个失败任务"
    )
    parser.add_argument("failed_job_id")
    args = parser.parse_args()

    settings = Settings.from_env()
    database = Database(str(settings.database_url))
    app = create_app(settings, database=database)
    try:
        with database.session() as session:
            active = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.status.in_(
                            (JobStatus.QUEUED, JobStatus.RUNNING)
                        )
                    )
                ).all()
            )
            if active:
                raise RuntimeError(
                    "存在其他 QUEUED/RUNNING 任务，拒绝不确定地领取任务："
                    + ", ".join(item.id for item in active)
                )

        with TestClient(app) as client:
            response = client.post(f"/api/jobs/{args.failed_job_id}/retry")
            if response.status_code != 202:
                raise RuntimeError(
                    f"创建恢复任务失败：HTTP {response.status_code} {response.text}"
                )
            retry_job_id = str(response.json()["job_id"])
            queued = client.get(f"/api/jobs/{retry_job_id}").json()
            if queued.get("request_json", {}).get("resumed_from_stage") != "MEDIA_RENDER":
                raise RuntimeError("重试任务没有记录 resumed_from_stage=MEDIA_RENDER")

            worker = Worker(settings=settings, database=database)
            if not worker.run_once():
                raise RuntimeError("Worker 没有领取到刚创建的恢复任务")
            recovered = client.get(f"/api/jobs/{retry_job_id}").json()

        result = recovered.get("result_json") or {}
        summary = {
            "status": "PASS" if recovered.get("status") == "SUCCEEDED" else "FAIL",
            "source_job_id": args.failed_job_id,
            "recovery_job_id": retry_job_id,
            "job_status": recovered.get("status"),
            "resumed_from_stage": result.get("resumed_from_stage"),
            "script_provider_calls_during_resume": result.get(
                "script_provider_calls_during_resume"
            ),
            "actual_shot_count": result.get("actual_shot_count"),
            "planned_duration_seconds": result.get("planned_duration_seconds"),
            "encoded_duration_seconds": result.get("encoded_duration_seconds"),
            "duration_delta_seconds": result.get("duration_delta_seconds"),
            "duration_tolerance_seconds": result.get(
                "duration_tolerance_seconds"
            ),
            "duration_validation": result.get("duration_validation"),
            "media_reused": result.get("media_reused"),
            "reencoded": result.get("reencoded"),
            "video_path": result.get("video_path"),
            "manifest_path": result.get("manifest_path"),
            "sha256": result.get("sha256"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] == "PASS" else 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

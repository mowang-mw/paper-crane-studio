from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import crud
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import JobStatus
from backend.app.providers.llama_cpp import (
    LlamaCppOutputError,
    LlamaCppScriptProvider,
)
from backend.app.providers.mock import (
    MockAudioProvider,
    MockImageProvider,
    MockScriptProvider,
)
from backend.app.schemas import ProjectCreate
from backend.app.services.generation import GenerationService
from backend.app.worker import Worker


def _script_payload(
    shot_count: int = 4,
    *,
    durations: list[float] | None = None,
) -> dict[str, Any]:
    shot_durations = durations or [7.0] * shot_count
    return {
        "schema_version": "script.v1",
        "title": "纸鹤的夜航",
        "synopsis": "少女与发光纸鹤完成一次穿过城市夜空的短暂旅程。",
        "characters": [
            {
                "id": "character_01",
                "name": "阿澄",
                "role": "跟随纸鹤完成夜航的主角",
                "appearance": "原创少女，深色短发，眼神温和。",
                "personality": "安静、好奇、勇敢",
                "costume": "浅色居家服与深色披肩",
                "consistency_prompt": "同一原创少女，深色短发与浅色居家服保持一致。",
            }
        ],
        "scenes": [
            {
                "id": f"scene_{index:02d}",
                "name": f"夜航场景 {index}",
                "description": f"纸鹤经过第 {index} 个原创城市夜景。",
                "time": "黎明" if index == shot_count else "夜晚",
                "lighting": "暖色晨光" if index == shot_count else "柔和月光",
                "consistency_prompt": f"原创二维动画夜航场景 {index}，空间关系稳定。",
            }
            for index in range(1, shot_count + 1)
        ],
        "shots": [
            {
                "id": f"shot_{index:02d}",
                "index": index,
                "title": f"镜头 {index}",
                "scene_id": f"scene_{index:02d}",
                "character_ids": ["character_01"],
                "visual_description": f"少女注视纸鹤飞过第 {index} 处夜景。",
                "camera": "缓慢推近",
                "image_prompt": f"原创二维动漫夜景，纸鹤飞过场景 {index}，16:9。",
                "negative_prompt": "文字，水印，品牌标志",
                "narration": f"纸鹤飞过第{index}盏灯。",
                "duration_seconds": shot_durations[index - 1],
            }
            for index in range(1, shot_count + 1)
        ],
    }


def _completion_envelope(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-usability-test",
        "created": 1,
        "model": "test-model.gguf",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _make_llama_provider(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> LlamaCppScriptProvider:
    return LlamaCppScriptProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="test-model.gguf",
        response_dir=tmp_path / "llm-responses",
        timeout_seconds=5.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _create_project(client: TestClient, *, story: str) -> dict[str, Any]:
    response = client.post(
        "/api/projects",
        json={"title": "生成可用性测试", "story": story},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_story_input_hard_limits_are_applied_after_trimming() -> None:
    minimum = "字" * 10
    maximum = "夜" * 3000

    assert ProjectCreate(title="边界", story=f" \n{minimum}\t ").story == minimum
    assert ProjectCreate(title="边界", story=maximum).story == maximum

    with pytest.raises(ValidationError, match="故事过短.*9 个字符.*至少需要 10"):
        ProjectCreate(title="边界", story=f"  {'短' * 9}  ")
    with pytest.raises(ValidationError, match="故事过长.*3001 个字符.*最多允许 3000"):
        ProjectCreate(title="边界", story="长" * 3001)


def test_generation_snapshot_and_manual_retry_preserve_count_and_story_length(
    client: TestClient,
    database: Database,
) -> None:
    story = "  少女在旧书店发现发光画册，蓝色鲸鱼飞出书页并带她穿过星光。  "
    project = _create_project(client, story=story)
    expected_story_count = len(story.strip())

    queued = client.post(
        f"/api/projects/{project['id']}/generate",
        json={"script_provider": "mock", "desired_shot_count": 5},
    )
    assert queued.status_code == 202, queued.text
    original_id = queued.json()["job_id"]
    original = client.get(f"/api/jobs/{original_id}").json()
    original_request = original["request_json"]
    assert original_request["desired_shot_count"] == 5
    assert original_request["story_char_count"] == expected_story_count
    assert original_request["script_provider"] == "mock"

    with database.session() as session:
        stored = crud.get_job(session, original_id)
        assert stored is not None
        crud.mark_job_failed(session, job=stored, error_message="受控失败")
        session.commit()

    retried = client.post(f"/api/jobs/{original_id}/retry")
    assert retried.status_code == 202, retried.text
    retry_id = retried.json()["job_id"]
    retry_request = client.get(f"/api/jobs/{retry_id}").json()["request_json"]
    for key, value in original_request.items():
        assert retry_request[key] == value
    assert retry_request["retry_of_job_id"] == original_id


@pytest.mark.parametrize(
    ("desired_shot_count", "expected_count", "expected_duration"),
    [(3, 3, 24.0), (4, 4, 28.0), (5, 5, 35.0), (None, 4, 28.0)],
)
def test_mock_provider_obeys_fixed_and_auto_shot_count(
    desired_shot_count: int | None,
    expected_count: int,
    expected_duration: float,
) -> None:
    root = Path(__file__).resolve().parents[2]
    result = MockScriptProvider(root).generate(
        title="星光旅途",
        story="少女跟随微光穿过屋顶与云层，最终在黎明找到回家的方向。",
        desired_shot_count=desired_shot_count,
    )

    assert len(result.shots) == expected_count
    assert [shot.shot_index for shot in result.shots] == list(
        range(1, expected_count + 1)
    )
    assert sum(shot.duration_seconds for shot in result.shots) == expected_duration
    assert result.trace is not None
    assert result.trace["desired_shot_count"] == desired_shot_count
    assert result.trace["actual_shot_count"] == expected_count


def test_fixed_shot_count_mismatch_repairs_once_and_never_silently_crops(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    four_shots = _script_payload(4)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_completion_envelope(
                json.dumps(four_shots, ensure_ascii=False)
            ),
        )

    provider = _make_llama_provider(tmp_path, handler)
    with pytest.raises(LlamaCppOutputError) as caught:
        provider.generate(
            title="纸鹤的夜航",
            story="少女跟随纸鹤穿过城市灯火，并在黎明回到窗边。",
            desired_shot_count=5,
        )

    assert len(calls) == 3
    assert provider.last_script is None
    assert caught.value.parsed_payload is not None
    assert len(caught.value.parsed_payload["shots"]) == 4
    error = caught.value.generation_error
    assert error is not None
    assert error["stage"] == "REPAIR_FAILED"
    assert error["failed_validation_stage"] == "SHOT_COUNT_VALIDATION"
    assert error["desired_shot_count"] == 5
    assert "恰好生成 5 个镜头" in error["summary"]
    assert "恰好 5 个镜头" in json.loads(calls[1].content)["messages"][1]["content"]

    trace = provider.last_trace
    assert trace is not None
    assert [attempt["kind"] for attempt in trace["attempts"]] == [
        "initial",
        "repair",
        "repair",
    ]
    raw_paths = [Path(attempt["raw_response_path"]) for attempt in trace["attempts"]]
    assert raw_paths[0].name == "first_raw_response.json"
    assert raw_paths[1].name == "repair_raw_response.json"
    assert raw_paths[2].name == "repair_2_raw_response.json"
    assert all(path.is_file() and path.stat().st_size > 0 for path in raw_paths)
    report_path = Path(error["validation_report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["repair_requested"] is True
    assert report["final_result"] == "FAILED"
    assert report["final_failure_code"] == "REPAIR_FAILED"


def test_duration_only_failure_after_single_repair_is_normalized_deterministically(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    invalid_durations = _script_payload(
        4,
        durations=[2.0, 4.0, 8.0, 14.0],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_completion_envelope(
                json.dumps(invalid_durations, ensure_ascii=False)
            ),
        )

    provider = _make_llama_provider(tmp_path, handler)
    result = provider.generate(
        title="纸鹤的夜航",
        story="少女跟随纸鹤穿过城市灯火，并在黎明回到窗边。",
        desired_shot_count=4,
    )

    assert len(calls) == 2
    assert [shot.provider_shot_id for shot in result.shots] == [
        "shot_01",
        "shot_02",
        "shot_03",
        "shot_04",
    ]
    assert [shot.duration_seconds for shot in result.shots] == [
        4.0,
        4.7,
        9.3,
        10.0,
    ]
    trace = provider.last_trace
    assert trace is not None
    assert trace["repair_used"] is True
    normalization = trace["duration_normalization"]
    assert normalization["normalized"] is True
    assert normalization["original_durations"] == [2.0, 4.0, 8.0, 14.0]
    assert normalization["normalized_durations"] == [4.0, 4.7, 9.3, 10.0]
    assert normalization["normalized_total"] == 28.0
    assert "未增删、合并或重排镜头" in normalization["reason"]
    assert trace["attempts"][0]["validation"]["status"] == "INVALID"
    assert trace["attempts"][1]["validation"]["status"] == "NORMALIZED"

    report_path = Path(trace["validation_report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["first_attempt_errors"]
    assert report["repair_attempt_errors"]
    assert report["duration_normalization"]["normalized"] is True
    assert report["final_result"] == "SUCCEEDED"
    assert (report_path.parent / "first_raw_response.json").is_file()
    assert (report_path.parent / "repair_raw_response.json").is_file()


@pytest.mark.parametrize(
    ("content_factory", "expected_stage", "summary_fragment"),
    [
        (
            lambda: _payload_with_missing_scene(),
            "SCRIPT_REFERENCE_VALIDATION",
            "不存在的场景",
        ),
        (
            lambda: "这不是 JSON",
            "MODEL_JSON_PARSE",
            "不是合法的单一 JSON 对象",
        ),
    ],
    ids=["unknown-scene-reference", "non-json"],
)
def test_controlled_invalid_model_outputs_have_specific_chinese_diagnostics(
    tmp_path: Path,
    content_factory: Callable[[], dict[str, Any] | str],
    expected_stage: str,
    summary_fragment: str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        value = content_factory()
        content = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, dict)
            else value
        )
        return httpx.Response(200, json=_completion_envelope(content))

    provider = _make_llama_provider(tmp_path, handler)
    with pytest.raises(LlamaCppOutputError) as caught:
        provider.generate(
            title="纸鹤的夜航",
            story="少女跟随纸鹤穿过城市灯火，并在黎明回到窗边。",
            desired_shot_count=4,
        )

    assert calls == 3
    error = caught.value.generation_error
    assert error is not None
    assert error["stage"] == "REPAIR_FAILED"
    assert error["failed_validation_stage"] == expected_stage
    assert summary_fragment in error["summary"]
    assert error["first_attempt_errors"][0]["stage"] == expected_stage
    assert error["repair_attempt_errors"][0]["stage"] == expected_stage
    assert error["suggestions"]
    assert error["provider_id"] == "llamacpp"
    assert error["model_id"] == "test-model.gguf"
    assert Path(error["raw_response_path"]).is_file()
    assert Path(error["repair_response_path"]).is_file()
    assert Path(error["validation_report_path"]).is_file()


def _payload_with_missing_scene() -> dict[str, Any]:
    payload = copy.deepcopy(_script_payload())
    payload["shots"][0]["scene_id"] = "scene_missing"
    return payload


def test_worker_failed_job_exposes_structured_error_and_safe_trace_paths(
    tmp_path: Path,
    settings: Settings,
    database: Database,
) -> None:
    request_count = 0
    invalid = _payload_with_missing_scene()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json=_completion_envelope(json.dumps(invalid, ensure_ascii=False)),
        )

    story = "少女跟随发光纸鹤穿过城市灯火，并在黎明回到窗边。"
    with database.session() as session:
        project = crud.create_project(
            session,
            title="Worker 失败诊断",
            story=story,
        )
        job = crud.create_job(
            session,
            project=project,
            provider_id="llamacpp",
            request_json={
                "project_id": project.id,
                "output": {"width": 1280, "height": 720, "fps": 24},
                "script_provider": "llamacpp",
                "desired_shot_count": 4,
                "story_char_count": len(story),
            },
        )
        job_id = job.id
        session.commit()

    provider = _make_llama_provider(tmp_path, handler)
    service = GenerationService(
        script_provider=provider,
        image_provider=MockImageProvider(),
        audio_provider=MockAudioProvider(),
    )
    worker = Worker(
        settings=settings,
        database=database,
        generation_service=service,
    )
    assert worker.run_once() is True
    assert request_count == 3

    with database.session() as session:
        failed = crud.get_job(session, job_id)
        assert failed is not None
        assert failed.status == JobStatus.FAILED
        assert failed.result_json is not None
        error = failed.result_json["generation_error"]
        assert error["code"] == "REPAIR_FAILED"
        assert error["stage"] == "REPAIR_FAILED"
        assert error["failed_validation_stage"] == "SCRIPT_REFERENCE_VALIDATION"
        assert "不存在的场景" in error["summary"]
        assert error["story_char_count"] == len(story)
        assert error["desired_shot_count"] == 4
        assert Path(error["raw_response_path"]).is_file()
        assert Path(error["repair_response_path"]).is_file()
        assert Path(error["validation_report_path"]).is_file()
        assert failed.error_message is not None
        assert failed.error_message.startswith("REPAIR_FAILED:")
        assert failed.result_json["story_char_count"] == len(story)
        assert failed.result_json["desired_shot_count"] == 4

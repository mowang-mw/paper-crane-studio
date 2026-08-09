from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from backend.app.providers.llama_cpp import (
    LlamaCppOutputError,
    LlamaCppProtocolError,
    LlamaCppScriptProvider,
    LlamaCppTransportError,
    parse_pure_script_json,
)
from backend.app.providers.mock import MockScriptProvider
from backend.app.script_schema import (
    ScriptV1,
    analyze_script_usage,
    script_v1_json_schema,
)


def valid_script_payload() -> dict:
    scenes = [
        {
            "id": f"scene_{index:02d}",
            "name": f"场景 {index}",
            "description": f"原创故事的第 {index} 个场景。",
            "time": "夜晚" if index < 4 else "黎明",
            "lighting": "柔和月光" if index < 4 else "暖色晨光",
            "consistency_prompt": f"原创二维动漫场景 {index}，统一色彩与空间关系。",
        }
        for index in range(1, 5)
    ]
    shots = [
        {
            "id": f"shot_{index:02d}",
            "index": index,
            "title": f"镜头 {index}",
            "scene_id": f"scene_{index:02d}",
            "character_ids": ["character_01"],
            "visual_description": f"主角在第 {index} 个原创场景中向前行走。",
            "camera": "缓慢推近",
            "image_prompt": f"原创二维动漫画面，统一角色造型，第 {index} 个场景，16:9。",
            "narration": f"这是旅途的第{index}步。",
            "duration_seconds": 7.0,
        }
        for index in range(1, 5)
    ]
    return {
        "schema_version": "script.v1",
        "title": "纸鹤的夜航",
        "synopsis": "少女与纸鹤在夜色中完成一次短暂旅程。",
        "characters": [
            {
                "id": "character_01",
                "name": "阿澄",
                "role": "折出纸鹤并见证夜航的主角",
                "appearance": "原创少女，深色短发，眼神温和。",
                "personality": "安静、专注、富有想象力",
                "costume": "浅色居家服与深色披肩",
                "consistency_prompt": "同一原创少女，深色短发，浅色居家服，造型一致。",
            }
        ],
        "scenes": scenes,
        "shots": shots,
    }


def completion_envelope(content: str, *, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "created": 1,
        "model": "test-model.gguf",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def make_provider(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = None,
    base_url: str = "http://127.0.0.1:8080/v1",
) -> LlamaCppScriptProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LlamaCppScriptProvider(
        base_url=base_url,
        model="test-model.gguf",
        response_dir=tmp_path / "raw-responses",
        timeout_seconds=5.0,
        api_key=api_key,
        client=client,
    )


def test_script_v1_accepts_valid_payload_and_exports_strict_json_schema() -> None:
    script = ScriptV1.model_validate(valid_script_payload())
    assert [shot.index for shot in script.shots] == [1, 2, 3, 4]
    assert sum(shot.duration_seconds for shot in script.shots) == 28.0
    assert script.shots[0].camera
    assert script.shots[0].image_prompt

    schema = script_v1_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["shots"]["minItems"] == 3
    assert schema["properties"]["shots"]["maxItems"] == 5
    for definition in ("Character", "Scene", "Shot"):
        assert schema["$defs"][definition]["additionalProperties"] is False
    shot_properties = schema["$defs"]["Shot"]["properties"]
    assert shot_properties["duration_seconds"]["minimum"] == 4.0
    assert shot_properties["duration_seconds"]["maximum"] == 10.0


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["characters"][0].update({"age": 16}),
        lambda value: value["shots"][0].update({"title": "   "}),
        lambda value: value["shots"][0].update({"index": "1"}),
        lambda value: value["shots"][0].update({"duration_seconds": 3.9}),
        lambda value: value["shots"][0].update({"duration_seconds": 10.1}),
        lambda value: value["shots"][1].update({"index": 4}),
        lambda value: value["shots"][0].update({"scene_id": "scene_missing"}),
        lambda value: value["shots"][0].update(
            {"character_ids": ["character_missing"]}
        ),
        lambda value: value["shots"][0].update(
            {"character_ids": ["character_01", "character_01"]}
        ),
        lambda value: value["shots"][0].update(
            {"narration": "字" * 36, "duration_seconds": 7.0}
        ),
    ],
    ids=[
        "root-extra",
        "nested-extra",
        "blank",
        "strict-type",
        "shot-too-short",
        "shot-too-long",
        "non-contiguous-index",
        "unknown-scene",
        "unknown-character",
        "duplicate-character-reference",
        "narration-too-long",
    ],
)
def test_script_v1_rejects_invalid_fields_and_references(mutator: Callable[[dict], None]) -> None:
    payload = valid_script_payload()
    mutator(payload)
    with pytest.raises(ValidationError):
        ScriptV1.model_validate(payload)


def test_script_v1_rejects_shot_count_and_total_duration_boundaries() -> None:
    too_few = valid_script_payload()
    too_few["shots"] = too_few["shots"][:2]
    too_few["scenes"] = too_few["scenes"][:2]
    with pytest.raises(ValidationError):
        ScriptV1.model_validate(too_few)

    too_many = valid_script_payload()
    for index in (5, 6):
        too_many["scenes"].append(
            {
                "id": f"scene_{index:02d}",
                "name": f"场景 {index}",
                "description": "额外原创场景。",
                "time": "黎明",
                "lighting": "暖色晨光",
                "consistency_prompt": "原创二维动漫额外场景，保持统一色彩。",
            }
        )
        too_many["shots"].append(
            {
                **copy.deepcopy(too_many["shots"][0]),
                "id": f"shot_{index:02d}",
                "index": index,
                "scene_id": f"scene_{index:02d}",
            }
        )
    with pytest.raises(ValidationError):
        ScriptV1.model_validate(too_many)

    total_too_short = valid_script_payload()
    total_too_short["shots"] = total_too_short["shots"][:3]
    total_too_short["scenes"] = total_too_short["scenes"][:3]
    for shot in total_too_short["shots"]:
        shot["duration_seconds"] = 4.0
    with pytest.raises(ValidationError, match="20—40"):
        ScriptV1.model_validate(total_too_short)

    total_too_long = valid_script_payload()
    total_too_long["scenes"].append(
        {
            "id": "scene_05",
            "name": "场景 5",
            "description": "额外原创场景。",
            "time": "黎明",
            "lighting": "暖色晨光",
            "consistency_prompt": "原创二维动漫额外场景，保持统一色彩。",
        }
    )
    total_too_long["shots"].append(
        {
            **copy.deepcopy(total_too_long["shots"][0]),
            "id": "shot_05",
            "index": 5,
            "scene_id": "scene_05",
        }
    )
    for shot in total_too_long["shots"]:
        shot["duration_seconds"] = 9.0
    with pytest.raises(ValidationError, match="20—40"):
        ScriptV1.model_validate(total_too_long)


def test_script_v1_rejects_duplicate_entities() -> None:
    duplicate = valid_script_payload()
    duplicate["characters"].append(copy.deepcopy(duplicate["characters"][0]))
    with pytest.raises(ValidationError, match="character_id"):
        ScriptV1.model_validate(duplicate)


def test_script_v1_accepts_and_reports_unused_entities_without_pruning() -> None:
    payload = valid_script_payload()
    payload["characters"].append(
        {
            "id": "character_02",
            "name": "未出场者",
            "role": "未出场的备用角色",
            "appearance": "原创角色，短发，清晰自然的面部特征。",
            "personality": "安静",
            "costume": "深色外套",
            "consistency_prompt": "同一原创备用角色，短发与深色外套保持一致。",
        }
    )
    payload["scenes"].append(
        {
            "id": "scene_unused",
            "name": "备用天台",
            "description": "尚未进入当前分镜的原创天台场景。",
            "time": "黎明",
            "lighting": "柔和晨光",
            "consistency_prompt": "原创二维动画天台，黎明柔光，固定构图。",
        }
    )

    script = ScriptV1.model_validate(payload)
    warnings = analyze_script_usage(script)

    assert warnings.unused_scene_ids == ["scene_unused"]
    assert warnings.unused_character_ids == ["character_02"]
    assert [scene.id for scene in script.scenes][-1] == "scene_unused"
    assert [character.id for character in script.characters][-1] == "character_02"


def test_mock_provider_uses_script_v1_for_demo_and_generic_story(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    provider = MockScriptProvider(root)

    demo = provider.generate(title="纸鹤的夜航", story="纸鹤飞向黎明。")
    assert demo.provider_id == "mock"
    assert isinstance(demo.script, ScriptV1)
    assert demo.script.title == "纸鹤的夜航"
    assert len(demo.shots) == 4
    assert all(shot.camera and shot.image_prompt for shot in demo.shots)
    ScriptV1.model_validate(demo.script.model_dump(mode="json"))

    generic = provider.generate(title="星灯", story="孩子沿着星光寻找回家的路。")
    assert isinstance(generic.script, ScriptV1)
    assert generic.script.title == "星灯"
    assert generic.trace is not None
    assert generic.trace["provider_id"] == "mock"
    assert generic.trace["source_type"] == "DETERMINISTIC_FALLBACK"
    assert generic.trace["schema_version"] == "script.v1"
    assert generic.trace["fixture"] == "generic"
    assert generic.trace["desired_shot_count"] is None
    assert generic.trace["actual_shot_count"] == 4
    assert generic.trace["duration_normalization"]["normalized"] is False


def test_llamacpp_valid_response_uses_json_schema_and_writes_full_trace(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    content = json.dumps(valid_script_payload(), ensure_ascii=False)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=completion_envelope(content),
            headers={"x-request-id": "server-request-1"},
        )

    provider = make_provider(tmp_path, handler, api_key="do-not-persist-this-secret")
    result = provider.generate(title="纸鹤的夜航", story="纸鹤飞向黎明。")

    assert result.provider_id == "llamacpp"
    assert result.source_type == "LOCAL_MODEL"
    assert isinstance(result.script, ScriptV1)
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    request_payload = json.loads(requests[0].content)
    assert request_payload["stream"] is False
    assert request_payload["chat_template_kwargs"] == {"enable_thinking": False}
    response_format = request_payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False

    trace = provider.last_trace
    assert trace is not None
    assert trace["status"] == "SUCCEEDED"
    assert trace["repair_used"] is False
    assert trace["elapsed_ms"] >= 0
    assert trace["attempts"][0]["validation"]["status"] == "VALID"
    assert trace["attempts"][0]["server_request_id"] == "server-request-1"
    raw_path = Path(trace["attempts"][0]["raw_response_path"])
    assert raw_path.is_file() and raw_path.stat().st_size > 0
    assert provider.last_trace_path is not None and provider.last_trace_path.is_file()
    assert "do-not-persist-this-secret" not in provider.last_trace_path.read_text(
        encoding="utf-8"
    )


def test_llamacpp_allows_exactly_one_repair_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    valid_content = json.dumps(valid_script_payload(), ensure_ascii=False)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = "```json\n{}\n```" if len(requests) == 1 else valid_content
        return httpx.Response(200, json=completion_envelope(content))

    provider = make_provider(tmp_path, handler)
    result = provider.generate(title="夜航", story="少女与纸鹤飞过夜空。")

    assert isinstance(result.script, ScriptV1)
    assert len(requests) == 2
    assert provider.last_trace is not None
    assert provider.last_trace["repair_used"] is True
    assert [item["kind"] for item in provider.last_trace["attempts"]] == [
        "initial",
        "repair",
    ]
    repair_payload = json.loads(requests[1].content)
    assert "校验错误" in repair_payload["messages"][1]["content"]


def test_llamacpp_repairs_narration_with_explicit_bounded_constraints(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    invalid = valid_script_payload()
    invalid["shots"] = invalid["shots"][:3]
    invalid["scenes"] = invalid["scenes"][:3]
    invalid["shots"][0]["narration"] = "字" * 60
    repaired = copy.deepcopy(invalid)
    repaired["shots"][0]["narration"] = "少女在雨夜发现发光的纸飞机。"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = invalid if len(requests) == 1 else repaired
        return httpx.Response(
            200,
            json=completion_envelope(json.dumps(payload, ensure_ascii=False)),
        )

    provider = make_provider(tmp_path, handler)
    result = provider.generate(
        title="雨夜车站",
        story="雨夜里，少女在车站发现了一只发光的纸飞机。",
        desired_shot_count=3,
    )

    assert len(requests) == 2
    assert len(result.script.shots) == 3
    assert result.script.shots[0].narration == repaired["shots"][0]["narration"]
    repair_text = json.loads(requests[1].content)["messages"][1]["content"]
    assert '"shot_id":"shot_01"' in repair_text
    assert '"shot_duration_seconds":7.0' in repair_text
    assert '"current_narration_characters":60' in repair_text
    assert '"maximum_narration_characters":35' in repair_text
    assert "maximum_narration_characters 是最大安全容量而非建议写满的目标" in repair_text
    assert "不用旁白填满镜头时长" in repair_text
    assert "允许保留没有 narration 的视觉时间" in repair_text
    assert '"narration_concision_applies_only_to":"narration"' in repair_text
    assert '"preserve_visual_description_when_valid":true' in repair_text
    assert '"preserve_image_prompt_when_valid":true' in repair_text
    assert "不得同步压缩 visual_description 或 image_prompt" in repair_text
    assert "image_prompt 继续以 visual_description 为主要视觉来源" in repair_text
    assert "保留原意" in repair_text
    assert "不增加新剧情" in repair_text
    assert "只从原始故事已有剧情事实中抽取、概括和精炼" in repair_text
    assert "不重新创作完整故事" in repair_text
    assert "优先删除属于 visual_description" in repair_text
    assert "其次将复杂旁白概括成更短的剧情事实表达" in repair_text
    assert "不把视觉细节换一种说法继续写回 narration" in repair_text
    assert "不得为了压缩而删除必要剧情事件" in repair_text
    assert "不得修改镜头数量" in repair_text
    assert "ScriptV1" in repair_text
    assert "纯 JSON" in repair_text


def test_llamacpp_allows_one_additional_bounded_repair(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    invalid = valid_script_payload()
    invalid["shots"][0]["narration"] = "字" * 60
    still_invalid = copy.deepcopy(invalid)
    still_invalid["shots"][0]["narration"] = "更" * 60
    repaired = valid_script_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = (
            invalid
            if len(requests) == 1
            else still_invalid
            if len(requests) == 2
            else repaired
        )
        return httpx.Response(
            200,
            json=completion_envelope(json.dumps(payload, ensure_ascii=False)),
        )

    provider = make_provider(tmp_path, handler)
    result = provider.generate(title="夜航", story="少女与纸鹤飞过夜空。")

    assert isinstance(result.script, ScriptV1)
    assert len(requests) == 3
    assert provider.last_trace is not None
    assert [item["kind"] for item in provider.last_trace["attempts"]] == [
        "initial",
        "repair",
        "repair",
    ]
    assert provider.last_trace["attempts"][2]["repair_attempt"] == 2
    report_path = Path(provider.last_trace["validation_report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["repair_request_limit"] == 2
    assert len(report["repair_attempts"]) == 2
    assert (report_path.parent / "repair_2_raw_response.json").is_file()


def test_llamacpp_unused_entities_are_warnings_without_repair(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    payload = valid_script_payload()
    payload["characters"].append(
        {
            "id": "character_unused",
            "name": "未出场角色",
            "role": "备用角色",
            "appearance": "原创短发角色，面部特征清晰。",
            "personality": "安静",
            "costume": "深色外套",
            "consistency_prompt": "原创备用角色，短发与深色外套保持一致。",
        }
    )
    payload["scenes"].append(
        {
            "id": "scene_unused",
            "name": "未使用场景",
            "description": "当前镜头未采用的备用场景。",
            "time": "黎明",
            "lighting": "柔和晨光",
            "consistency_prompt": "原创备用场景，黎明柔光。",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=completion_envelope(json.dumps(payload, ensure_ascii=False)),
        )

    provider = make_provider(tmp_path, handler)
    result = provider.generate(title="夜航", story="纸鹤飞过夜空。")

    assert len(requests) == 1
    assert len(result.script.scenes) == len(payload["scenes"])
    assert len(result.script.characters) == len(payload["characters"])
    assert provider.last_trace is not None
    assert provider.last_trace["repair_used"] is False
    assert provider.last_trace["validation_warnings"] == {
        "unused_scene_ids": ["scene_unused"],
        "unused_character_ids": ["character_unused"],
    }
    assert provider.last_trace["attempts"][0]["validation"]["warnings"] == (
        provider.last_trace["validation_warnings"]
    )
    assert "unused_scene_ids=['scene_unused']" in caplog.text


@pytest.mark.parametrize(
    "invalid_content",
    [
        "```json\n{}\n```",
        "<think>先思考</think>{}",
        "下面是结果：{}",
        '{"schema_version":"script.v1","schema_version":"script.v1"}',
        '{"value":NaN}',
    ],
    ids=["code-fence", "think", "explanation-prefix", "duplicate-key", "nan"],
)
def test_llamacpp_rejects_non_pure_output_without_mock_fallback(
    tmp_path: Path,
    invalid_content: str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion_envelope(invalid_content))

    provider = make_provider(tmp_path, handler)
    with pytest.raises(LlamaCppOutputError, match="未执行 Mock 回退"):
        provider.generate(title="夜航", story="纸鹤飞过夜空。")
    assert calls == 3
    assert provider.last_script is None
    assert provider.last_trace is not None
    assert provider.last_trace["status"] == "FAILED"
    assert len(provider.last_trace["attempts"]) == 3
    assert len(list((tmp_path / "raw-responses").rglob("attempt_*.response.bin"))) == 3


def test_llamacpp_repairs_schema_invalid_pure_json(tmp_path: Path) -> None:
    calls = 0
    invalid = valid_script_payload()
    invalid["shots"][1]["index"] = 4

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = invalid if calls == 1 else valid_script_payload()
        return httpx.Response(
            200,
            json=completion_envelope(json.dumps(payload, ensure_ascii=False)),
        )

    provider = make_provider(tmp_path, handler)
    assert provider.generate(title="夜航", story="纸鹤飞过夜空。").script is not None
    assert calls == 2


def test_llamacpp_http_error_is_not_retried_and_raw_response_is_saved(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="server unavailable")

    provider = make_provider(tmp_path, handler)
    with pytest.raises(LlamaCppTransportError, match="HTTP 503"):
        provider.generate(title="夜航", story="纸鹤飞过夜空。")
    assert calls == 1
    assert provider.last_trace is not None
    assert provider.last_trace["status"] == "FAILED"
    raw_path = Path(provider.last_trace["attempts"][0]["raw_response_path"])
    assert raw_path.read_bytes() == b"server unavailable"


def test_llamacpp_protocol_error_is_not_repaired(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    provider = make_provider(tmp_path, handler)
    with pytest.raises(LlamaCppProtocolError, match="choices"):
        provider.generate(title="夜航", story="纸鹤飞过夜空。")
    assert calls == 1
    assert provider.last_trace is not None
    assert provider.last_trace["status"] == "FAILED"


def test_llamacpp_connection_error_is_not_retried(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    provider = make_provider(tmp_path, handler)
    with pytest.raises(LlamaCppTransportError, match="无法调用"):
        provider.generate(title="夜航", story="纸鹤飞过夜空。")
    assert calls == 1
    assert provider.last_trace is not None
    assert provider.last_trace["status"] == "FAILED"
    assert provider.last_trace["attempts"][0]["transport_error"]


def test_parse_pure_script_json_accepts_outer_whitespace_only() -> None:
    content = " \n" + json.dumps(valid_script_payload(), ensure_ascii=False) + "\r\n "
    assert parse_pure_script_json(content).schema_version == "script.v1"

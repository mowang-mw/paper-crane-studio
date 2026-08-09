from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from backend.app.script_schema import (
    DurationNormalizationError,
    ScriptV1,
    analyze_script_candidate,
    normalize_script_durations,
    script_v1_json_schema,
    validate_desired_shot_count,
)


def _payload(shot_count: int = 4, durations: list[float] | None = None) -> dict:
    durations = durations or [7.0] * shot_count
    return {
        "schema_version": "script.v1",
        "title": "纸鹤的夜航",
        "synopsis": "少女与纸鹤完成一次夜航。",
        "characters": [
            {
                "id": "character_01",
                "name": "阿澄",
                "role": "主角",
                "appearance": "深色短发，眼神温和。",
                "personality": "安静而勇敢",
                "costume": "浅色居家服与深色披肩",
                "consistency_prompt": "同一原创少女，造型保持一致。",
            }
        ],
        "scenes": [
            {
                "id": f"scene_{index:02d}",
                "name": f"场景 {index}",
                "description": f"原创夜航场景 {index}。",
                "time": "夜晚",
                "lighting": "柔和月光",
                "consistency_prompt": f"原创二维动画场景 {index}，空间稳定。",
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
                "visual_description": f"少女经过第 {index} 个场景。",
                "camera": "缓慢推近",
                "image_prompt": f"原创二维动漫夜景，镜头 {index}。",
                "narration": f"纸鹤飞过第{index}盏灯。",
                "duration_seconds": durations[index - 1],
            }
            for index in range(1, shot_count + 1)
        ],
    }


@pytest.mark.parametrize("value", [None, 3, 4, 5])
def test_desired_shot_count_accepts_only_contract_values(value: int | None) -> None:
    assert validate_desired_shot_count(value) == value


@pytest.mark.parametrize("value", [True, False, 2, 6, 4.0, "4"])
def test_desired_shot_count_rejects_invalid_or_coerced_values(value: object) -> None:
    with pytest.raises(ValueError, match="3、4、5"):
        validate_desired_shot_count(value)


def test_auto_mode_accepts_three_to_five_and_fixed_mode_requires_exact_count() -> None:
    for shot_count in (3, 4, 5):
        payload = _payload(shot_count, [8.0] * shot_count)
        assert analyze_script_candidate(payload).valid is True

    mismatch = analyze_script_candidate(_payload(), desired_shot_count=5)
    assert mismatch.valid is False
    assert mismatch.actual_shot_count == 4
    assert mismatch.diagnostics[0].stage == "SHOT_COUNT_VALIDATION"
    assert mismatch.diagnostics[0].code == "SHOT_COUNT_MISMATCH"
    assert "恰好生成 5 个镜头" in mismatch.diagnostics[0].summary


def test_fixed_json_schema_has_exact_shot_count_without_changing_default() -> None:
    fixed = script_v1_json_schema(4)
    automatic = script_v1_json_schema()
    assert fixed["properties"]["shots"]["minItems"] == 4
    assert fixed["properties"]["shots"]["maxItems"] == 4
    assert automatic["properties"]["shots"]["minItems"] == 3
    assert automatic["properties"]["shots"]["maxItems"] == 5


def test_candidate_analysis_distinguishes_structure_reference_and_duration() -> None:
    unknown_field = _payload()
    unknown_field["shots"][0]["surprise"] = True
    structure = analyze_script_candidate(unknown_field)
    assert structure.duration_only is False
    assert structure.diagnostics[0].stage == "SCRIPT_SCHEMA_VALIDATION"
    assert structure.diagnostics[0].code == "UNKNOWN_FIELD"
    assert structure.diagnostics[0].summary.startswith("包含 ScriptV1 未定义的字段")

    bad_reference = _payload()
    bad_reference["shots"][0]["scene_id"] = "scene_missing"
    reference = analyze_script_candidate(bad_reference)
    assert reference.duration_only is False
    assert reference.diagnostics[0].stage == "SCRIPT_REFERENCE_VALIDATION"
    assert "不存在的场景" in reference.diagnostics[0].summary

    bad_duration = _payload(durations=[2.0, 13.0, 3.0, 12.0])
    duration = analyze_script_candidate(bad_duration)
    assert duration.duration_only is True
    assert duration.normalizable_duration_only is True
    assert all(
        item.stage == "DURATION_VALIDATION"
        for item in duration.diagnostics
    )


def test_normalization_is_proportional_deterministic_and_traceable() -> None:
    payload = _payload(durations=[2.0, 4.0, 8.0, 14.0])
    first = normalize_script_durations(payload, desired_shot_count=4)
    second = normalize_script_durations(payload, desired_shot_count=4)

    assert first == second
    assert first.normalized is True
    assert first.original_durations == [2.0, 4.0, 8.0, 14.0]
    assert first.original_total == 28.0
    assert first.normalized_durations == [4.0, 4.7, 9.3, 10.0]
    assert first.normalized_total == 28.0
    assert sum(first.normalized_durations) == 28.0
    assert all(4.0 <= value <= 10.0 for value in first.normalized_durations)
    assert [shot.id for shot in first.script.shots] == [
        "shot_01",
        "shot_02",
        "shot_03",
        "shot_04",
    ]
    assert "未增删、合并或重排镜头" in first.reason
    ScriptV1.model_validate(first.script.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("shot_count", "target"),
    [(3, 24.0), (4, 28.0), (5, 35.0)],
)
def test_normalization_targets_by_shot_count(shot_count: int, target: float) -> None:
    durations = [3.0 + index for index in range(shot_count)]
    result = normalize_script_durations(_payload(shot_count, durations))
    assert result.normalized_total == target
    assert all(4.0 <= value <= 10.0 for value in result.normalized_durations)


def test_normalization_expands_preferred_total_to_narration_minimum() -> None:
    payload = _payload(3, [8.0, 6.0, 6.0])
    for shot, character_count in zip(
        payload["shots"],
        [39, 39, 47],
        strict=True,
    ):
        shot["narration"] = "字" * character_count

    analysis = analyze_script_candidate(payload, desired_shot_count=3)
    result = normalize_script_durations(payload, desired_shot_count=3)

    assert analysis.duration_only is True
    assert analysis.normalizable_duration_only is True
    assert result.original_durations == [8.0, 6.0, 6.0]
    assert result.normalized_durations == [7.8, 7.8, 9.4]
    assert result.normalized_total == 25.0
    assert "优选目标 24 秒" in result.reason
    assert "最小可行总时长 25 秒" in result.reason
    ScriptV1.model_validate(result.script.model_dump(mode="json"))


def test_normalization_still_rejects_narration_over_single_shot_hard_limit() -> None:
    payload = _payload(3, [8.0, 6.0, 6.0])
    payload["shots"][0]["narration"] = "字" * 51

    analysis = analyze_script_candidate(payload, desired_shot_count=3)

    assert analysis.duration_only is True
    assert analysis.normalizable_duration_only is False
    with pytest.raises(DurationNormalizationError, match="10 秒"):
        normalize_script_durations(payload, desired_shot_count=3)


def test_normalization_still_rejects_required_total_over_hard_limit() -> None:
    payload = _payload(5, [8.0] * 5)
    for shot in payload["shots"]:
        shot["narration"] = "字" * 41

    analysis = analyze_script_candidate(payload, desired_shot_count=5)

    assert analysis.duration_only is True
    assert analysis.normalizable_duration_only is False
    with pytest.raises(DurationNormalizationError, match="40 秒业务上限"):
        normalize_script_durations(payload, desired_shot_count=5)


def test_normalization_never_mutates_or_masks_structural_errors() -> None:
    payload = _payload(durations=[3.0, 6.0, 9.0, 12.0])
    original = copy.deepcopy(payload)
    normalize_script_durations(payload)
    assert payload == original

    payload["shots"][0]["scene_id"] = "scene_missing"
    with pytest.raises(DurationNormalizationError, match="结构或引用"):
        normalize_script_durations(payload)


def test_normalization_never_masks_fixed_shot_count_mismatch() -> None:
    with pytest.raises(DurationNormalizationError, match="恰好生成 5 个镜头"):
        normalize_script_durations(
            _payload(durations=[2.0, 3.0, 12.0, 13.0]),
            desired_shot_count=5,
        )


def test_strict_script_v1_remains_the_final_gate() -> None:
    invalid = _payload(durations=[3.0, 7.0, 7.0, 7.0])
    with pytest.raises(ValidationError):
        ScriptV1.model_validate(invalid)

    normalized = normalize_script_durations(invalid)
    assert isinstance(normalized.script, ScriptV1)

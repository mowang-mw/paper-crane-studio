"""M3 结构化剧本的单一权威契约。"""

from __future__ import annotations

import copy
import math
import re
from decimal import Decimal, ROUND_FLOOR
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


NARRATION_MAX_CHARACTERS_PER_SECOND = 5
"""中文旁白上限：每秒最多 5 个非空白字符，给正常语速留出余量。"""

SHOT_COUNT_MIN = 3
SHOT_COUNT_MAX = 5
SHOT_DURATION_MIN_SECONDS = 4.0
SHOT_DURATION_MAX_SECONDS = 10.0
SCRIPT_DURATION_MIN_SECONDS = 20.0
SCRIPT_DURATION_MAX_SECONDS = 40.0
NORMALIZED_TOTAL_BY_SHOT_COUNT = {3: 24.0, 4: 28.0, 5: 35.0}

DesiredShotCount = Literal[3, 4, 5] | None
ValidationStage = Literal[
    "SCRIPT_SCHEMA_VALIDATION",
    "SCRIPT_REFERENCE_VALIDATION",
    "SHOT_COUNT_VALIDATION",
    "DURATION_VALIDATION",
]

EntityId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="稳定的 ASCII 标识，必须以英文字母开头。",
    ),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


class Character(_StrictModel):
    id: EntityId
    name: Annotated[str, Field(min_length=1, max_length=80)]
    role: Annotated[str, Field(min_length=1, max_length=200)]
    appearance: Annotated[str, Field(min_length=1, max_length=500)]
    personality: Annotated[str, Field(min_length=1, max_length=300)]
    costume: Annotated[str, Field(min_length=1, max_length=300)]
    consistency_prompt: Annotated[str, Field(min_length=1, max_length=800)]


class Scene(_StrictModel):
    id: EntityId
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    time: Annotated[str, Field(min_length=1, max_length=120)]
    lighting: Annotated[str, Field(min_length=1, max_length=300)]
    consistency_prompt: Annotated[str, Field(min_length=1, max_length=800)]


class ScriptValidationWarnings(_StrictModel):
    """不影响 ScriptV1 合法性的引用覆盖率分析结果。"""

    unused_scene_ids: list[EntityId] = Field(default_factory=list)
    unused_character_ids: list[EntityId] = Field(default_factory=list)


class ScriptValidationDiagnostic(_StrictModel):
    """可以安全写入 Job、追溯文件和前端响应的单条中文诊断。"""

    code: str
    stage: ValidationStage
    summary: str
    path: str | None = None


class Shot(_StrictModel):
    id: EntityId
    index: Annotated[int, Field(ge=1, le=5)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    scene_id: EntityId
    character_ids: Annotated[list[EntityId], Field(min_length=1, max_length=8)]
    visual_description: Annotated[str, Field(min_length=1, max_length=800)]
    camera: Annotated[str, Field(min_length=1, max_length=120)]
    image_prompt: Annotated[str, Field(min_length=1, max_length=1200)]
    negative_prompt: Annotated[str, Field(max_length=800)] | None = None
    narration: Annotated[str, Field(min_length=1, max_length=200)]
    duration_seconds: Annotated[
        float,
        Field(
            ge=SHOT_DURATION_MIN_SECONDS,
            le=SHOT_DURATION_MAX_SECONDS,
            allow_inf_nan=False,
        ),
    ]

    @model_validator(mode="after")
    def validate_local_rules(self) -> "Shot":
        _validate_unique_character_references(self)
        narration_characters = _narration_character_count(self.narration)
        maximum = int(self.duration_seconds * NARRATION_MAX_CHARACTERS_PER_SECOND)
        if narration_characters > maximum:
            raise ValueError(
                "旁白过长：按中文旁白每秒最多 "
                f"{NARRATION_MAX_CHARACTERS_PER_SECOND} 个非空白字符计算，"
                f"{self.duration_seconds:g} 秒镜头最多 {maximum} 字，"
                f"实际 {narration_characters} 字"
            )
        return self


class ScriptV1(_StrictModel):
    """持久化和媒体渲染前必须通过的严格最终契约。"""

    schema_version: Literal["script.v1"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    synopsis: Annotated[str, Field(min_length=1, max_length=1000)]
    characters: Annotated[list[Character], Field(min_length=1, max_length=8)]
    scenes: Annotated[list[Scene], Field(min_length=1, max_length=8)]
    shots: Annotated[
        list[Shot],
        Field(min_length=SHOT_COUNT_MIN, max_length=SHOT_COUNT_MAX),
    ]

    @model_validator(mode="after")
    def validate_cross_references_and_timeline(self) -> "ScriptV1":
        _validate_cross_references_and_indices(self)
        total_duration = sum(item.duration_seconds for item in self.shots)
        if not SCRIPT_DURATION_MIN_SECONDS <= total_duration <= SCRIPT_DURATION_MAX_SECONDS:
            raise ValueError(
                "镜头总时长必须在 20—40 秒之间，"
                f"实际 {total_duration:g} 秒"
            )
        return self


class _DurationRelaxedShot(_StrictModel):
    """候选分析专用：只放宽时长，其他 Shot 字段仍与 ScriptV1 一致。"""

    id: EntityId
    index: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    scene_id: EntityId
    character_ids: Annotated[list[EntityId], Field(min_length=1, max_length=8)]
    visual_description: Annotated[str, Field(min_length=1, max_length=800)]
    camera: Annotated[str, Field(min_length=1, max_length=120)]
    image_prompt: Annotated[str, Field(min_length=1, max_length=1200)]
    negative_prompt: Annotated[str, Field(max_length=800)] | None = None
    narration: Annotated[str, Field(min_length=1, max_length=200)]
    duration_seconds: Annotated[float, Field(allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_local_rules(self) -> "_DurationRelaxedShot":
        _validate_unique_character_references(self)
        return self


class _DurationRelaxedScript(_StrictModel):
    """候选分析专用：不放宽字段、唯一性、顺序或引用规则。"""

    schema_version: Literal["script.v1"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    synopsis: Annotated[str, Field(min_length=1, max_length=1000)]
    characters: Annotated[list[Character], Field(min_length=1, max_length=8)]
    scenes: Annotated[list[Scene], Field(min_length=1, max_length=8)]
    shots: Annotated[list[_DurationRelaxedShot], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_cross_references_and_timeline(self) -> "_DurationRelaxedScript":
        _validate_cross_references_and_indices(self)
        return self


class ScriptCandidateAnalysis(_StrictModel):
    """一次纯 JSON 候选的分阶段验证结果。"""

    valid: bool
    script: ScriptV1 | None = None
    diagnostics: list[ScriptValidationDiagnostic] = Field(default_factory=list)
    duration_only: bool = False
    normalizable_duration_only: bool = False
    desired_shot_count: DesiredShotCount = None
    actual_shot_count: int | None = None


class DurationNormalizationResult(_StrictModel):
    """确定性时长规范化的完整可追溯结果。"""

    normalized: bool
    script: ScriptV1
    original_durations: list[float]
    normalized_durations: list[float]
    original_total: float
    normalized_total: float
    reason: str


class DurationNormalizationError(ValueError):
    """候选不是纯时长问题，或无法在不改剧情结构时规范化。"""


def _narration_character_count(narration: str) -> int:
    return len(re.sub(r"\s+", "", narration))


def _validate_unique_character_references(
    shot: Shot | _DurationRelaxedShot,
) -> None:
    if len(set(shot.character_ids)) != len(shot.character_ids):
        raise ValueError("character_ids 不得重复")


def _validate_cross_references_and_indices(
    script: ScriptV1 | _DurationRelaxedScript,
) -> None:
    character_ids = [item.id for item in script.characters]
    scene_ids = [item.id for item in script.scenes]
    shot_ids = [item.id for item in script.shots]
    if len(set(character_ids)) != len(character_ids):
        raise ValueError("character_id 必须唯一")
    if len(set(scene_ids)) != len(scene_ids):
        raise ValueError("scene_id 必须唯一")
    if len(set(shot_ids)) != len(shot_ids):
        raise ValueError("shot_id 必须唯一")

    actual_indices = [item.index for item in script.shots]
    expected_indices = list(range(1, len(script.shots) + 1))
    if actual_indices != expected_indices:
        raise ValueError(
            "镜头 index 必须按输入顺序从 1 连续递增；"
            f"预期 {expected_indices}，实际 {actual_indices}"
        )

    known_characters = set(character_ids)
    known_scenes = set(scene_ids)
    for shot in script.shots:
        unknown_characters = set(shot.character_ids) - known_characters
        if unknown_characters:
            raise ValueError(
                f"镜头 {shot.id} 引用了不存在的角色："
                f"{sorted(unknown_characters)}"
            )
        if shot.scene_id not in known_scenes:
            raise ValueError(
                f"镜头 {shot.id} 引用了不存在的场景：{shot.scene_id}"
            )


def validate_desired_shot_count(value: object) -> DesiredShotCount:
    """严格校验生成参数；自动模式为 ``None``，固定模式只能为 3、4、5。"""

    if value is None:
        return None
    if type(value) is not int or value not in NORMALIZED_TOTAL_BY_SHOT_COUNT:
        raise ValueError("desired_shot_count 只能是 3、4、5 或 null（自动）")
    return value


def _format_validation_path(location: tuple[object, ...]) -> str:
    if not location:
        return "$"
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _strip_value_error_prefix(message: str) -> str:
    prefix = "Value error, "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _pydantic_diagnostics(
    error: ValidationError,
) -> list[ScriptValidationDiagnostic]:
    diagnostics: list[ScriptValidationDiagnostic] = []
    for item in error.errors(include_url=False, include_context=False):
        error_type = str(item["type"])
        path = _format_validation_path(tuple(item["loc"]))
        raw_message = _strip_value_error_prefix(str(item["msg"]))
        reference_error = (
            "引用了不存在的角色" in raw_message
            or "引用了不存在的场景" in raw_message
        )
        stage: ValidationStage = (
            "SCRIPT_REFERENCE_VALIDATION"
            if reference_error
            else "SCRIPT_SCHEMA_VALIDATION"
        )

        if error_type == "missing":
            summary = f"缺少必填字段：{path}"
            code = "MISSING_FIELD"
        elif error_type == "extra_forbidden":
            summary = f"包含 ScriptV1 未定义的字段：{path}"
            code = "UNKNOWN_FIELD"
        elif error_type in {"string_too_short", "too_short"}:
            summary = f"字段为空或数量不足：{path}"
            code = "FIELD_TOO_SHORT"
        elif error_type in {"string_too_long", "too_long"}:
            summary = f"字段内容或数量超出上限：{path}"
            code = "FIELD_TOO_LONG"
        elif error_type == "string_pattern_mismatch":
            summary = f"标识符格式不合法：{path}"
            code = "INVALID_IDENTIFIER"
        elif error_type == "literal_error":
            summary = f"固定版本字段取值不正确：{path}"
            code = "INVALID_LITERAL"
        elif error_type.endswith("_type"):
            summary = f"字段类型不正确：{path}"
            code = "INVALID_FIELD_TYPE"
        elif error_type == "finite_number":
            summary = f"时长必须是有限数字：{path}"
            code = "INVALID_DURATION_NUMBER"
            stage = "DURATION_VALIDATION"
        elif error_type == "value_error":
            summary = raw_message
            code = "INVALID_REFERENCE" if reference_error else "SCHEMA_RULE_VIOLATION"
        else:
            summary = f"字段不符合 ScriptV1 约束：{path}"
            code = "SCRIPT_SCHEMA_INVALID"

        diagnostics.append(
            ScriptValidationDiagnostic(
                code=code,
                stage=stage,
                summary=summary,
                path=path,
            )
        )
    return diagnostics


def _shot_count_diagnostic(
    actual: int,
    desired: DesiredShotCount,
) -> ScriptValidationDiagnostic | None:
    if desired is not None and actual != desired:
        return ScriptValidationDiagnostic(
            code="SHOT_COUNT_MISMATCH",
            stage="SHOT_COUNT_VALIDATION",
            summary=f"要求恰好生成 {desired} 个镜头，当前为 {actual} 个",
            path="$.shots",
        )
    if not SHOT_COUNT_MIN <= actual <= SHOT_COUNT_MAX:
        return ScriptValidationDiagnostic(
            code="SHOT_COUNT_OUT_OF_RANGE",
            stage="SHOT_COUNT_VALIDATION",
            summary=f"自动模式要求 3—5 个镜头，当前为 {actual} 个",
            path="$.shots",
        )
    return None


def _duration_diagnostics(
    script: _DurationRelaxedScript,
) -> list[ScriptValidationDiagnostic]:
    diagnostics: list[ScriptValidationDiagnostic] = []
    for position, shot in enumerate(script.shots):
        path = f"$.shots[{position}].duration_seconds"
        if not (
            SHOT_DURATION_MIN_SECONDS
            <= shot.duration_seconds
            <= SHOT_DURATION_MAX_SECONDS
        ):
            diagnostics.append(
                ScriptValidationDiagnostic(
                    code="SHOT_DURATION_OUT_OF_RANGE",
                    stage="DURATION_VALIDATION",
                    summary=(
                        f"镜头 {shot.id} 时长必须在 4—10 秒之间，"
                        f"当前为 {shot.duration_seconds:g} 秒"
                    ),
                    path=path,
                )
            )
        narration_characters = _narration_character_count(shot.narration)
        maximum = int(
            max(shot.duration_seconds, 0)
            * NARRATION_MAX_CHARACTERS_PER_SECOND
        )
        if narration_characters > maximum:
            diagnostics.append(
                ScriptValidationDiagnostic(
                    code="NARRATION_TOO_LONG_FOR_DURATION",
                    stage="DURATION_VALIDATION",
                    summary=(
                        f"镜头 {shot.id} 的旁白按当前时长最多容纳 {maximum} 字，"
                        f"实际为 {narration_characters} 字"
                    ),
                    path=f"$.shots[{position}].narration",
                )
            )

    total = sum(shot.duration_seconds for shot in script.shots)
    if not SCRIPT_DURATION_MIN_SECONDS <= total <= SCRIPT_DURATION_MAX_SECONDS:
        diagnostics.append(
            ScriptValidationDiagnostic(
                code="TOTAL_DURATION_OUT_OF_RANGE",
                stage="DURATION_VALIDATION",
                summary=f"镜头总时长必须在 20—40 秒之间，当前为 {total:g} 秒",
                path="$.shots",
            )
        )
    return diagnostics


def _duration_lower_bound_tenths(shot: _DurationRelaxedShot) -> int:
    narration_characters = _narration_character_count(shot.narration)
    narration_tenths = narration_characters * 2
    return max(int(SHOT_DURATION_MIN_SECONDS * 10), narration_tenths)


def _proportional_duration_tenths(
    script: _DurationRelaxedScript,
) -> list[int]:
    shot_count = len(script.shots)
    target_tenths = int(NORMALIZED_TOTAL_BY_SHOT_COUNT[shot_count] * 10)
    lower_bounds = [_duration_lower_bound_tenths(shot) for shot in script.shots]
    upper_bound = int(SHOT_DURATION_MAX_SECONDS * 10)

    if any(not math.isfinite(shot.duration_seconds) for shot in script.shots):
        raise DurationNormalizationError("时长包含非有限数字，无法规范化")
    if any(shot.duration_seconds <= 0 for shot in script.shots):
        raise DurationNormalizationError("时长必须大于 0，无法按比例规范化")
    if any(lower > upper_bound for lower in lower_bounds):
        raise DurationNormalizationError(
            "至少一个镜头的旁白即使按 10 秒计算仍然过长，无法仅调整时长"
        )
    if sum(lower_bounds) > target_tenths:
        raise DurationNormalizationError(
            "旁白所需的最短总时长超过本镜头数量的规范化目标，无法仅调整时长"
        )

    weights = [Decimal(str(shot.duration_seconds)) for shot in script.shots]
    allocations: list[Decimal | None] = [None] * shot_count
    active = set(range(shot_count))
    remaining = Decimal(target_tenths)

    while active:
        total_weight = sum(weights[index] for index in active)
        if total_weight <= 0:
            raise DurationNormalizationError("原始时长无法形成有效比例")
        provisional = {
            index: remaining * weights[index] / total_weight
            for index in active
        }
        clamped = False
        for index in sorted(active):
            lower = Decimal(lower_bounds[index])
            upper = Decimal(upper_bound)
            if provisional[index] < lower:
                allocations[index] = lower
                remaining -= lower
                active.remove(index)
                clamped = True
            elif provisional[index] > upper:
                allocations[index] = upper
                remaining -= upper
                active.remove(index)
                clamped = True
        if not clamped:
            for index in active:
                allocations[index] = provisional[index]
            break

    exact_allocations = [
        value if value is not None else Decimal(0)
        for value in allocations
    ]
    rounded = [
        max(
            lower_bounds[index],
            int(value.to_integral_value(rounding=ROUND_FLOOR)),
        )
        for index, value in enumerate(exact_allocations)
    ]
    remainder = target_tenths - sum(rounded)
    order = sorted(
        range(shot_count),
        key=lambda index: (
            exact_allocations[index] - Decimal(rounded[index]),
            -index,
        ),
        reverse=True,
    )
    while remainder > 0:
        changed = False
        for index in order:
            if rounded[index] < upper_bound:
                rounded[index] += 1
                remainder -= 1
                changed = True
                if remainder == 0:
                    break
        if not changed:
            raise DurationNormalizationError("无法在单镜头 4—10 秒范围内分配目标总时长")
    if remainder < 0:
        raise DurationNormalizationError("时长下限超过目标总时长")
    return rounded


def _normalize_relaxed_script(
    script: _DurationRelaxedScript,
) -> DurationNormalizationResult:
    original_durations = [shot.duration_seconds for shot in script.shots]
    normalized_tenths = _proportional_duration_tenths(script)
    normalized_durations = [value / 10 for value in normalized_tenths]
    normalized_payload = script.model_dump(mode="json")
    for shot, duration in zip(
        normalized_payload["shots"],
        normalized_durations,
        strict=True,
    ):
        shot["duration_seconds"] = duration

    try:
        normalized_script = ScriptV1.model_validate(normalized_payload)
    except ValidationError as exc:
        raise DurationNormalizationError(
            "规范化后的候选仍未通过严格 ScriptV1，未保存该结果"
        ) from exc

    original_total = sum(original_durations)
    normalized_total = sum(normalized_durations)
    return DurationNormalizationResult(
        normalized=any(
            not math.isclose(before, after, abs_tol=1e-9)
            for before, after in zip(
                original_durations,
                normalized_durations,
                strict=True,
            )
        ),
        script=normalized_script,
        original_durations=original_durations,
        normalized_durations=normalized_durations,
        original_total=original_total,
        normalized_total=normalized_total,
        reason=(
            "修复后仅剩时长约束错误；"
            f"按原始时长比例缩放并以 0.1 秒确定性取整到 "
            f"{NORMALIZED_TOTAL_BY_SHOT_COUNT[len(script.shots)]:g} 秒，"
            "未增删、合并或重排镜头"
        ),
    )


def analyze_script_candidate(
    payload: object,
    desired_shot_count: DesiredShotCount = None,
) -> ScriptCandidateAnalysis:
    """按结构/引用、镜头数、时长分层分析一个已解析的 JSON 候选。"""

    desired = validate_desired_shot_count(desired_shot_count)
    try:
        relaxed = _DurationRelaxedScript.model_validate(payload)
    except ValidationError as exc:
        diagnostics = _pydantic_diagnostics(exc)
        return ScriptCandidateAnalysis(
            valid=False,
            diagnostics=diagnostics,
            duration_only=bool(diagnostics)
            and all(item.stage == "DURATION_VALIDATION" for item in diagnostics),
            normalizable_duration_only=False,
            desired_shot_count=desired,
        )

    shot_count = len(relaxed.shots)
    count_diagnostic = _shot_count_diagnostic(shot_count, desired)
    if count_diagnostic is not None:
        return ScriptCandidateAnalysis(
            valid=False,
            diagnostics=[count_diagnostic],
            desired_shot_count=desired,
            actual_shot_count=shot_count,
        )

    duration_diagnostics = _duration_diagnostics(relaxed)
    if duration_diagnostics:
        try:
            _normalize_relaxed_script(relaxed)
        except DurationNormalizationError:
            normalizable = False
        else:
            normalizable = True
        return ScriptCandidateAnalysis(
            valid=False,
            diagnostics=duration_diagnostics,
            duration_only=True,
            normalizable_duration_only=normalizable,
            desired_shot_count=desired,
            actual_shot_count=shot_count,
        )

    try:
        strict_script = ScriptV1.model_validate(relaxed.model_dump(mode="json"))
    except ValidationError as exc:
        diagnostics = _pydantic_diagnostics(exc)
        return ScriptCandidateAnalysis(
            valid=False,
            diagnostics=diagnostics,
            duration_only=bool(diagnostics)
            and all(item.stage == "DURATION_VALIDATION" for item in diagnostics),
            normalizable_duration_only=False,
            desired_shot_count=desired,
            actual_shot_count=shot_count,
        )
    return ScriptCandidateAnalysis(
        valid=True,
        script=strict_script,
        desired_shot_count=desired,
        actual_shot_count=shot_count,
    )


def validate_script_candidate(
    payload: object,
    desired_shot_count: DesiredShotCount = None,
) -> ScriptV1:
    """严格返回 ScriptV1；失败时抛出带中文分层摘要的 ValueError。"""

    analysis = analyze_script_candidate(payload, desired_shot_count)
    if analysis.script is not None:
        return analysis.script
    summary = "；".join(item.summary for item in analysis.diagnostics)
    raise ValueError(summary or "候选未通过 ScriptV1 校验")


def normalize_script_durations(
    payload: object,
    desired_shot_count: DesiredShotCount = None,
) -> DurationNormalizationResult:
    """只对“结构与镜头数合法、仅时长非法”的候选执行确定性规范化。"""

    desired = validate_desired_shot_count(desired_shot_count)
    try:
        relaxed = _DurationRelaxedScript.model_validate(payload)
    except ValidationError as exc:
        raise DurationNormalizationError(
            "候选存在结构或引用错误，禁止时长规范化"
        ) from exc

    count_diagnostic = _shot_count_diagnostic(len(relaxed.shots), desired)
    if count_diagnostic is not None:
        raise DurationNormalizationError(
            f"{count_diagnostic.summary}，禁止通过时长规范化掩盖镜头数错误"
        )

    duration_diagnostics = _duration_diagnostics(relaxed)
    if not duration_diagnostics:
        strict_script = ScriptV1.model_validate(relaxed.model_dump(mode="json"))
        durations = [shot.duration_seconds for shot in strict_script.shots]
        return DurationNormalizationResult(
            normalized=False,
            script=strict_script,
            original_durations=durations,
            normalized_durations=durations.copy(),
            original_total=sum(durations),
            normalized_total=sum(durations),
            reason="原始时长已满足严格 ScriptV1，无需规范化",
        )
    return _normalize_relaxed_script(relaxed)


def analyze_script_usage(script: ScriptV1) -> ScriptValidationWarnings:
    """分析未被镜头使用的实体；不裁剪数据，也不改变校验成功状态。"""

    referenced_scene_ids = {shot.scene_id for shot in script.shots}
    referenced_character_ids = {
        character_id
        for shot in script.shots
        for character_id in shot.character_ids
    }
    return ScriptValidationWarnings(
        unused_scene_ids=[
            scene.id for scene in script.scenes if scene.id not in referenced_scene_ids
        ],
        unused_character_ids=[
            character.id
            for character in script.characters
            if character.id not in referenced_character_ids
        ],
    )


def script_v1_json_schema(
    desired_shot_count: DesiredShotCount = None,
) -> dict:
    """导出结构化输出 JSON Schema；固定模式同步收紧 shots 数量。"""

    desired = validate_desired_shot_count(desired_shot_count)
    schema = copy.deepcopy(ScriptV1.model_json_schema())
    if desired is not None:
        schema["properties"]["shots"]["minItems"] = desired
        schema["properties"]["shots"]["maxItems"] = desired
    return schema

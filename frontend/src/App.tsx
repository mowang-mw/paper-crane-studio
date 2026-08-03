import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent,
} from "react";
import {
  API_BASE,
  ApiError,
  createProject,
  deleteProject,
  exportUrls,
  generateProject,
  getHealth,
  getJob,
  getProject,
  getProviders,
  imageAssetUrl,
  listProjects,
  renderRealImages,
  retryJob,
} from "./api";
import type {
  DesiredShotCount,
  DurationNormalization,
  GeneratedImageShot,
  GenerationAttemptError,
  GenerationErrorDetail,
  GenerationJob,
  HealthStatus,
  JobStatus,
  Project,
  ProjectDetail,
  ProvidersStatus,
  ScriptProviderId,
  ScriptProviderStatus,
} from "./types";

const PAPER_CRANE_STORY =
  "深夜，少女在窗边折出一只纸鹤。纸鹤亮起微光，飞过屋顶、灯火与云层；黎明时，它飞向远方，少女在窗边静静注视。";
const PAPER_CRANE_TITLE = "纸鹤的夜航";
const LLM_START_COMMAND = ".\\scripts\\run_llm_server.ps1";
const REAL_IMAGE_PROVIDER_ID = "comfyui-animagine-xl-4";
const STORY_MIN_CHARS = 10;
const STORY_MAX_CHARS = 3000;
const STORY_RECOMMENDED_MIN_CHARS = 50;
const STORY_RECOMMENDED_MAX_CHARS = 1000;

type SectionName = "create" | "project" | "shots" | "result";
type Notice = { kind: "info" | "success"; message: string; action?: SectionName };
type PresentedShot = {
  id?: string;
  shot_id?: string;
  index?: number;
  shot_index?: number;
  sequence_no?: number;
  title: string;
  visual_description: string;
  narration: string;
  duration_seconds: number;
  camera?: string;
  camera_motion?: string;
  image_prompt?: string;
  provider_id?: string;
  generation_parameters?: Record<string, unknown>;
};

const statusLabels: Record<JobStatus, string> = {
  QUEUED: "等待 Worker",
  RUNNING: "生成中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
};

function readableError(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "发生未知错误，请检查后端日志。";
}

function displayHealth(health: HealthStatus | null): string {
  if (!health) return "连接中";
  if (typeof health.status === "string") return health.status;
  if (typeof health.service === "string") return health.service;
  return health.service?.status ?? "可用";
}

function projectStatus(project: Project): string {
  return project.workflow_status ?? project.status ?? "DRAFT";
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isRealImageJob(job: GenerationJob | null | undefined): boolean {
  if (!job) return false;
  return (
    job.job_type === "GENERATE_REAL_IMAGE_VIDEO" ||
    job.job_type === "RENDER_REAL_IMAGES" ||
    textValue(job.request_json?.image_provider) === REAL_IMAGE_PROVIDER_ID ||
    textValue(job.result_json?.image_provider) === REAL_IMAGE_PROVIDER_ID
  );
}

function jobImageProvider(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  return (
    textValue(job.result_json?.image_provider) ??
    textValue(job.request_json?.image_provider) ??
    (isRealImageJob(job) ? textValue(job.provider_id) : null)
  );
}

function jobImageShots(job: GenerationJob | null | undefined): GeneratedImageShot[] {
  const images = job?.result_json?.image_shots;
  if (!Array.isArray(images)) return [];
  return images.filter(
    (item): item is GeneratedImageShot =>
      recordValue(item) !== null && typeof item.shot_id === "string" && item.shot_id.length > 0,
  );
}

function jobImageCompletedCount(job: GenerationJob | null | undefined): number {
  if (!job) return 0;
  return (
    numberValue(job.result_json?.image_completed_count) ??
    numberValue(job.result_json?.completed_image_count) ??
    numberValue(job.result_json?.completed_images) ??
    jobImageShots(job).filter(
      (image) => image.status === "SUCCEEDED" || Boolean(textValue(image.image_url)),
    ).length
  );
}

function jobImageTotalCount(job: GenerationJob | null | undefined, fallback = 0): number {
  if (!job) return fallback;
  return (
    numberValue(job.result_json?.image_total_count) ??
    numberValue(job.result_json?.total_image_count) ??
    numberValue(job.result_json?.total_images) ??
    jobActualShotCount(job, fallback) ??
    fallback
  );
}

function jobImageGenerationSeconds(job: GenerationJob | null | undefined): number | null {
  if (!job) return null;
  return (
    numberValue(job.result_json?.image_generation_seconds) ??
    numberValue(job.result_json?.image_generation_total_seconds) ??
    numberValue(job.result_json?.generation_seconds_total)
  );
}

function jobBaseSeed(job: GenerationJob | null | undefined): number | null {
  if (!job) return null;
  return numberValue(job.result_json?.base_seed) ?? numberValue(job.request_json?.base_seed);
}

function jobSourceScriptId(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  return (
    textValue(job.result_json?.script_source_job_id) ??
    textValue(job.result_json?.source_script_job_id) ??
    textValue(job.request_json?.source_script_job_id) ??
    textValue(job.request_json?.reuse_script_from_job_id)
  );
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
}

function characterCount(value: string): number {
  return Array.from(value.trim()).length;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function desiredShotCountValue(value: unknown): DesiredShotCount | undefined {
  if (value === null || value === 3 || value === 4 || value === 5) return value;
  return undefined;
}

function generationErrorValue(value: unknown): GenerationErrorDetail | null {
  const record = recordValue(value);
  if (!record) return null;
  const nested = recordValue(record.generation_error);
  return (nested ?? record) as GenerationErrorDetail;
}

function jobGenerationError(job: GenerationJob | null): GenerationErrorDetail | null {
  return generationErrorValue(job?.result_json?.generation_error);
}

function jobDesiredShotCount(job: GenerationJob | null): DesiredShotCount | undefined {
  if (!job) return undefined;
  const candidates = [
    job.request_json?.desired_shot_count,
    job.result_json?.desired_shot_count,
    jobGenerationError(job)?.desired_shot_count,
  ];
  for (const candidate of candidates) {
    const parsed = desiredShotCountValue(candidate);
    if (parsed !== undefined) return parsed;
  }
  return undefined;
}

function jobStoryCharCount(job: GenerationJob | null): number | null {
  if (!job) return null;
  return (
    numberValue(job.request_json?.story_char_count) ??
    numberValue(job.result_json?.story_char_count) ??
    numberValue(jobGenerationError(job)?.story_char_count)
  );
}

function jobActualShotCount(job: GenerationJob | null, fallback?: number): number | null {
  if (!job) return fallback ?? null;
  return (
    numberValue(job.result_json?.actual_shot_count) ??
    numberValue(job.result_json?.final_shot_count) ??
    fallback ??
    null
  );
}

function jobRepairUsed(job: GenerationJob | null): boolean | null {
  if (!job) return null;
  const trace = recordValue(job.result_json?.script_trace);
  return (
    booleanValue(job.result_json?.repair_used) ??
    booleanValue(trace?.repair_used)
  );
}

function jobDurationNormalization(job: GenerationJob | null): DurationNormalization | null {
  if (!job) return null;
  const trace = recordValue(job.result_json?.script_trace);
  return (
    recordValue(job.result_json?.duration_normalization) ??
    recordValue(trace?.duration_normalization)
  ) as DurationNormalization | null;
}

function shotCountLabel(value: DesiredShotCount | undefined): string {
  if (value === null) return "自动（接受 3—5 个）";
  if (value === undefined) return "未记录";
  return `固定 ${value} 个`;
}

function generationSuccessSummary(job: GenerationJob, fallbackActual?: number): string {
  if (isRealImageJob(job)) {
    const completed = jobImageCompletedCount(job);
    const total = jobImageTotalCount(job, fallbackActual ?? 0);
    const elapsed = jobImageGenerationSeconds(job);
    const countText = total > 0 ? `${completed}/${total} 张` : `${completed} 张`;
    const elapsedText = elapsed === null ? "" : `，图像生成共 ${elapsed.toFixed(1)} 秒`;
    return `真实动漫关键帧已完成 ${countText}${elapsedText}，并已合成为可播放短片。`;
  }
  const desired = jobDesiredShotCount(job);
  const actual = jobActualShotCount(job, fallbackActual);
  const plannedDuration = numberValue(job.result_json?.planned_duration_seconds);
  const encodedDuration =
    numberValue(job.result_json?.encoded_duration_seconds) ??
    numberValue(job.result_json?.duration_seconds);
  const durationDelta = numberValue(job.result_json?.duration_delta_seconds);
  const durationValidation = textValue(job.result_json?.duration_validation);
  const duration = encodedDuration;
  const repairUsed = jobRepairUsed(job) === true;
  const normalization = jobDurationNormalization(job);
  const normalizationApplied =
    normalization?.normalized === true || normalization?.applied === true;
  const durationText = duration !== null ? `，成片 ${duration.toFixed(1)} 秒` : "";
  const traceText = [
    repairUsed ? "经过 1 次结构修复" : null,
    normalizationApplied ? "进行了确定性时长归一化" : null,
  ].filter((item): item is string => item !== null);
  const traceSuffix = traceText.length > 0 ? `；本次输出${traceText.join("，并")}。` : "。";
  const mediaDurationSuffix =
    durationValidation === "passed_with_media_tolerance" &&
    plannedDuration !== null &&
    encodedDuration !== null &&
    durationDelta !== null
      ? `计划时长 ${plannedDuration.toFixed(3)} 秒，编码时长 ${encodedDuration.toFixed(3)} 秒；${Math.round(Math.abs(durationDelta) * 1000)} 毫秒差异来自媒体帧量化，验收通过。`
      : "";
  if (desired === null) {
    return actual
      ? `自动模式已生成 ${actual} 个镜头${durationText}${traceSuffix}${mediaDurationSuffix}短片可以播放和下载。`
      : "自动模式短片已经生成完成，可以播放和下载。";
  }
  if (desired !== undefined) {
    if (actual !== null && actual !== desired) {
      return `任务已完成，但固定要求为 ${desired} 个镜头，实际报告 ${actual} 个，请检查结果。`;
    }
    return actual
      ? `已按固定 ${desired} 镜头要求生成 ${actual} 个镜头${durationText}${traceSuffix}${mediaDurationSuffix}短片可以播放和下载。`
      : `已按固定 ${desired} 镜头要求完成生成，可以播放和下载。`;
  }
  return "短片已经生成完成，可在下方播放和下载。";
}

function attemptErrorText(value: GenerationAttemptError | string): string {
  if (typeof value === "string") return value;
  const location = Array.isArray(value.location)
    ? value.location.join(".")
    : Array.isArray(value.loc)
      ? value.loc.join(".")
      : textValue(value.location) ?? textValue(value.path) ?? textValue(value.field);
  const message =
    textValue(value.summary) ??
    textValue(value.message) ??
    textValue(value.msg) ??
    textValue(value.code) ??
    "未提供错误摘要";
  return location ? `${location}：${message}` : message;
}

function errorList(value: unknown): Array<GenerationAttemptError | string> {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is GenerationAttemptError | string =>
      typeof item === "string" || recordValue(item) !== null,
  );
}

function FailureCard({
  detail,
  fallbackMessage,
  retrying,
  onRetry,
}: {
  detail: GenerationErrorDetail | null;
  fallbackMessage?: string | null;
  retrying: boolean;
  onRetry: () => void;
}) {
  const fallbackFirstLine = fallbackMessage
    ?.split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith("Traceback"));
  const safeFallback =
    fallbackFirstLine && !fallbackMessage?.includes("Traceback")
      ? fallbackFirstLine.slice(0, 500)
      : null;
  const summary =
    textValue(detail?.summary) ??
    textValue(detail?.message) ??
    safeFallback ??
    "生成失败，后端没有返回详细信息。";
  const stage = textValue(detail?.stage);
  const code = textValue(detail?.code);
  const storyCharCount = numberValue(detail?.story_char_count);
  const desired = desiredShotCountValue(detail?.desired_shot_count);
  const firstErrors = errorList(detail?.first_attempt_errors);
  const repairErrors = errorList(detail?.repair_attempt_errors);
  const combinedErrors =
    firstErrors.length === 0 && repairErrors.length === 0
      ? errorList(detail?.attempt_errors)
      : [];
  const suggestions = stringList(detail?.suggestions);
  const inputLengthValid =
    storyCharCount !== null &&
    storyCharCount >= STORY_MIN_CHARS &&
    storyCharCount <= STORY_MAX_CHARS;
  const imageFailure =
    stage?.startsWith("IMAGE_") === true ||
    stage?.startsWith("COMFYUI_") === true ||
    code === "GPU_HANDOFF_REQUIRED" ||
    code === "GPU_OOM" ||
    code === "MODEL_NOT_FOUND" ||
    code === "MODEL_HASH_MISMATCH";
  const failedShotId = textValue(detail?.failed_shot_id) ?? textValue(detail?.shot_id);
  const failedShotIndex =
    numberValue(detail?.failed_shot_index) ?? numberValue(detail?.shot_index);
  const completedImages =
    numberValue(detail?.image_completed_count) ??
    numberValue(detail?.completed_image_count) ??
    numberValue(detail?.completed_images);
  const totalImages =
    numberValue(detail?.image_total_count) ??
    numberValue(detail?.total_image_count) ??
    numberValue(detail?.total_images);
  const retryable = booleanValue(detail?.retryable);
  const requiresQwenShutdown =
    booleanValue(detail?.requires_qwen_shutdown) === true || code === "GPU_HANDOFF_REQUIRED";
  const oom = booleanValue(detail?.oom) === true || code === "GPU_OOM";
  const rawLogPaths = detail?.log_paths;
  const logPathRecord = recordValue(rawLogPaths);
  const logPaths = [
    ...stringList(rawLogPaths),
    ...(logPathRecord
      ? Object.values(logPathRecord).filter(
          (item): item is string => typeof item === "string" && item.trim().length > 0,
        )
      : []),
    ...(textValue(detail?.log_path) ? [textValue(detail?.log_path)!] : []),
  ];

  return (
    <div className="failure-box">
      <p className="failure-summary">{summary}</p>
      {inputLengthValid && stage !== "INPUT_VALIDATION" && !imageFailure && (
        <p className="failure-context">
          输入故事共 {storyCharCount} 个字符，长度合法；失败发生在
          {stage ? ` ${stage} 阶段` : "模型输出处理阶段"}，不是故事长度拦截。
        </p>
      )}
      <details className="failure-details">
        <summary>查看诊断详情</summary>
        <dl>
          <div><dt>错误代码</dt><dd>{code ?? "未报告"}</dd></div>
          <div><dt>失败阶段</dt><dd>{stage ?? "未报告"}</dd></div>
          <div><dt>镜头要求</dt><dd>{shotCountLabel(desired)}</dd></div>
          <div><dt>输入字符数</dt><dd>{storyCharCount ?? "未报告"}</dd></div>
          <div><dt>Provider</dt><dd>{textValue(detail?.provider_id) ?? "未报告"}</dd></div>
          <div><dt>模型</dt><dd>{textValue(detail?.model_id) ?? "未报告"}</dd></div>
          {imageFailure && <div><dt>失败镜头</dt><dd>{failedShotIndex !== null ? `第 ${failedShotIndex} 镜` : failedShotId ?? "尚未进入单镜生成"}</dd></div>}
          {imageFailure && <div><dt>已完成图片</dt><dd>{completedImages === null ? "未报告" : `${completedImages}/${totalImages ?? "?"}`}</dd></div>}
          {imageFailure && <div><dt>可直接重试</dt><dd>{retryable === null ? "未报告" : retryable ? "可以" : "不建议"}</dd></div>}
          {imageFailure && <div><dt>需要停止 Qwen</dt><dd>{requiresQwenShutdown ? "是" : "否"}</dd></div>}
          {imageFailure && <div><dt>发生显存不足</dt><dd>{oom ? "是" : "否"}</dd></div>}
        </dl>
        {logPaths.length > 0 && (
          <div className="attempt-errors">
            <strong>日志路径</strong>
            <ul>{logPaths.map((item, index) => <li key={index}><code>{item}</code></li>)}</ul>
          </div>
        )}
        {firstErrors.length > 0 && (
          <div className="attempt-errors">
            <strong>首次输出</strong>
            <ul>{firstErrors.map((item, index) => <li key={index}>{attemptErrorText(item)}</li>)}</ul>
          </div>
        )}
        {repairErrors.length > 0 && (
          <div className="attempt-errors">
            <strong>唯一一次修复输出</strong>
            <ul>{repairErrors.map((item, index) => <li key={index}>{attemptErrorText(item)}</li>)}</ul>
          </div>
        )}
        {combinedErrors.length > 0 && (
          <div className="attempt-errors">
            <strong>校验错误</strong>
            <ul>{combinedErrors.map((item, index) => <li key={index}>{attemptErrorText(item)}</li>)}</ul>
          </div>
        )}
        <div className="failure-suggestions">
          <strong>建议</strong>
          {suggestions.length > 0 ? (
            <ul>{suggestions.map((item, index) => <li key={index}>{item}</li>)}</ul>
          ) : (
            <p>
              {imageFailure
                ? "可在排除显存、模型或 ComfyUI 问题后重试；已完成且校验通过的图片会被复用。"
                : "可手动重试；若重复失败，请查看后端保存的校验报告。"}
            </p>
          )}
        </div>
      </details>
      <button
        className="button button-danger"
        onClick={onRetry}
        disabled={retrying || retryable === false}
      >
        {retrying ? "正在创建重试任务…" : retryable === false ? "当前不可直接重试" : "手动重试"}
      </button>
    </div>
  );
}

function jobValidationWarnings(job: GenerationJob | null) {
  const resultWarnings = recordValue(job?.result_json?.script_validation_warnings);
  const trace = recordValue(job?.result_json?.script_trace);
  const traceWarnings = recordValue(trace?.validation_warnings);
  const warnings = resultWarnings ?? traceWarnings;
  return {
    unusedSceneIds: stringList(warnings?.unused_scene_ids),
    unusedCharacterIds: stringList(warnings?.unused_character_ids),
  };
}

function jobScriptProvider(job: GenerationJob | null): string | null {
  if (!job) return null;
  return (
    textValue(job.request_json?.script_provider) ??
    textValue(job.result_json?.script_provider) ??
    textValue(job.provider_id)
  );
}

function jobModelId(job: GenerationJob | null): string | null {
  if (!job) return null;
  const scriptTrace = recordValue(job.result_json?.script_trace);
  return (
    textValue(job.result_json?.script_model_id) ??
    textValue(scriptTrace?.model)
  );
}

function formatCheckedAt(value: string | null | undefined): string {
  if (!value) return "尚未检查";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function providerName(providerId: ScriptProviderId): string {
  return providerId === "mock" ? "Mock 离线保底" : "本地 Qwen（llama.cpp）";
}

function shotNumber(shot: PresentedShot, fallback: number): number {
  return shot.index ?? shot.shot_index ?? shot.sequence_no ?? fallback;
}

function JobPanel({
  job,
  retrying,
  onRetry,
  onViewResult,
  configuredModelId,
  configuredImageModelId,
  actualShotCount,
}: {
  job: GenerationJob;
  retrying: boolean;
  onRetry: () => void;
  onViewResult: () => void;
  configuredModelId?: string | null;
  configuredImageModelId?: string | null;
  actualShotCount?: number;
}) {
  const imageJob = isRealImageJob(job);
  const progress = Math.max(0, Math.min(100, Math.round(job.progress ?? 0)));
  const tracedModelId = jobModelId(job);
  const modelLabel =
    tracedModelId ??
    (configuredModelId ? `${configuredModelId}（配置值，任务尚未报告）` : "任务尚未报告");
  const desired = jobDesiredShotCount(job);
  const finalShotCount = jobActualShotCount(job, actualShotCount);
  const storyCharCount = jobStoryCharCount(job);
  const repairUsed = jobRepairUsed(job);
  const normalization = jobDurationNormalization(job);
  const normalizationApplied =
    normalization?.normalized === true || normalization?.applied === true;
  const originalDurations = Array.isArray(normalization?.original_durations)
    ? normalization.original_durations.filter((item): item is number => typeof item === "number")
    : [];
  const normalizedDurationValues =
    normalization?.normalized_durations ?? normalization?.final_durations;
  const finalDurations = Array.isArray(normalizedDurationValues)
    ? normalizedDurationValues.filter((item): item is number => typeof item === "number")
    : [];
  const originalTotal =
    numberValue(normalization?.original_total) ??
    numberValue(normalization?.original_total_seconds);
  const normalizedTotal =
    numberValue(normalization?.normalized_total) ??
    numberValue(normalization?.final_total_seconds);
  const warnings = jobValidationWarnings(job);
  const plannedDuration = numberValue(job.result_json?.planned_duration_seconds);
  const encodedDuration = numberValue(job.result_json?.encoded_duration_seconds);
  const durationDelta = numberValue(job.result_json?.duration_delta_seconds);
  const durationTolerance = numberValue(job.result_json?.duration_tolerance_seconds);
  const mediaDurationValidation = textValue(job.result_json?.duration_validation);
  const imageCompleted = jobImageCompletedCount(job);
  const imageTotal = jobImageTotalCount(job, actualShotCount ?? 0);
  const imageElapsed = jobImageGenerationSeconds(job);
  const baseSeed = jobBaseSeed(job);
  const currentShotIndex = numberValue(job.result_json?.current_shot_index);
  const currentShotId = textValue(job.result_json?.current_shot_id);
  const imageModelLabel =
    textValue(job.result_json?.image_model_id) ??
    configuredImageModelId ??
    "任务尚未报告";
  const jobStage = textValue(job.result_json?.stage);
  return (
    <section className={`job-panel job-${job.status.toLowerCase()}`} aria-live="polite">
      <div className="job-heading">
        <div>
          <span className="eyebrow">{imageJob ? "真实图像与成片任务" : "生成任务"}</span>
          <h3>{statusLabels[job.status] ?? job.status}</h3>
        </div>
        <span className="job-percent">{progress}%</span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <p className="job-meta">
        任务 {job.id.slice(0, 8)} ·
        {imageJob
          ? ` 图像 Provider：${jobImageProvider(job) ?? "未报告"} · 模型：${imageModelLabel}`
          : ` 剧本 Provider：${jobScriptProvider(job) ?? "未报告"} · 模型：${modelLabel}`}
      </p>
      <dl className="job-facts">
        {imageJob ? (
          <>
            <div><dt>图像进度</dt><dd>{imageCompleted}/{imageTotal || "?"} 张</dd></div>
            <div><dt>当前阶段</dt><dd>{jobStage ?? (job.status === "QUEUED" ? "等待 Worker" : "图像生成")}</dd></div>
            <div><dt>当前镜头</dt><dd>{currentShotIndex !== null ? `第 ${currentShotIndex} 镜` : currentShotId ?? "未报告"}</dd></div>
            <div><dt>图像总耗时</dt><dd>{imageElapsed === null ? "生成后报告" : `${imageElapsed.toFixed(1)} 秒`}</dd></div>
            <div><dt>Base seed</dt><dd>{baseSeed ?? "未报告"}</dd></div>
            <div><dt>剧本来源 Job</dt><dd title={jobSourceScriptId(job) ?? undefined}>{jobSourceScriptId(job)?.slice(0, 8) ?? "未报告"}</dd></div>
            <div><dt>并发</dt><dd>1（顺序生成）</dd></div>
          </>
        ) : (
          <>
            <div><dt>本次镜头要求</dt><dd>{shotCountLabel(desired)}</dd></div>
            <div><dt>最终实际镜头</dt><dd>{finalShotCount === null ? "生成后报告" : `${finalShotCount} 个`}</dd></div>
            <div><dt>故事字符数</dt><dd>{storyCharCount ?? "未报告"}</dd></div>
            <div>
              <dt>LLM 修复</dt>
              <dd>{repairUsed === null ? "未报告" : repairUsed ? "已使用唯一一次修复" : "未使用"}</dd>
            </div>
            <div>
              <dt>时长归一化</dt>
              <dd>
                {normalization
                  ? normalizationApplied
                    ? "已执行"
                    : "未执行"
                  : "未报告"}
              </dd>
            </div>
          </>
        )}
        <div><dt>计划时长</dt><dd>{plannedDuration === null ? "未报告" : `${plannedDuration.toFixed(3)} 秒`}</dd></div>
        <div><dt>编码时长</dt><dd>{encodedDuration === null ? "未报告" : `${encodedDuration.toFixed(3)} 秒`}</dd></div>
      </dl>
      {imageJob && (job.status === "RUNNING" || imageCompleted > 0) && (
        <p className="job-hint image-progress-copy">
          已完成 {imageCompleted}/{imageTotal || "?"} 张真实关键帧；同一任务内 ComfyUI 仅启动一次并顺序生成。
        </p>
      )}
      {mediaDurationValidation === "passed_with_media_tolerance" &&
        plannedDuration !== null && encodedDuration !== null &&
        durationDelta !== null && durationTolerance !== null && (
          <p className="job-hint">
            计划时长 {plannedDuration.toFixed(3)} 秒，编码时长 {encodedDuration.toFixed(3)} 秒；
            {Math.round(Math.abs(durationDelta) * 1000)} 毫秒差异来自媒体帧量化，
            在 ±{durationTolerance.toFixed(3)} 秒容差内，验收通过。
          </p>
        )}
      {!imageJob && normalizationApplied && (
        <div className="normalization-note" role="status">
          <strong>已进行确定性时长归一化。</strong>
          {textValue(normalization?.reason) && <span>原因：{textValue(normalization?.reason)}</span>}
          {(originalDurations.length > 0 || finalDurations.length > 0) && (
            <span>
              {originalDurations.length > 0 ? `原始 ${originalDurations.join(" / ")} 秒` : "原始时长未报告"}
              {" → "}
              {finalDurations.length > 0 ? `最终 ${finalDurations.join(" / ")} 秒` : "最终时长未报告"}
            </span>
          )}
          {(originalTotal !== null || normalizedTotal !== null) && (
            <span>
              总时长：{originalTotal ?? "未报告"} 秒 → {normalizedTotal ?? "未报告"} 秒
            </span>
          )}
        </div>
      )}
      {!imageJob && (warnings.unusedSceneIds.length > 0 || warnings.unusedCharacterIds.length > 0) && (
        <p className="job-warning">
          非阻断警告：
          {warnings.unusedSceneIds.length > 0 && `${warnings.unusedSceneIds.length} 个未使用场景`}
          {warnings.unusedSceneIds.length > 0 && warnings.unusedCharacterIds.length > 0 && "，"}
          {warnings.unusedCharacterIds.length > 0 && `${warnings.unusedCharacterIds.length} 个未使用角色`}
          。
        </p>
      )}
      {job.status === "QUEUED" && (
        <p className="job-hint">
          {imageJob
            ? "真实图像任务已入队。开始前请确保本地 Qwen 已停止并释放显存。"
            : "任务已入队。请确认独立 Worker 正在运行。"}
        </p>
      )}
      {job.status === "FAILED" && (
        <FailureCard
          detail={jobGenerationError(job)}
          fallbackMessage={job.error_message}
          retrying={retrying}
          onRetry={onRetry}
        />
      )}
      {job.status === "SUCCEEDED" && (
        <div className="success-box">
          <p>{generationSuccessSummary(job, actualShotCount)}</p>
          <button className="button button-success" type="button" onClick={onViewResult}>
            查看成片
          </button>
        </div>
      )}
    </section>
  );
}

function ProviderSelector({
  value,
  status,
  error,
  checking,
  disabled,
  onChange,
  onRefresh,
}: {
  value: ScriptProviderId;
  status: ProvidersStatus | null;
  error: string;
  checking: boolean;
  disabled: boolean;
  onChange: (providerId: ScriptProviderId) => void;
  onRefresh: () => void;
}) {
  const descriptors = new Map(
    (status?.providers ?? []).map((provider) => [provider.provider_id, provider]),
  );
  const llamaAvailable = descriptors.get("llamacpp")?.available === true && !error;
  const initialCheckInProgress = checking && status === null;
  const providerIds: ScriptProviderId[] = ["mock", "llamacpp"];

  return (
    <section className="provider-control" aria-labelledby="provider-title">
      <div className="provider-control-heading">
        <div>
          <p className="eyebrow">剧本模型</p>
          <h4 id="provider-title">选择 Script Provider</h4>
        </div>
        <button
          className="button button-ghost button-small"
          type="button"
          onClick={onRefresh}
          disabled={checking || disabled}
        >
          {checking ? "检查中…" : "重新检查"}
        </button>
      </div>

      <label className="provider-select-label">
        本次生成使用
        <select
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value as ScriptProviderId)}
          aria-describedby="provider-help"
        >
          <option value="mock">Mock（离线保底）</option>
          <option value="llamacpp" disabled={!llamaAvailable || checking}>
            本地 Qwen（llama.cpp）
            {llamaAvailable ? "" : initialCheckInProgress ? " — 检查中" : " — 离线"}
          </option>
        </select>
      </label>

      <div className="provider-status-grid" aria-live="polite">
        {providerIds.map((providerId) => {
          const descriptor: ScriptProviderStatus | undefined = descriptors.get(providerId);
          const available = providerId === "mock" ? true : descriptor?.available === true && !error;
          const stateLabel =
            providerId === "mock"
              ? "离线可用"
              : initialCheckInProgress
                ? "检查中"
                : available
                  ? "在线"
                  : descriptor?.configured === false
                    ? "离线（未配置）"
                    : "离线";
          return (
            <article
              className={`provider-status-card ${
                initialCheckInProgress && providerId === "llamacpp"
                  ? "is-checking"
                  : available
                    ? "is-available"
                    : "is-unavailable"
              }`}
              key={providerId}
            >
              <div>
                <strong>{descriptor?.display_name ?? providerName(providerId)}</strong>
                {status?.default_script_provider === providerId && (
                  <span className="provider-default">默认</span>
                )}
              </div>
              <span className="provider-state">{stateLabel}</span>
              <small>Provider ID：{providerId}</small>
              <small>模型 ID：{descriptor?.model_id ?? (providerId === "mock" ? "N/A（Mock）" : "未报告")}</small>
              <small>
                配置：
                {descriptor?.configured === true
                  ? "已配置"
                  : descriptor?.configured === false
                    ? "未配置"
                    : "未报告"}
              </small>
              <small>来源：{descriptor?.source_type ?? (providerId === "mock" ? "MOCK" : "未报告")}</small>
              {descriptor?.detail && <small className="provider-detail">{descriptor.detail}</small>}
            </article>
          );
        })}
      </div>

      <p className="provider-check-meta" id="provider-help">
        API 默认：{status?.default_script_provider ?? "未报告"} · 最近检查：
        {formatCheckedAt(status?.checked_at)}
      </p>
      {error && <p className="provider-warning">{error}；当前仅允许 Mock 离线保底。</p>}
      {!checking && !llamaAvailable && (
        <p className="provider-command">
          本地 Qwen 当前离线，不会提交 llamacpp 任务。启动后重新检查：
          <code>{LLM_START_COMMAND}</code>
        </p>
      )}
    </section>
  );
}

function StageNavigation({
  current,
  completed,
  available,
  onNavigate,
}: {
  current: SectionName;
  completed: Record<SectionName, boolean>;
  available: Record<SectionName, boolean>;
  onNavigate: (section: SectionName) => void;
}) {
  const stages: Array<{ key: SectionName; number: string; label: string }> = [
    { key: "create", number: "01", label: "创建项目" },
    { key: "project", number: "02", label: "生成任务" },
    { key: "shots", number: "03", label: "查看镜头" },
    { key: "result", number: "04", label: "播放与下载成片" },
  ];
  return (
    <nav className="stage-navigation" aria-label="制作流程">
      {stages.map((stage) => {
        const state = current === stage.key ? "current" : completed[stage.key] ? "done" : "pending";
        return (
          <button
            key={stage.key}
            className={`stage-step is-${state}`}
            type="button"
            disabled={!available[stage.key]}
            aria-current={current === stage.key ? "step" : undefined}
            onClick={() => onNavigate(stage.key)}
          >
            <span className="stage-marker">{completed[stage.key] ? "✓" : stage.number}</span>
            <span>{stage.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState("");
  const [providersStatus, setProvidersStatus] = useState<ProvidersStatus | null>(null);
  const [providerError, setProviderError] = useState("");
  const [providerChecking, setProviderChecking] = useState(false);
  const [scriptProvider, setScriptProvider] = useState<ScriptProviderId>("mock");
  const [desiredShotCount, setDesiredShotCount] = useState<DesiredShotCount>(4);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [activeJob, setActiveJob] = useState<GenerationJob | null>(null);
  const [title, setTitle] = useState("");
  const [story, setStory] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [generationRequestError, setGenerationRequestError] =
    useState<GenerationErrorDetail | null>(null);
  const [imageRequestError, setImageRequestError] =
    useState<GenerationErrorDetail | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<Project | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const createSectionRef = useRef<HTMLElement>(null);
  const projectSectionRef = useRef<HTMLElement>(null);
  const shotsSectionRef = useRef<HTMLElement>(null);
  const resultSectionRef = useRef<HTMLElement>(null);
  const projectTitleRef = useRef<HTMLHeadingElement>(null);
  const resultTitleRef = useRef<HTMLHeadingElement>(null);
  const creationInFlightRef = useRef(false);
  const deletionInFlightRef = useRef(false);
  const providerSelectionTouchedRef = useRef(false);
  const pendingNavigationRef = useRef<"project" | "result" | null>(null);
  const handledSucceededJobsRef = useRef(new Set<string>());

  const scrollToSection = useCallback((section: SectionName) => {
    const sections: Record<SectionName, HTMLElement | null> = {
      create: createSectionRef.current,
      project: projectSectionRef.current,
      shots: shotsSectionRef.current,
      result: resultSectionRef.current,
    };
    const target = sections[section];
    if (!target) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    const focusTarget =
      section === "project"
        ? projectTitleRef.current ?? target
        : section === "result"
          ? resultTitleRef.current ?? target
          : target;
    focusTarget.focus({ preventScroll: true });
  }, []);

  const refreshProjects = useCallback(async () => {
    const items = await listProjects();
    setProjects(items);
    return items;
  }, []);

  const refreshProviderStatus = useCallback(async (): Promise<ProvidersStatus | null> => {
    setProviderChecking(true);
    setProviderError("");
    try {
      const value = await getProviders();
      setProvidersStatus(value);
      if (!providerSelectionTouchedRef.current && value.default_script_provider) {
        const defaultDescriptor = value.providers.find(
          (provider) => provider.provider_id === value.default_script_provider,
        );
        if (value.default_script_provider === "mock" || defaultDescriptor?.available === true) {
          setScriptProvider(value.default_script_provider);
        }
      }
      return value;
    } catch (cause) {
      setProvidersStatus(null);
      setProviderError(`Provider 状态检查失败：${readableError(cause)}`);
      return null;
    } finally {
      setProviderChecking(false);
    }
  }, []);

  const refreshDetail = useCallback(async (projectId: string) => {
    const value = await getProject(projectId);
    setDetail(value);
    const pending = value.recent_jobs.find(
      (job) => job.status === "QUEUED" || job.status === "RUNNING",
    );
    if (pending) setActiveJob(pending);
    return value;
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getHealth(), refreshProjects()])
      .then(([healthValue, items]) => {
        if (cancelled) return;
        setHealth(healthValue);
        if (items.length > 0) setSelectedId((current) => current ?? items[0].id);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setHealthError(readableError(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshProjects]);

  useEffect(() => {
    void refreshProviderStatus();
  }, [refreshProviderStatus]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setActiveJob(null);
    setError("");
    setGenerationRequestError(null);
    setImageRequestError(null);
    refreshDetail(selectedId).catch((cause: unknown) => setError(readableError(cause)));
  }, [refreshDetail, selectedId]);

  useEffect(() => {
    if (!activeJob || (activeJob.status !== "QUEUED" && activeJob.status !== "RUNNING")) return;

    let cancelled = false;
    const timer = window.setInterval(() => {
      getJob(activeJob.id)
        .then(async (job) => {
          if (cancelled) return;
          if (
            job.status === "SUCCEEDED" &&
            activeJob.status !== "SUCCEEDED" &&
            !handledSucceededJobsRef.current.has(job.id)
          ) {
            handledSucceededJobsRef.current.add(job.id);
            pendingNavigationRef.current = "result";
            setNotice({
              kind: "success",
              message: generationSuccessSummary(job),
              action: "result",
            });
          }
          setActiveJob(job);
          if (job.status === "SUCCEEDED" || job.status === "FAILED") {
            window.clearInterval(timer);
            await Promise.all([refreshDetail(job.project_id), refreshProjects()]);
          }
        })
        .catch((cause: unknown) => {
          if (!cancelled) setError(`任务状态更新失败：${readableError(cause)}`);
        });
    }, 1200);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeJob, refreshDetail, refreshProjects]);

  useEffect(() => {
    if (
      pendingNavigationRef.current === "project" &&
      selectedId &&
      detail?.project.id === selectedId
    ) {
      pendingNavigationRef.current = null;
      scrollToSection("project");
    }
  }, [detail?.project.id, scrollToSection, selectedId]);

  useEffect(() => {
    if (pendingNavigationRef.current !== "result" || !detail?.latest_export) return;
    pendingNavigationRef.current = null;
    const focused = document.activeElement;
    const userIsEditing =
      focused instanceof HTMLInputElement ||
      focused instanceof HTMLTextAreaElement ||
      focused instanceof HTMLSelectElement ||
      (focused instanceof HTMLElement && focused.isContentEditable);
    if (!userIsEditing) scrollToSection("result");
  }, [detail?.latest_export, scrollToSection]);

  const submitProject = async (event: FormEvent) => {
    event.preventDefault();
    if (creationInFlightRef.current || busy !== null) return;
    if (!title.trim()) {
      setError("请输入项目标题。");
      return;
    }
    const currentStoryCharCount = characterCount(story);
    if (currentStoryCharCount < STORY_MIN_CHARS) {
      setError(`故事正文至少需要 ${STORY_MIN_CHARS} 个字符，当前为 ${currentStoryCharCount} 个。`);
      return;
    }
    if (currentStoryCharCount > STORY_MAX_CHARS) {
      setError(`故事正文最多允许 ${STORY_MAX_CHARS} 个字符，当前为 ${currentStoryCharCount} 个。`);
      return;
    }
    creationInFlightRef.current = true;
    setBusy("create");
    setError("");
    setNotice(null);
    try {
      const project = await createProject({ title: title.trim(), story: story.trim() });
      await refreshProjects();
      pendingNavigationRef.current = "project";
      setSelectedId(project.id);
      setNotice({
        kind: "success",
        message: `项目“${project.title}”已创建并选中。表单内容已保留，可继续修改。`,
      });
    } catch (cause) {
      setError(`创建项目失败：${readableError(cause)}`);
    } finally {
      creationInFlightRef.current = false;
      setBusy(null);
    }
  };

  const makeDemo = () => {
    if (creationInFlightRef.current || busy !== null) return;
    setError("");
    setTitle(PAPER_CRANE_TITLE);
    setStory(PAPER_CRANE_STORY);
    setDesiredShotCount(4);
    setNotice({
      kind: "info",
      message: "演示故事已填入，请确认内容后点击创建项目。",
    });
  };

  const openDeleteConfirmation = (event: MouseEvent, project: Project) => {
    event.stopPropagation();
    if (deletingId) return;
    setDeleteCandidate(project);
    setError("");
  };

  const confirmDelete = async (event: MouseEvent) => {
    event.stopPropagation();
    if (!deleteCandidate || deletionInFlightRef.current) return;
    deletionInFlightRef.current = true;
    const project = deleteCandidate;
    setDeletingId(project.id);
    setError("");
    setNotice(null);
    try {
      await deleteProject(project.id);
      const items = await refreshProjects();
      setDeleteCandidate(null);
      if (selectedId === project.id) {
        setDetail(null);
        setActiveJob(null);
        setSelectedId(items[0]?.id ?? null);
      }
      setNotice({ kind: "success", message: `项目“${project.title}”已删除。` });
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setError("当前项目仍有任务正在等待或生成，请等待任务结束后再删除。");
      } else {
        setError(`删除项目失败：${readableError(cause)}`);
      }
    } finally {
      deletionInFlightRef.current = false;
      setDeletingId(null);
    }
  };

  const startGeneration = async () => {
    if (!selectedId) return;
    const projectForGeneration =
      detail?.project ?? projects.find((project) => project.id === selectedId);
    const selectedStoryCharCount = characterCount(projectForGeneration?.story ?? "");
    if (
      selectedStoryCharCount < STORY_MIN_CHARS ||
      selectedStoryCharCount > STORY_MAX_CHARS
    ) {
      setGenerationRequestError({
        code: "STORY_LENGTH_OUT_OF_RANGE",
        stage: "INPUT_VALIDATION",
        summary: `故事正文必须为 ${STORY_MIN_CHARS}—${STORY_MAX_CHARS} 个字符，当前为 ${selectedStoryCharCount} 个。`,
        story_char_count: selectedStoryCharCount,
        desired_shot_count: desiredShotCount,
        suggestions: ["请返回创建区，使用符合长度要求的故事新建项目。"],
      });
      setError("");
      return;
    }
    setBusy("generate");
    setError("");
    setGenerationRequestError(null);
    setImageRequestError(null);
    setNotice(null);
    try {
      if (scriptProvider === "llamacpp") {
        const latestStatus = await refreshProviderStatus();
        const localQwenReady = latestStatus?.providers.some(
          (provider) => provider.provider_id === "llamacpp" && provider.available,
        );
        if (!localQwenReady) {
          setError(`本地 Qwen 当前离线，未提交生成任务。请先运行 ${LLM_START_COMMAND}，再重新检查。`);
          return;
        }
      }
      const job = await generateProject(selectedId, scriptProvider, desiredShotCount);
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const structured = generationErrorValue(cause.detail);
        if (structured) {
          setGenerationRequestError(structured);
          setError("");
        } else {
          setError(cause.message);
        }
      } else {
        setError(readableError(cause));
      }
    } finally {
      setBusy(null);
    }
  };

  const retryGeneration = async (failedJob: GenerationJob) => {
    setBusy("retry");
    setError("");
    setGenerationRequestError(null);
    setNotice(null);
    try {
      const job = await retryJob(failedJob.id);
      setActiveJob({ ...job, project_id: job.project_id || failedJob.project_id });
      await refreshDetail(failedJob.project_id);
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const startRealImageGeneration = async () => {
    if (!selectedId || !scriptJob) return;
    setBusy("real-image");
    setError("");
    setGenerationRequestError(null);
    setImageRequestError(null);
    setNotice(null);
    try {
      const job = await renderRealImages(selectedId, scriptJob.id);
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const structured = generationErrorValue(cause.detail);
        if (structured) {
          setImageRequestError(structured);
          setError("");
        } else {
          setError(cause.message);
        }
      } else {
        setError(readableError(cause));
      }
    } finally {
      setBusy(null);
    }
  };

  const selectedProject = detail?.project ?? projects.find((item) => item.id === selectedId) ?? null;
  const formStoryCharCount = characterCount(story);
  const formStoryLengthValid =
    formStoryCharCount >= STORY_MIN_CHARS && formStoryCharCount <= STORY_MAX_CHARS;
  const formStoryLengthRecommended =
    formStoryCharCount >= STORY_RECOMMENDED_MIN_CHARS &&
    formStoryCharCount <= STORY_RECOMMENDED_MAX_CHARS;
  const selectedStoryCharCount = selectedProject ? characterCount(selectedProject.story) : 0;
  const latestVisibleJob = activeJob ?? detail?.recent_jobs[0] ?? null;
  const media = useMemo(() => {
    if (!selectedId || !detail?.latest_export) return null;
    return exportUrls(selectedId, detail.latest_export);
  }, [detail?.latest_export, selectedId]);
  const generationInProgress =
    activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING";
  const providerDescriptors = providersStatus?.providers ?? [];
  const imageProviderDescriptors = providersStatus?.image_providers ?? [];
  const selectedProviderDescriptor = providerDescriptors.find(
    (provider) => provider.provider_id === scriptProvider,
  );
  const llamaAvailable = providerDescriptors.some(
    (provider) => provider.provider_id === "llamacpp" && provider.available,
  ) && !providerError;
  const realImageProviderDescriptor = imageProviderDescriptors.find(
    (provider) => provider.provider_id === REAL_IMAGE_PROVIDER_ID,
  );
  const realImageProviderConfigured = realImageProviderDescriptor?.configured !== false;
  const gpuHandoffRequired =
    llamaAvailable || realImageProviderDescriptor?.requires_gpu_handoff === true;
  const structuredScript = detail?.project.script_json ?? null;
  const scriptCharacters = Array.isArray(structuredScript?.characters)
    ? structuredScript.characters
    : [];
  const scriptScenes = Array.isArray(structuredScript?.scenes) ? structuredScript.scenes : [];
  const scriptShots: PresentedShot[] =
    Array.isArray(structuredScript?.shots) && structuredScript.shots.length > 0
      ? structuredScript.shots
      : (detail?.shots ?? []).map((shot) => {
          const parameters = recordValue(shot.parameters_json);
          return {
            id: shot.id,
            index: shot.shot_index,
            title: shot.title,
            visual_description: shot.visual_description,
            narration: shot.narration,
            duration_seconds: shot.duration_seconds,
            camera:
              textValue(parameters?.camera) ??
              textValue(parameters?.camera_motion) ??
              textValue(parameters?.motion) ??
              undefined,
            image_prompt: textValue(parameters?.image_prompt) ?? undefined,
            provider_id: shot.provider_id,
            generation_parameters: parameters ?? undefined,
          };
        });
  const exportJob = detail?.latest_export
    ? detail.recent_jobs.find((job) => job.id === detail.latest_export?.job_id) ?? null
    : null;
  const referencedScriptJobId =
    jobSourceScriptId(latestVisibleJob) ?? jobSourceScriptId(exportJob);
  const referencedScriptJob = referencedScriptJobId
    ? detail?.recent_jobs.find((job) => job.id === referencedScriptJobId) ?? null
    : null;
  const scriptJob =
    (referencedScriptJob?.status === "SUCCEEDED" ? referencedScriptJob : null) ??
    detail?.recent_jobs.find(
      (job) =>
        job.status === "SUCCEEDED" &&
        !isRealImageJob(job) &&
        recordValue(job.result_json?.script_trace) !== null,
    ) ??
    null;
  const scriptProviderUsed = jobScriptProvider(exportJob) ?? "未报告";
  const scriptModelUsed = jobModelId(exportJob) ?? jobModelId(scriptJob) ?? "未报告";
  const scriptSourceUsed = textValue(exportJob?.result_json?.script_source_type) ?? "未报告";
  const imageProviderUsed = textValue(exportJob?.result_json?.image_provider) ?? "未报告";
  const audioProviderUsed = textValue(exportJob?.result_json?.audio_provider) ?? "未报告";
  const videoSourceUsed =
    textValue(exportJob?.result_json?.video_source_type) ??
    textValue(exportJob?.result_json?.source_type) ??
    "未报告";
  const exportDesiredShotCount = jobDesiredShotCount(exportJob);
  const exportActualShotCount = jobActualShotCount(exportJob, scriptShots.length);
  const exportRepairUsed = jobRepairUsed(exportJob);
  const exportDurationNormalization = jobDurationNormalization(exportJob);
  const exportIsRealImage =
    exportJob?.status === "SUCCEEDED" && imageProviderUsed === REAL_IMAGE_PROVIDER_ID;
  const imageDisplayJob =
    (isRealImageJob(latestVisibleJob) ? latestVisibleJob : null) ??
    (isRealImageJob(exportJob) ? exportJob : null) ??
    detail?.recent_jobs.find((job) => isRealImageJob(job)) ??
    null;
  const generatedImageShots = jobImageShots(imageDisplayJob);
  const imageGenerationInProgress =
    isRealImageJob(activeJob) &&
    (activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING");
  const scriptWarnings = jobValidationWarnings(exportJob ?? scriptJob ?? latestVisibleJob);
  const currentJobProviderId = jobScriptProvider(latestVisibleJob);
  const currentJobProviderDescriptor = providerDescriptors.find(
    (provider) => provider.provider_id === currentJobProviderId,
  );
  const currentStage: SectionName =
    latestVisibleJob?.status === "QUEUED" ||
    latestVisibleJob?.status === "RUNNING" ||
    latestVisibleJob?.status === "FAILED"
      ? "project"
      : media
        ? "result"
        : scriptShots.length
          ? "shots"
          : selectedProject
            ? "project"
            : "create";
  const completedStages: Record<SectionName, boolean> = {
    create: Boolean(selectedProject),
    project: latestVisibleJob?.status === "SUCCEEDED" || Boolean(media),
    shots: Boolean(scriptShots.length),
    result: Boolean(media),
  };
  const availableStages: Record<SectionName, boolean> = {
    create: true,
    project: true,
    shots: Boolean(scriptShots.length),
    result: Boolean(media),
  };

  return (
    <div className="app-shell">
      <header className="hero">
        <nav className="topbar" aria-label="主导航">
          <a className="brand" href="#top" aria-label="纸鹤工坊首页">
            <span className="brand-mark">折</span>
            <span>
              <strong>纸鹤工坊</strong>
              <small>Paper Crane Studio</small>
            </span>
          </a>
          <div className={`health-pill ${healthError ? "is-offline" : ""}`}>
            <span className="health-dot" />
            {healthError ? "后端未连接" : `${textValue(health?.stage) ?? "M4-B"} · ${displayHealth(health)}`}
          </div>
        </nav>

        <div className="hero-copy" id="top">
          <p className="kicker">SCRIPT PROVIDER × IMAGE PROVIDER × FFMPEG</p>
          <h1>把一个故事，折成一段<br />真正可播放的短片。</h1>
          <p>
            剧本可选择 Mock 离线保底或本地 Qwen；图像可显式选择 Mock，或在释放
            Qwen 显存后使用本地 Animagine。音频仍为 Mock，FFmpeg 负责运镜、字幕与成片。
          </p>
          <a className="text-link" href="#workspace">开始创建 <span>↓</span></a>
        </div>
        <div className="hero-orbit" aria-hidden="true">
          <span className="crane">⌁</span>
          <i className="star star-one" />
          <i className="star star-two" />
          <i className="star star-three" />
        </div>
      </header>

      <main id="workspace">
        {(error || healthError) && (
          <div className="global-alert" role="alert">
            <strong>{healthError ? "连接提示" : "操作失败"}</strong>
            <span>{error || healthError}</span>
            <small>API：{API_BASE}</small>
          </div>
        )}

        {notice && (
          <div className={`inline-notice notice-${notice.kind}`} aria-live="polite">
            <span>{notice.message}</span>
            {notice.action && availableStages[notice.action] && (
              <button className="notice-action" type="button" onClick={() => scrollToSection(notice.action!)}>
                查看成片
              </button>
            )}
          </div>
        )}

        <section
          className="section create-section"
          id="create-section"
          ref={createSectionRef}
          tabIndex={-1}
        >
          <div className="section-heading">
            <span className="section-number">01</span>
            <div>
              <p className="eyebrow">创建项目</p>
              <h2>从短篇故事开始</h2>
            </div>
          </div>
          <div className="create-grid">
            <form className="story-form" onSubmit={submitProject}>
              <label>
                项目标题
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  maxLength={120}
                  required
                  placeholder="例如：纸鹤的夜航"
                />
              </label>
              <label>
                故事正文（这里只写故事）
                <textarea
                  value={story}
                  onChange={(event) => {
                    setStory(event.target.value);
                    setError("");
                  }}
                  minLength={STORY_MIN_CHARS}
                  maxLength={STORY_MAX_CHARS}
                  rows={6}
                  required
                  aria-invalid={story.length > 0 && !formStoryLengthValid}
                  aria-describedby="story-length-help"
                  placeholder="请只描述人物、事件与结局；镜头数量在生成区域单独选择。"
                />
                <span
                  className={`story-length ${
                    formStoryLengthValid
                      ? formStoryLengthRecommended
                        ? "is-recommended"
                        : "is-valid"
                      : story.length > 0
                        ? "is-invalid"
                        : ""
                  }`}
                  id="story-length-help"
                >
                  {formStoryCharCount} / {STORY_MAX_CHARS} 字符 · 推荐{" "}
                  {STORY_RECOMMENDED_MIN_CHARS}—{STORY_RECOMMENDED_MAX_CHARS}，硬限制{" "}
                  {STORY_MIN_CHARS}—{STORY_MAX_CHARS}
                  {formStoryLengthValid
                    ? formStoryLengthRecommended
                      ? " · 推荐长度"
                      : " · 长度合法"
                    : story.length > 0
                      ? " · 长度不合法"
                      : ""}
                </span>
              </label>
              <div className="form-actions">
                <button
                  className="button button-primary"
                  type="submit"
                  disabled={busy !== null || !title.trim() || !formStoryLengthValid}
                >
                  {busy === "create" ? "正在创建……" : "创建项目"}
                </button>
                <button
                  className="button button-ghost"
                  type="button"
                  onClick={makeDemo}
                  disabled={busy !== null}
                >
                  载入《纸鹤的夜航》Demo
                </button>
              </div>
            </form>
            <aside className="workflow-card">
              <p className="eyebrow">本次生成路径</p>
              <ol>
                <li><span>1</span>所选剧本 Provider 生成 3—5 个镜头</li>
                <li><span>2</span>保存并复用严格 ScriptV1</li>
                <li><span>3</span>选择 Mock 或真实动漫关键帧</li>
                <li><span>4</span>FFmpeg 合成运动、字幕与音频</li>
                <li><span>5</span>浏览器播放并下载 MP4</li>
              </ol>
              <p className="offline-note">Mock 保底无需网络、API Key 或模型权重</p>
            </aside>
          </div>
        </section>

        <StageNavigation
          current={currentStage}
          completed={completedStages}
          available={availableStages}
          onNavigate={scrollToSection}
        />

        <section
          className="section projects-section"
          id="project-section"
          ref={projectSectionRef}
          tabIndex={-1}
        >
          <div className="section-heading">
            <span className="section-number">02</span>
            <div>
              <p className="eyebrow">项目与任务</p>
              <h2>选择一个项目生成</h2>
            </div>
          </div>
          <div className="projects-layout">
            <aside className="project-list" aria-label="项目列表">
              {projects.length === 0 ? (
                <div className="empty-state">还没有项目，请先创建一个故事。</div>
              ) : (
                projects.map((project) => (
                  <div className="project-list-entry" key={project.id}>
                    <div className={`project-item ${selectedId === project.id ? "is-active" : ""}`}>
                      <button
                        type="button"
                        className="project-select"
                        disabled={deletingId !== null}
                        onClick={() => {
                          setDeleteCandidate(null);
                          setSelectedId(project.id);
                          setActiveJob(null);
                          setImageRequestError(null);
                        }}
                      >
                        <span className="project-index">{project.title.slice(0, 1)}</span>
                        <span className="project-summary">
                          <strong>{project.title}</strong>
                          <small>{projectStatus(project)}</small>
                        </span>
                        <span aria-hidden="true">›</span>
                      </button>
                      <button
                        className="project-delete"
                        type="button"
                        aria-label={`删除项目“${project.title}”`}
                        disabled={deletingId !== null}
                        onClick={(event) => openDeleteConfirmation(event, project)}
                      >
                        删除
                      </button>
                    </div>
                    {deleteCandidate?.id === project.id && (
                      <div
                        className="delete-confirmation"
                        role="dialog"
                        aria-modal="false"
                        aria-labelledby={`delete-title-${project.id}`}
                      >
                        <strong id={`delete-title-${project.id}`}>确认删除“{project.title}”？</strong>
                        <p>删除后项目、任务和生成文件将无法恢复。</p>
                        <div className="delete-actions">
                          <button
                            className="button button-ghost button-small"
                            type="button"
                            disabled={deletingId !== null}
                            onClick={(event) => {
                              event.stopPropagation();
                              setDeleteCandidate(null);
                            }}
                          >
                            取消
                          </button>
                          <button
                            className="button button-danger button-small"
                            type="button"
                            disabled={deletingId !== null}
                            onClick={confirmDelete}
                          >
                            {deletingId === project.id ? "正在删除……" : "确认删除"}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                ))
              )}
            </aside>

            <article className="project-detail">
              {!selectedProject ? (
                <div className="empty-state large">选择项目后，这里会显示镜头与生成状态。</div>
              ) : (
                <>
                  <div className="project-title-row">
                    <div>
                      <p className="eyebrow">{projectStatus(selectedProject)}</p>
                      <h3 ref={projectTitleRef} tabIndex={-1}>{selectedProject.title}</h3>
                    </div>
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={startGeneration}
                      disabled={
                        busy !== null ||
                        generationInProgress ||
                        (scriptProvider === "llamacpp" && (!llamaAvailable || providerChecking))
                      }
                    >
                      {busy === "generate"
                        ? "正在提交…"
                        : generationInProgress
                          ? "任务进行中"
                          : scriptProvider === "llamacpp" && !llamaAvailable
                            ? "本地 Qwen 未启动"
                            : detail?.latest_export
                              ? `再次生成（${selectedProviderDescriptor?.display_name ?? providerName(scriptProvider)}）`
                              : scriptProvider === "mock"
                                ? "生成 Mock 短片"
                                : "用本地 Qwen 生成"}
                    </button>
                  </div>
                  <p className="story-preview">{selectedProject.story}</p>
                  <ProviderSelector
                    value={scriptProvider}
                    status={providersStatus}
                    error={providerError}
                    checking={providerChecking}
                    disabled={busy !== null || generationInProgress}
                    onChange={(providerId) => {
                      providerSelectionTouchedRef.current = true;
                      setScriptProvider(providerId);
                      setError("");
                      setGenerationRequestError(null);
                      setImageRequestError(null);
                      setNotice(null);
                    }}
                    onRefresh={() => void refreshProviderStatus()}
                  />
                  <section className="generation-options" aria-labelledby="shot-count-title">
                    <label className="shot-count-control">
                      <span id="shot-count-title">本次生成的镜头数量</span>
                      <select
                        value={desiredShotCount === null ? "auto" : String(desiredShotCount)}
                        disabled={busy !== null || generationInProgress}
                        onChange={(event) => {
                          const value = event.target.value;
                          setDesiredShotCount(value === "auto" ? null : Number(value) as 3 | 4 | 5);
                          setGenerationRequestError(null);
                          setImageRequestError(null);
                          setError("");
                        }}
                      >
                        <option value="auto">自动（接受 3—5 个）</option>
                        <option value="3">固定 3 个</option>
                        <option value="4">固定 4 个（默认）</option>
                        <option value="5">固定 5 个</option>
                      </select>
                    </label>
                    <div className="generation-option-help">
                      <p>
                        {desiredShotCount === null
                          ? "自动模式接受模型生成 3—5 个镜头，并在完成后显示实际数量。"
                          : `固定模式要求模型最终严格生成 ${desiredShotCount} 个镜头。`}
                      </p>
                      <p>
                        当前项目故事：{selectedStoryCharCount} 个字符 ·
                        {selectedStoryCharCount >= STORY_MIN_CHARS &&
                        selectedStoryCharCount <= STORY_MAX_CHARS
                          ? " 长度合法"
                          : ` 不符合 ${STORY_MIN_CHARS}—${STORY_MAX_CHARS} 字符硬限制`}
                      </p>
                    </div>
                  </section>
                  {generationRequestError && (
                    <section className="job-panel job-failed request-failure" aria-live="polite">
                      <div className="job-heading">
                        <div>
                          <span className="eyebrow">生成请求</span>
                          <h3>未创建任务</h3>
                        </div>
                      </div>
                      <FailureCard
                        detail={generationRequestError}
                        retrying={busy === "generate"}
                        onRetry={() => void startGeneration()}
                      />
                    </section>
                  )}
                  {detail && scriptShots.length === 0 && !latestVisibleJob && (
                    <p className="inline-empty">
                      尚未生成剧本。提交任务后，独立 Worker 会写入角色、场景和 3—5 个结构化镜头。
                    </p>
                  )}
                  {latestVisibleJob && (
                    <JobPanel
                      job={latestVisibleJob}
                      retrying={busy === "retry"}
                      onRetry={() => retryGeneration(latestVisibleJob)}
                      onViewResult={() => scrollToSection("result")}
                      configuredModelId={currentJobProviderDescriptor?.model_id}
                      configuredImageModelId={realImageProviderDescriptor?.model_id}
                      actualShotCount={
                        latestVisibleJob.status === "SUCCEEDED" ? scriptShots.length : undefined
                      }
                    />
                  )}
                </>
              )}
            </article>
          </div>
        </section>

        {detail && scriptShots.length > 0 && (
          <section
            className="section shots-section"
            id="shots-section"
            ref={shotsSectionRef}
            tabIndex={-1}
          >
            <div className="section-heading compact">
              <span className="section-number">03</span>
              <div>
                <p className="eyebrow">结构化剧本</p>
                <h2>{scriptShots.length} 个镜头</h2>
              </div>
            </div>

            <div className="script-overview">
              <div className="script-overview-heading">
                <div>
                  <p className="eyebrow">SCRIPT.V1</p>
                  <h3>{structuredScript?.title ?? detail.project.title}</h3>
                  <p>{structuredScript?.synopsis ?? "当前剧本未提供独立摘要。"}</p>
                </div>
                <dl className="script-trace">
                  <div><dt>Schema</dt><dd>{structuredScript?.schema_version ?? "兼容旧结构"}</dd></div>
                  <div><dt>Provider</dt><dd>{jobScriptProvider(scriptJob) ?? "未报告"}</dd></div>
                  <div><dt>模型</dt><dd>{jobModelId(scriptJob) ?? "任务未报告"}</dd></div>
                </dl>
              </div>
              {!structuredScript && (
                <p className="script-compatibility-note">
                  这是 M2 兼容项目：未保存完整 script.v1，以下镜头由 Shot 表兼容展示。
                </p>
              )}
              {(scriptWarnings.unusedSceneIds.length > 0 ||
                scriptWarnings.unusedCharacterIds.length > 0) && (
                <div className="script-validation-warning" role="status">
                  {scriptWarnings.unusedSceneIds.length > 0 && (
                    <p>
                      剧本包含 {scriptWarnings.unusedSceneIds.length} 个当前分镜未使用的场景：
                      <code>{scriptWarnings.unusedSceneIds.join("、")}</code>
                    </p>
                  )}
                  {scriptWarnings.unusedCharacterIds.length > 0 && (
                    <p>
                      剧本包含 {scriptWarnings.unusedCharacterIds.length} 个当前分镜未使用的角色：
                      <code>{scriptWarnings.unusedCharacterIds.join("、")}</code>
                    </p>
                  )}
                </div>
              )}
              <div className="script-entity-grid">
                <section className="script-entity-panel">
                  <h4>角色 · {scriptCharacters.length}</h4>
                  {scriptCharacters.length > 0 ? (
                    <ul>
                      {scriptCharacters.map((character, index) => (
                        <li key={character.id || `${character.name}-${index}`}>
                          <strong>
                            {character.name}
                            {` · ${character.role}`}
                          </strong>
                          <span>
                            {[character.appearance, character.personality, character.costume].join("；")}
                          </span>
                          <small>一致性提示：{character.consistency_prompt}</small>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>当前剧本未提供角色列表。</p>
                  )}
                </section>
                <section className="script-entity-panel">
                  <h4>场景 · {scriptScenes.length}</h4>
                  {scriptScenes.length > 0 ? (
                    <ul>
                      {scriptScenes.map((scene, index) => (
                        <li key={scene.id || `${scene.name}-${index}`}>
                          <strong>{scene.name}</strong>
                          <span>{scene.description}</span>
                          <small>{scene.time} · {scene.lighting}</small>
                          <small>一致性提示：{scene.consistency_prompt}</small>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>当前剧本未提供场景列表。</p>
                  )}
                </section>
              </div>
            </div>

            <section className="real-image-control" aria-labelledby="real-image-title">
              <div className="real-image-heading">
                <div>
                  <p className="eyebrow">M4-B · IMAGE PROVIDER</p>
                  <h3 id="real-image-title">使用当前剧本生成真实动漫画面</h3>
                </div>
                <span className={`visual-source-badge ${exportIsRealImage ? "is-real" : "is-mock"}`}>
                  当前成片：{exportIsRealImage ? "真实动漫视觉" : "Mock 视觉"}
                </span>
              </div>
              <div className="real-image-provider-line">
                <strong>
                  当前 Image Provider：
                  {realImageProviderDescriptor?.display_name ?? "Animagine XL 4.0（ComfyUI）"}
                </strong>
                <span>Provider ID：{REAL_IMAGE_PROVIDER_ID}</span>
                <span>模型：{realImageProviderDescriptor?.model_id ?? "animagine-xl-4.0-opt.safetensors"}</span>
                <span>
                  状态：
                  {realImageProviderDescriptor
                    ? realImageProviderDescriptor.available
                      ? "可启动"
                      : realImageProviderDescriptor.configured
                        ? "等待 GPU 交接"
                        : "配置不完整"
                    : "等待后端报告"}
                </span>
                {realImageProviderDescriptor?.detail && (
                  <span className="image-provider-detail">{realImageProviderDescriptor.detail}</span>
                )}
              </div>
              <div className={`gpu-handoff-notice ${gpuHandoffRequired ? "needs-action" : "is-ready"}`} role="status">
                <strong>8GB 显存互斥</strong>
                <span>
                  {gpuHandoffRequired
                    ? "检测到本地 Qwen 在线。请先自行停止 Qwen、释放 8081 和显存，再重新检查；平台不会结束外部进程。"
                    : "开始真实图像生成前仍会由后端复查 8081 与 llama-server；ComfyUI 与 Qwen 不会同时驻留 GPU。"}
                </span>
              </div>
              <dl className="real-image-facts">
                <div><dt>剧本来源</dt><dd>{scriptJob ? `Job ${scriptJob.id.slice(0, 8)}` : "没有可复用的成功 Job"}</dd></div>
                <div><dt>运行方式</dt><dd>lowvram · 单并发 · 顺序生成</dd></div>
                <div><dt>可复现</dt><dd>{jobBaseSeed(imageDisplayJob) ?? "提交后记录 base seed"}</dd></div>
                <div><dt>图片进度</dt><dd>{imageDisplayJob ? `${jobImageCompletedCount(imageDisplayJob)}/${jobImageTotalCount(imageDisplayJob, scriptShots.length)}` : `0/${scriptShots.length}`}</dd></div>
              </dl>
              {!realImageProviderConfigured && (
                <p className="provider-warning">真实图像 Provider 尚未配置完整，请检查模型文件和独立 ComfyUI 环境。</p>
              )}
              <div className="real-image-actions">
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => void startRealImageGeneration()}
                  disabled={
                    busy !== null ||
                    generationInProgress ||
                    !scriptJob ||
                    gpuHandoffRequired ||
                    !realImageProviderConfigured
                  }
                >
                  {busy === "real-image"
                    ? "正在提交真实图像任务…"
                    : imageGenerationInProgress
                      ? `正在生成 ${jobImageCompletedCount(activeJob)}/${jobImageTotalCount(activeJob, scriptShots.length)}`
                      : gpuHandoffRequired
                        ? "请先停止本地 Qwen"
                        : "使用当前剧本生成真实动漫画面"}
                </button>
                <button
                  className="button button-ghost"
                  type="button"
                  disabled={providerChecking || busy !== null}
                  onClick={() => void refreshProviderStatus()}
                >
                  {providerChecking ? "正在检查…" : "重新检查 Provider"}
                </button>
              </div>
              {imageRequestError && (
                <FailureCard
                  detail={imageRequestError}
                  retrying={busy === "real-image"}
                  onRetry={() => void startRealImageGeneration()}
                />
              )}
            </section>

            <div className="shot-grid">
              {[...scriptShots]
                .sort((left, right) => shotNumber(left, 0) - shotNumber(right, 0))
                .map((shot, index) => {
                  const sequence = shotNumber(shot, index + 1);
                  const sourceShotId = shot.shot_id ?? shot.id;
                  const generatedImage = generatedImageShots.find(
                    (image) =>
                      (sourceShotId && image.shot_id === sourceShotId) ||
                      numberValue(image.shot_index) === sequence,
                  );
                  const thumbnailUrl = imageAssetUrl(textValue(generatedImage?.image_url) ?? undefined);
                  const imageStatus =
                    textValue(generatedImage?.status) ?? (thumbnailUrl ? "SUCCEEDED" : "PENDING");
                  const hasRealImage =
                    Boolean(thumbnailUrl) &&
                    (generatedImage?.provider_id === REAL_IMAGE_PROVIDER_ID || isRealImageJob(imageDisplayJob));
                  const imageStatusLabel =
                    imageStatus === "RUNNING"
                      ? "正在生成"
                      : imageStatus === "FAILED"
                        ? "生成失败"
                        : imageStatus === "REUSED"
                          ? "已校验复用"
                        : imageStatus === "SUCCEEDED"
                          ? "已生成"
                          : "等待生成";
                  const displayedImageStatus = isRealImageJob(imageDisplayJob)
                    ? imageStatusLabel
                    : "Mock 已准备";
                  return (
                    <article
                      className={`shot-card ${hasRealImage ? "has-real-image" : ""}`}
                      key={shot.id ?? shot.shot_id ?? `${shot.title}-${index}`}
                    >
                      <div className="shot-art" data-shot={(index % 4) + 1}>
                        {hasRealImage && thumbnailUrl ? (
                          <img src={thumbnailUrl} alt={`第 ${sequence} 镜真实动漫关键帧：${shot.title}`} loading="lazy" />
                        ) : (
                          <i />
                        )}
                        <span className="shot-number">{String(sequence).padStart(2, "0")}</span>
                        <small className={`shot-image-status status-${imageStatus.toLowerCase()}`}>
                          {hasRealImage ? "真实模型" : isRealImageJob(imageDisplayJob) ? imageStatusLabel : "Mock 视觉"}
                        </small>
                      </div>
                      <div className="shot-copy">
                        <div className="shot-provider-row">
                          <p className="eyebrow">{shot.duration_seconds}s · 图像 {displayedImageStatus}</p>
                          <span className={`visual-source-badge ${hasRealImage ? "is-real" : "is-mock"}`}>
                            {hasRealImage ? "Animagine XL 4.0" : isRealImageJob(imageDisplayJob) ? "等待真实图片" : "Mock 视觉"}
                          </span>
                        </div>
                        <h3>{shot.title}</h3>
                        <p>{shot.visual_description}</p>
                        <blockquote>“{shot.narration}”</blockquote>
                        <dl className="shot-details">
                          <div>
                            <dt>Camera</dt>
                            <dd>{shot.camera ?? shot.camera_motion ?? "未提供"}</dd>
                          </div>
                          <div>
                            <dt>Image prompt</dt>
                            <dd>{shot.image_prompt ?? "未提供"}</dd>
                          </div>
                          {generatedImage && (
                            <div>
                              <dt>真实图片追溯</dt>
                              <dd>
                                seed {numberValue(generatedImage.seed) ?? "未报告"} · {numberValue(generatedImage.generation_seconds)?.toFixed(1) ?? "—"} 秒
                              </dd>
                            </div>
                          )}
                        </dl>
                      </div>
                    </article>
                  );
                })}
            </div>
            <div
              className={`shot-next-step next-${latestVisibleJob?.status?.toLowerCase() ?? "ready"}`}
              aria-live="polite"
            >
              {latestVisibleJob?.status === "QUEUED" || latestVisibleJob?.status === "RUNNING" ? (
                <p>镜头和成片正在生成，请查看任务进度。</p>
              ) : latestVisibleJob?.status === "FAILED" ? (
                <>
                  <p>生成失败，请返回任务区域查看错误并重试。</p>
                  <button className="button button-ghost" type="button" onClick={() => scrollToSection("project")}>
                    返回任务区域
                  </button>
                </>
              ) : media ? (
                <>
                  <p>镜头已准备完成，最终成片已生成。</p>
                  <button className="button button-primary" type="button" onClick={() => scrollToSection("result")}>
                    前往播放成片 ↓
                  </button>
                </>
              ) : (
                <p>镜头已准备完成，正在等待最终成片。</p>
              )}
            </div>
          </section>
        )}

        {detail?.latest_export && media && (
          <section
            className="section result-section is-ready"
            id="result-section"
            ref={resultSectionRef}
            tabIndex={-1}
          >
            <div className="section-heading light compact">
              <span className="section-number">04</span>
              <div>
                <p className="eyebrow">播放与下载</p>
                <h2 ref={resultTitleRef} tabIndex={-1}>
                  {exportIsRealImage ? "真实动漫成片" : "Mock 视觉成片"}
                </h2>
              </div>
            </div>
            {imageGenerationInProgress && !exportIsRealImage && (
              <p className="previous-export-notice">
                下方仍是上一版 Mock 视觉成片；新的真实动漫画面尚未完成，不会提前标记为真实模型输出。
              </p>
            )}
            <div className="result-status-summary" aria-label="成片状态">
              <span className="result-ready">● 已生成</span>
              <span className={`result-provider-badge ${exportIsRealImage ? "is-real" : "is-mock"}`}>
                {exportIsRealImage ? "真实模型 · Animagine XL 4.0" : "Mock 视觉"}
              </span>
              <span>{detail.latest_export.duration_seconds?.toFixed(2) ?? "—"} 秒</span>
              <span>{scriptShots.length} 个镜头</span>
              <span>要求：{shotCountLabel(exportDesiredShotCount)}</span>
              <span>实际：{exportActualShotCount ?? scriptShots.length} 个</span>
              <span>
                修复：{exportRepairUsed === null ? "未报告" : exportRepairUsed ? "已使用" : "未使用"}
              </span>
              <span>
                时长归一化：
                {exportDurationNormalization
                  ? exportDurationNormalization.normalized || exportDurationNormalization.applied
                    ? "已执行"
                    : "未执行"
                  : "未报告"}
              </span>
              <span>剧本：{scriptProviderUsed}</span>
              {exportIsRealImage && (
                <span>
                  图像耗时：{jobImageGenerationSeconds(exportJob)?.toFixed(1) ?? "未报告"} 秒
                </span>
              )}
            </div>
            <div className="result-grid">
              <div className="video-frame">
                <video controls preload="metadata" src={media.video}>
                  当前浏览器不支持 HTML5 视频，请下载 MP4 后播放。
                </video>
              </div>
              <aside className="export-info">
                <p className="eyebrow">EXPORT READY</p>
                <h3>{detail.project.title}</h3>
                <dl>
                  <div><dt>时长</dt><dd>{detail.latest_export.duration_seconds?.toFixed(2) ?? "—"} 秒</dd></div>
                  <div><dt>镜头</dt><dd>{scriptShots.length} 个</dd></div>
                  <div><dt>Script Provider</dt><dd title={scriptProviderUsed}>{scriptProviderUsed}</dd></div>
                  <div><dt>Script Model</dt><dd title={scriptModelUsed}>{scriptModelUsed}</dd></div>
                  <div><dt>Script Source</dt><dd title={scriptSourceUsed}>{scriptSourceUsed}</dd></div>
                  <div><dt>Image Provider</dt><dd title={imageProviderUsed}>{imageProviderUsed}</dd></div>
                  <div><dt>视觉来源</dt><dd>{exportIsRealImage ? "真实本地模型" : "Mock 确定性保底"}</dd></div>
                  {exportIsRealImage && <div><dt>Base seed</dt><dd>{jobBaseSeed(exportJob) ?? "未报告"}</dd></div>}
                  {exportIsRealImage && <div><dt>真实关键帧</dt><dd>{jobImageCompletedCount(exportJob)}/{jobImageTotalCount(exportJob, scriptShots.length)} 张</dd></div>}
                  <div><dt>Audio Provider</dt><dd title={audioProviderUsed}>{audioProviderUsed}</dd></div>
                  <div><dt>Video</dt><dd title={videoSourceUsed}>{videoSourceUsed}</dd></div>
                  <div>
                    <dt>SHA-256</dt>
                    <dd title={detail.latest_export.sha256}>{detail.latest_export.sha256?.slice(0, 12) ?? "—"}…</dd>
                  </div>
                </dl>
                <div className="download-actions">
                  <a className="button button-light" href={media.download} download>
                    下载 MP4
                  </a>
                  <a className="button button-outline-light" href={media.manifest} download>
                    下载 Manifest
                  </a>
                </div>
              </aside>
            </div>
          </section>
        )}
      </main>

      {latestVisibleJob && (
        <aside className={`task-shortcut shortcut-${latestVisibleJob.status.toLowerCase()}`} aria-live="polite">
          {latestVisibleJob.status === "QUEUED" || latestVisibleJob.status === "RUNNING" ? (
            <span>
              {isRealImageJob(latestVisibleJob)
                ? `正在生成真实图片 ${jobImageCompletedCount(latestVisibleJob)}/${jobImageTotalCount(latestVisibleJob, scriptShots.length)}`
                : "正在生成短片"}
              {` · ${Math.max(0, Math.min(100, Math.round(latestVisibleJob.progress)))}%`}
            </span>
          ) : latestVisibleJob.status === "SUCCEEDED" && media ? (
            <>
              <span>短片已生成</span>
              <button type="button" onClick={() => scrollToSection("result")}>查看成片</button>
            </>
          ) : latestVisibleJob.status === "FAILED" ? (
            <>
              <span>短片生成失败</span>
              <button type="button" onClick={() => scrollToSection("project")}>查看任务</button>
            </>
          ) : null}
        </aside>
      )}

      <footer>
        <span>纸鹤工坊 · M4-B 真实动漫关键帧纵向链路</span>
        <span>Mock 始终可辨识；Qwen 与 ComfyUI 在 8GB 显存下分阶段运行</span>
      </footer>
    </div>
  );
}

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ChangeEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import {
  API_BASE,
  ApiError,
  createProject,
  deleteBackgroundAudio,
  deleteProject,
  exportUrls,
  generateProject,
  getExternalImagePrompt,
  getHealth,
  getBackgroundAudio,
  getCompositionPlan,
  getJob,
  getProject,
  getProviders,
  imageAssetUrl,
  listProjects,
  mediaAssetUrl,
  renderRealAudio,
  renderRealImages,
  renderVideo,
  retryJob,
  smartRenderBestMedia,
  uploadBackgroundAudio,
  uploadExternalImage,
  updateVisualSelection,
  updateShotPlanning,
} from "./api";
import type {
  AudioProviderStatus,
  AudioSpeaker,
  BackgroundAudioAsset,
  BestMediaPlan,
  CompositionMode,
  DesiredShotCount,
  DurationNormalization,
  ExternalImagePromptBundle,
  ExternalImageSourceType,
  GeneratedAudioShot,
  GeneratedImageShot,
  GeneratedVideoShot,
  GenerationAttemptError,
  GenerationErrorDetail,
  GenerationJob,
  HealthStatus,
  ImageAssetRecord,
  JobStatus,
  MediaTimingPlan,
  MotionPreset,
  Project,
  ProjectDetail,
  ProvidersStatus,
  ScriptProviderId,
  ScriptProviderStatus,
  VideoMode,
  VideoProviderStatus,
} from "./types";
import { formatLocalDateTime } from "./local-time.js";
import { describeVideoVersion, selectVideoDisplayJob } from "./video-version.js";

const PAPER_CRANE_STORY =
  "深夜，少女在窗边折出一只纸鹤。纸鹤亮起微光，飞过屋顶、灯火与云层；黎明时，它飞向远方，少女在窗边静静注视。";
const PAPER_CRANE_TITLE = "纸鹤的夜航";
const REAL_IMAGE_PROVIDER_ID = "comfyui-animagine-xl-4";
const REAL_AUDIO_PROVIDER_ID = "qwen3-tts-0.6b-customvoice";
const STORY_MIN_CHARS = 10;
const STORY_MAX_CHARS = 3000;
const STORY_RECOMMENDED_MIN_CHARS = 50;
const STORY_RECOMMENDED_MAX_CHARS = 1000;
const PROJECTS_PER_PAGE = 8;
const SELECTED_PROJECT_STORAGE_KEY = "paper-crane:selected-project";

type SectionName = "create" | "project" | "shots" | "result";
type Notice = {
  kind: "info" | "success";
  message: string;
  action?: SectionName | "composition";
  actionLabel?: string;
};
type PresentedShot = {
  id?: string;
  shot_id?: string;
  index?: number;
  shot_index?: number;
  sequence_no?: number;
  title: string;
  scene_id?: string;
  character_ids?: string[];
  visual_description: string;
  narration: string;
  duration_seconds: number;
  camera?: string;
  camera_motion?: string;
  image_prompt?: string;
  provider_id?: string;
  generation_parameters?: Record<string, unknown>;
};

type ProjectSignal = {
  hasExport: boolean;
  realScript: boolean;
  realImage: boolean;
  realAudio: boolean;
  realChain: boolean;
};

type ProjectFilter = "all" | "real" | "mock";

type ProjectPreference = { urlId: string | null; storageId: string | null };

function readInitialProjectPreference(): ProjectPreference {
  if (typeof window === "undefined") return { urlId: null, storageId: null };
  const urlId = new URLSearchParams(window.location.search).get("project")?.trim() || null;
  let storageId: string | null = null;
  try {
    storageId = window.localStorage.getItem(SELECTED_PROJECT_STORAGE_KEY)?.trim() || null;
  } catch {
    storageId = null;
  }
  return { urlId, storageId };
}

function persistSelectedProjectId(projectId: string): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("project", projectId);
  window.history.replaceState(window.history.state, "", url);
  try {
    window.localStorage.setItem(SELECTED_PROJECT_STORAGE_KEY, projectId);
  } catch {
    // Private browsing or a disabled storage area must not block selection.
  }
}

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

function isRealAudioJob(job: GenerationJob | null | undefined): boolean {
  if (!job) return false;
  return (
    job.job_type === "GENERATE_REAL_AUDIO_VIDEO" ||
    job.job_type === "MEDIA_RERENDER" ||
    textValue(job.request_json?.audio_provider) === REAL_AUDIO_PROVIDER_ID ||
    textValue(job.result_json?.audio_provider) === REAL_AUDIO_PROVIDER_ID
  );
}

function isVideoJob(job: GenerationJob | null | undefined): boolean {
  return job?.job_type === "GENERATE_VIDEO";
}

function jobHasFinalMedia(job: GenerationJob | null | undefined): boolean {
  return Boolean(
    job?.result_json?.export_id ||
    job?.result_json?.video_url ||
    job?.result_json?.download_url,
  );
}

function jobVideoShots(job: GenerationJob | null | undefined): GeneratedVideoShot[] {
  const videos = job?.result_json?.video_shots;
  if (!Array.isArray(videos)) return [];
  return videos.filter(
    (item): item is GeneratedVideoShot =>
      recordValue(item) !== null && typeof item.shot_id === "string" && item.shot_id.length > 0,
  );
}

function jobAudioProvider(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  const provider = (
    textValue(job.result_json?.audio_provider) ??
    textValue(job.request_json?.audio_provider) ??
    (isRealAudioJob(job) ? textValue(job.provider_id) : null)
  );
  return provider === "reused"
    ? textValue(job.result_json?.source_audio_provider) ??
        textValue(job.request_json?.source_audio_provider) ??
        REAL_AUDIO_PROVIDER_ID
    : provider;
}

function jobAudioSpeaker(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  return textValue(job.result_json?.speaker) ?? textValue(job.request_json?.speaker);
}

function jobAudioLanguage(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  return textValue(job.result_json?.language) ?? textValue(job.request_json?.language);
}

function jobSourceImageId(job: GenerationJob | null | undefined): string | null {
  if (!job) return null;
  return (
    textValue(job.result_json?.source_image_job_id) ??
    textValue(job.request_json?.source_image_job_id)
  );
}

function jobAudioShots(job: GenerationJob | null | undefined): GeneratedAudioShot[] {
  const audios = job?.result_json?.audio_shots;
  if (!Array.isArray(audios)) return [];
  return audios.filter(
    (item): item is GeneratedAudioShot =>
      recordValue(item) !== null && typeof item.shot_id === "string" && item.shot_id.length > 0,
  );
}

function jobAudioCompletedCount(job: GenerationJob | null | undefined): number {
  if (!job) return 0;
  return (
    numberValue(job.result_json?.audio_completed_count) ??
    numberValue(job.result_json?.completed_audio_count) ??
    jobAudioShots(job).filter(
      (audio) =>
        audio.status === "SUCCEEDED" ||
        audio.status === "REUSED" ||
        Boolean(textValue(audio.audio_sha256)),
    ).length
  );
}

function jobAudioTotalCount(job: GenerationJob | null | undefined, fallback = 0): number {
  if (!job) return fallback;
  return (
    numberValue(job.result_json?.audio_total_count) ??
    numberValue(job.result_json?.total_audio_count) ??
    jobActualShotCount(job, fallback) ??
    fallback
  );
}

function jobAudioGenerationSeconds(job: GenerationJob | null | undefined): number | null {
  if (!job) return null;
  return (
    numberValue(job.result_json?.audio_generation_total_seconds) ??
    numberValue(job.result_json?.audio_generation_seconds) ??
    numberValue(job.result_json?.tts_generation_seconds)
  );
}

function jobTimingPlan(job: GenerationJob | null | undefined): MediaTimingPlan | null {
  const plan = recordValue(job?.result_json?.timing_plan);
  return plan ? (plan as MediaTimingPlan) : null;
}

function jobSourcePlannedDuration(job: GenerationJob | null | undefined): number | null {
  const plan = jobTimingPlan(job);
  return (
    numberValue(job?.result_json?.source_planned_duration_seconds) ??
    numberValue(plan?.source_planned_duration_seconds) ??
    numberValue(plan?.source_total_duration_seconds) ??
    numberValue(job?.result_json?.planned_duration_seconds)
  );
}

function jobRenderedPlannedDuration(job: GenerationJob | null | undefined): number | null {
  const plan = jobTimingPlan(job);
  return (
    numberValue(job?.result_json?.rendered_planned_duration_seconds) ??
    numberValue(plan?.rendered_planned_duration_seconds) ??
    numberValue(plan?.rendered_total_duration_seconds) ??
    numberValue(job?.result_json?.planned_duration_seconds)
  );
}

function jobAudioExtensionSeconds(job: GenerationJob | null | undefined): number | null {
  const plan = jobTimingPlan(job);
  return (
    numberValue(job?.result_json?.audio_extension_seconds) ??
    numberValue(job?.result_json?.extended_by_seconds) ??
    numberValue(plan?.audio_extension_seconds) ??
    numberValue(plan?.extended_by_seconds) ??
    (() => {
      const source = jobSourcePlannedDuration(job);
      const rendered = jobRenderedPlannedDuration(job);
      return source !== null && rendered !== null ? Math.max(0, rendered - source) : null;
    })()
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
  if (job.job_type === "MEDIA_RERENDER") {
    return "短片已生成。";
  }
  if (isVideoJob(job)) {
    return "动态镜头已准备完成，当前最终成片尚未包含这些新素材。";
  }
  if (isRealAudioJob(job)) {
    const completed = jobAudioCompletedCount(job);
    const total = jobAudioTotalCount(job, fallbackActual ?? 0);
    const speaker = jobAudioSpeaker(job) ?? "未报告音色";
    const elapsed = jobAudioGenerationSeconds(job);
    const countText = total > 0 ? `${completed}/${total} 段` : `${completed} 段`;
    const elapsedText = elapsed === null ? "" : `，TTS 共 ${elapsed.toFixed(1)} 秒`;
    return `AI 旁白生成完成：${countText}（${speaker}）${elapsedText}。`;
  }
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
  const audioFailure =
    stage?.startsWith("TTS_") === true ||
    stage?.startsWith("AUDIO_") === true ||
    code?.startsWith("TTS_") === true ||
    code?.startsWith("AUDIO_") === true ||
    textValue(detail?.provider_id) === REAL_AUDIO_PROVIDER_ID ||
    textValue(detail?.audio_provider) === REAL_AUDIO_PROVIDER_ID ||
    textValue(detail?.speaker) !== null;
  const imageFailure = !audioFailure && (
    stage?.startsWith("IMAGE_") === true ||
    stage?.startsWith("COMFYUI_") === true ||
    code === "GPU_HANDOFF_REQUIRED" ||
    code === "GPU_OOM" ||
    code === "MODEL_NOT_FOUND" ||
    code === "MODEL_HASH_MISMATCH"
  );
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
  const completedAudios =
    numberValue(detail?.audio_completed_count) ??
    numberValue(detail?.completed_audio_count) ??
    numberValue(detail?.completed_audios);
  const totalAudios =
    numberValue(detail?.audio_total_count) ??
    numberValue(detail?.total_audio_count) ??
    numberValue(detail?.total_audios);
  const reusableAudios = numberValue(detail?.reusable_audio_count);
  const failureSpeaker = textValue(detail?.speaker);
  const retryable = booleanValue(detail?.retryable);
  const requiresGpuHandoff =
    booleanValue(detail?.requires_gpu_handoff) === true ||
    booleanValue(detail?.requires_qwen_shutdown) === true ||
    code === "GPU_HANDOFF_REQUIRED";
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
      {inputLengthValid && stage !== "INPUT_VALIDATION" && !imageFailure && !audioFailure && (
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
          {imageFailure && <div><dt>需要释放 GPU</dt><dd>{requiresGpuHandoff ? "是" : "否"}</dd></div>}
          {imageFailure && <div><dt>发生显存不足</dt><dd>{oom ? "是" : "否"}</dd></div>}
          {audioFailure && <div><dt>失败镜头</dt><dd>{failedShotIndex !== null ? `第 ${failedShotIndex} 镜` : failedShotId ?? "尚未进入单镜生成"}</dd></div>}
          {audioFailure && <div><dt>已完成旁白</dt><dd>{completedAudios === null ? "未报告" : `${completedAudios}/${totalAudios ?? "?"} 段`}</dd></div>}
          {audioFailure && <div><dt>旁白音色</dt><dd>{failureSpeaker ?? "未报告"}</dd></div>}
          {audioFailure && <div><dt>可复用旁白</dt><dd>{reusableAudios === null ? "由重试任务重新校验" : `${reusableAudios} 段`}</dd></div>}
          {audioFailure && <div><dt>可直接重试</dt><dd>{retryable === null ? "未报告" : retryable ? "可以" : "不建议"}</dd></div>}
          {audioFailure && <div><dt>需要释放 GPU</dt><dd>{requiresGpuHandoff ? "是" : "否"}</dd></div>}
          {audioFailure && <div><dt>发生显存不足</dt><dd>{oom ? "是" : "否"}</dd></div>}
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
            <strong>最后一次修复输出</strong>
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
              {audioFailure
                ? "排除独立 TTS 环境、模型或显存问题后可手动重试；后端只会复用通过校验的旁白，不会回退 Mock。"
                : imageFailure
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

function isRealScriptJob(job: GenerationJob | null): boolean {
  if (!job || job.status !== "SUCCEEDED") return false;
  const sourceProvider =
    textValue(job.result_json?.source_script_provider) ??
    textValue(job.request_json?.source_script_provider);
  return jobScriptProvider(job) === "llamacpp" || sourceProvider === "llamacpp";
}

function summarizeProjectDetail(value: ProjectDetail): ProjectSignal {
  const succeededJobs = value.recent_jobs.filter((job) => job.status === "SUCCEEDED");
  const realScript = succeededJobs.some(isRealScriptJob);
  const realImage = succeededJobs.some((job) => {
    if (!isRealImageJob(job)) return false;
    return (
      job.result_json?.mock_image_fallback === false ||
      textValue(job.result_json?.image_source_type)?.includes("REAL") === true
    );
  });
  const realAudio = succeededJobs.some((job) => {
    if (!isRealAudioJob(job)) return false;
    return (
      job.result_json?.mock_audio_fallback === false ||
      textValue(job.result_json?.audio_source_type)?.includes("REAL") === true ||
      jobAudioProvider(job) === REAL_AUDIO_PROVIDER_ID
    );
  });
  const hasExport = value.latest_export !== null;
  return {
    hasExport,
    realScript,
    realImage,
    realAudio,
    realChain: hasExport && realScript && realImage && realAudio,
  };
}

function projectSignalLabel(signal: ProjectSignal | undefined): string {
  if (!signal) return "正在读取制作状态";
  if (signal.realChain) return "真实成片";
  if (signal.realScript || signal.realImage || signal.realAudio) return "真实链路未完成";
  return signal.hasExport ? "Mock 成片" : "Mock / 测试";
}

function projectSortScore(
  project: Project,
  signal: ProjectSignal | undefined,
  selectedId: string | null,
): number {
  if (signal?.realChain) return 0;
  if (signal?.hasExport) return 1;
  if (selectedId === project.id) return 2;
  if (signal?.realScript || signal?.realImage || signal?.realAudio) return 3;
  return 4;
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
  return formatLocalDateTime(value);
}

function providerName(providerId: ScriptProviderId): string {
  return providerId === "mock" ? "Mock 离线保底" : "本地 Qwen（llama.cpp）";
}

function shotNumber(shot: PresentedShot, fallback: number): number {
  return shot.index ?? shot.shot_index ?? shot.sequence_no ?? fallback;
}

function audioAssetUrl(value: unknown): string | null {
  return typeof value === "string" ? imageAssetUrl(value) : null;
}

function imageAssetLabel(asset: ImageAssetRecord): string {
  if (asset.source_type === "EXTERNAL_IMPORT") {
    if (asset.external_source_type === "AI_GENERATED") {
      return `External AI · ${asset.provider_hint || "其他服务"}`;
    }
    if (asset.external_source_type === "HUMAN_CREATED") return "人工制作 · Human-in-the-loop";
    return "其他外部素材 · Human-in-the-loop";
  }
  if (asset.provider_id === REAL_IMAGE_PROVIDER_ID) return "Animagine XL 4.0 · Local AI";
  return `${asset.provider_id} · ${asset.source_type}`;
}

function ShotImage({ src, sequence, title }: { src: string; sequence: number; title: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="media-load-error" role="status">
        <strong>图片加载失败</strong>
        <span>请检查后端媒体服务后重试。</span>
        <a href={src} target="_blank" rel="noreferrer">打开原始地址</a>
      </div>
    );
  }
  return (
    <a className="shot-image-link" href={src} target="_blank" rel="noreferrer" aria-label={`在新窗口查看第 ${sequence} 镜大图`}>
      <img
        src={src}
        alt={`第 ${sequence} 镜真实动漫关键帧：${title}`}
        loading="lazy"
        onError={() => setFailed(true)}
      />
    </a>
  );
}

function ShotAudioPlayer({
  src,
  sequence,
  missingReason,
}: {
  src: string | null;
  sequence: number;
  missingReason?: string;
}) {
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [src]);

  if (!src) {
    return (
      <div className="shot-audio-error" role="alert">
        <strong>旁白暂时无法播放</strong>
        <span>{missingReason ?? "AUDIO_ASSET_URL_MISSING：后端没有提供有效的公共音频地址。"}</span>
      </div>
    );
  }

  return (
    <div className="shot-audio-player">
      <span>逐镜头试听</span>
      <audio
        controls
        preload="metadata"
        src={src}
        aria-label={`第 ${sequence} 镜旁白试听`}
        onLoadedMetadata={() => setFailed(false)}
        onError={() => setFailed(true)}
      />
      {failed && (
        <span className="shot-audio-error" role="alert">
          AUDIO_DECODE_FAILED：旁白加载失败，请检查后端媒体服务后重试。
        </span>
      )}
    </div>
  );
}

function JobPanel({
  job,
  retrying,
  onRetry,
  onViewResult,
  configuredModelId,
  configuredImageModelId,
  configuredAudioModelId,
  actualShotCount,
}: {
  job: GenerationJob;
  retrying: boolean;
  onRetry: () => void;
  onViewResult: () => void;
  configuredModelId?: string | null;
  configuredImageModelId?: string | null;
  configuredAudioModelId?: string | null;
  actualShotCount?: number;
}) {
  const audioJob = isRealAudioJob(job);
  const imageJob = !audioJob && isRealImageJob(job);
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
  const audioCompleted = jobAudioCompletedCount(job);
  const audioTotal = jobAudioTotalCount(job, actualShotCount ?? 0);
  const audioElapsed = jobAudioGenerationSeconds(job);
  const audioSpeaker = jobAudioSpeaker(job);
  const audioLanguage = jobAudioLanguage(job);
  const audioModelLabel =
    textValue(job.result_json?.audio_model_id) ??
    configuredAudioModelId ??
    "任务尚未报告";
  const sourcePlannedDuration = jobSourcePlannedDuration(job);
  const renderedPlannedDuration = jobRenderedPlannedDuration(job);
  const audioExtension = jobAudioExtensionSeconds(job);
  const currentAudioShotIndex =
    numberValue(job.result_json?.current_audio_shot_index) ?? currentShotIndex;
  const currentAudioShotId =
    textValue(job.result_json?.current_audio_shot_id) ?? currentShotId;
  const jobStage = textValue(job.result_json?.stage);
  return (
    <section className={`job-panel job-${job.status.toLowerCase()}`} aria-live="polite">
      <div className="job-heading">
        <div>
          <span className="eyebrow">
            {audioJob ? "真实 AI 旁白与成片任务" : imageJob ? "真实图像与成片任务" : "生成任务"}
          </span>
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
        {audioJob
          ? ` Audio Provider：${jobAudioProvider(job) ?? "未报告"} · 模型：${audioModelLabel}`
          : imageJob
          ? ` 图像 Provider：${jobImageProvider(job) ?? "未报告"} · 模型：${imageModelLabel}`
          : ` 剧本 Provider：${jobScriptProvider(job) ?? "未报告"} · 模型：${modelLabel}`}
      </p>
      <dl className="job-facts">
        {audioJob ? (
          <>
            <div><dt>旁白进度</dt><dd>{audioCompleted}/{audioTotal || "?"} 段</dd></div>
            <div><dt>当前阶段</dt><dd>{jobStage ?? (job.status === "QUEUED" ? "等待 Worker" : "TTS 生成")}</dd></div>
            <div><dt>当前镜头</dt><dd>{currentAudioShotIndex !== null ? `第 ${currentAudioShotIndex} 镜` : currentAudioShotId ?? "未报告"}</dd></div>
            <div><dt>音色</dt><dd>{audioSpeaker ?? "未报告"}</dd></div>
            <div><dt>语言</dt><dd>{audioLanguage ?? "未报告"}</dd></div>
            <div><dt>TTS 总耗时</dt><dd>{audioElapsed === null ? "生成后报告" : `${audioElapsed.toFixed(1)} 秒`}</dd></div>
            <div><dt>源 Script Job</dt><dd title={jobSourceScriptId(job) ?? undefined}>{jobSourceScriptId(job)?.slice(0, 8) ?? "未报告"}</dd></div>
            {jobSourceImageId(job) && (
              <div><dt>兼容的源图像 Job</dt><dd title={jobSourceImageId(job) ?? undefined}>{jobSourceImageId(job)?.slice(0, 8)}</dd></div>
            )}
            <div><dt>并发</dt><dd>1（同次加载、顺序生成）</dd></div>
          </>
        ) : imageJob ? (
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
        <div><dt>{audioJob ? "源计划时长" : "计划时长"}</dt><dd>{(audioJob ? sourcePlannedDuration : plannedDuration) === null ? "未报告" : `${(audioJob ? sourcePlannedDuration : plannedDuration)!.toFixed(3)} 秒`}</dd></div>
        {audioJob && <div><dt>渲染计划时长</dt><dd>{renderedPlannedDuration === null ? "未报告" : `${renderedPlannedDuration.toFixed(3)} 秒`}</dd></div>}
        <div><dt>编码时长</dt><dd>{encodedDuration === null ? "未报告" : `${encodedDuration.toFixed(3)} 秒`}</dd></div>
        {audioJob && <div><dt>旁白延长</dt><dd>{audioExtension === null ? "未报告" : `${audioExtension.toFixed(3)} 秒`}</dd></div>}
      </dl>
      {audioJob && (job.status === "RUNNING" || audioCompleted > 0) && (
        <p className="job-hint audio-progress-copy">
          已完成 {audioCompleted}/{audioTotal || "?"} 段真实旁白；本 Job 只加载一次 Qwen3-TTS，并按镜头顺序单并发生成。
        </p>
      )}
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
          {audioJob
            ? "真实旁白任务已入队。请确保文本 Qwen 与 ComfyUI 已停止并释放 GPU；失败不会回退 Mock 音频。"
            : imageJob
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
          {job.job_type === "MEDIA_RERENDER" && jobHasFinalMedia(job) && (
            <button className="button button-success" type="button" onClick={onViewResult}>
              查看成片
            </button>
          )}
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
  const llamaRuntimeState = descriptors.get("llamacpp")?.runtime_state;
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
            {initialCheckInProgress
              ? " — 检查中"
              : llamaRuntimeState === "ONLINE"
                ? " — 在线"
                : llamaRuntimeState === "READY_TO_START"
                  ? " — 可按需启动"
                  : " — 配置不可用"}
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
                : descriptor?.runtime_state === "ONLINE"
                  ? "在线"
                  : descriptor?.runtime_state === "READY_TO_START"
                    ? "可按需启动"
                    : available
                      ? "已配置"
                  : descriptor?.configured === false
                    ? "离线（未配置）"
                    : descriptor?.runtime_state === "PORT_CONFLICT"
                      ? "端口冲突"
                      : "不可用";
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
          本地 Qwen 配置或端口状态不可用，不会提交任务。请检查可执行文件、GGUF 模型和 8081 端口后重新检查。
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
    { key: "create", number: "01", label: "项目与故事" },
    { key: "project", number: "02", label: "AI 剧本" },
    { key: "shots", number: "03", label: "动漫画面" },
    { key: "result", number: "04", label: "配音与成片" },
  ];
  return (
    <nav className="stage-navigation" aria-label="制作流程">
      {stages.map((stage) => {
        const state = current === stage.key ? "current" : completed[stage.key] ? "done" : "pending";
        const stateLabel = state === "current" ? "当前" : state === "done" ? "已完成" : available[stage.key] ? "可以开始" : "未开始";
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
            <span className="stage-copy"><strong>{stage.label}</strong><small>{stateLabel}</small></span>
          </button>
        );
      })}
    </nav>
  );
}

function StageAccordion({
  title,
  summary,
  status,
  open,
  children,
}: {
  title: string;
  summary: string;
  status: string;
  open: boolean;
  children: ReactNode;
}) {
  return (
    <details className="stage-accordion" open={open}>
      <summary>
        <span className="stage-accordion-copy">
          <strong>{title}</strong>
          <small>{summary}</small>
        </span>
        <span className="stage-accordion-status">{status}</span>
      </summary>
      <div className="stage-accordion-body">{children}</div>
    </details>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState("");
  const [providersStatus, setProvidersStatus] = useState<ProvidersStatus | null>(null);
  const [providerError, setProviderError] = useState("");
  const [providerChecking, setProviderChecking] = useState(false);
  const [scriptProvider, setScriptProvider] = useState<ScriptProviderId>("mock");
  const [audioSpeaker, setAudioSpeaker] = useState<AudioSpeaker>("Serena");
  const [videoMode, setVideoMode] = useState<VideoMode>("keyframe_motion");
  const [motionPreset, setMotionPreset] = useState<MotionPreset>("gentle_zoom");
  const [backgroundAudioEnabled, setBackgroundAudioEnabled] = useState(false);
  const [backgroundVolume, setBackgroundVolume] = useState(0.12);
  const [backgroundAudio, setBackgroundAudio] = useState<BackgroundAudioAsset | null>(null);
  const [backgroundLoading, setBackgroundLoading] = useState(false);
  const [desiredShotCount, setDesiredShotCount] = useState<DesiredShotCount>(4);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectSignals, setProjectSignals] = useState<Record<string, ProjectSignal>>({});
  const [projectSearch, setProjectSearch] = useState("");
  const [projectFilter, setProjectFilter] = useState<ProjectFilter>("all");
  const [projectPage, setProjectPage] = useState(1);
  const [initialProjectPreference] = useState<ProjectPreference>(() => readInitialProjectPreference());
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
  const [audioRequestError, setAudioRequestError] =
    useState<GenerationErrorDetail | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [externalPrompts, setExternalPrompts] = useState<
    Record<string, ExternalImagePromptBundle>
  >({});
  const [shotPlanningDrafts, setShotPlanningDrafts] = useState<
    Record<string, { keyframe: string; motion: string }>
  >({});
  const [selectedVideoImageAssets, setSelectedVideoImageAssets] = useState<
    Record<string, string>
  >({});
  const [selectedFinalVideoJobId, setSelectedFinalVideoJobId] = useState("");
  const [targetVideoShotIds, setTargetVideoShotIds] = useState<string[]>([]);
  const [imageCompositionPlan, setImageCompositionPlan] = useState<BestMediaPlan | null>(null);
  const [videoCompositionPlan, setVideoCompositionPlan] = useState<BestMediaPlan | null>(null);
  const [externalSourceTypes, setExternalSourceTypes] = useState<
    Record<string, ExternalImageSourceType>
  >({});
  const [externalProviderHints, setExternalProviderHints] = useState<Record<string, string>>({});
  const [deleteCandidate, setDeleteCandidate] = useState<Project | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const createSectionRef = useRef<HTMLElement>(null);
  const projectSectionRef = useRef<HTMLElement>(null);
  const shotsSectionRef = useRef<HTMLElement>(null);
  const compositionSectionRef = useRef<HTMLElement>(null);
  const resultSectionRef = useRef<HTMLElement>(null);
  const projectTitleRef = useRef<HTMLHeadingElement>(null);
  const resultTitleRef = useRef<HTMLHeadingElement>(null);
  const creationInFlightRef = useRef(false);
  const deletionInFlightRef = useRef(false);
  const providerSelectionTouchedRef = useRef(false);
  const pendingNavigationRef = useRef<"project" | "composition" | "result" | null>(null);
  const handledSucceededJobsRef = useRef(new Set<string>());
  const projectSignalsRef = useRef<Record<string, ProjectSignal>>({});
  const mediaPolishOptions = {
    motionPreset,
    backgroundAudioEnabled,
    backgroundVolume,
  };

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

  const scrollToComposition = useCallback(() => {
    const target = compositionSectionRef.current;
    if (!target) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    target.focus({ preventScroll: true });
  }, []);

  const loadProjectSignals = useCallback(async (items: Project[]) => {
    const missing = items.filter((project) => !projectSignalsRef.current[project.id]);
    if (missing.length === 0) return;
    const resolved = await Promise.all(
      missing.map(async (project) => {
        try {
          const value = await getProject(project.id);
          return [project.id, summarizeProjectDetail(value)] as const;
        } catch {
          return null;
        }
      }),
    );
    const next = { ...projectSignalsRef.current };
    for (const item of resolved) {
      if (item) next[item[0]] = item[1];
    }
    projectSignalsRef.current = next;
    setProjectSignals(next);
    setSelectedId((current) => {
      if (current || items.length === 0) return current;
      const persisted =
        items.find((project) => project.id === initialProjectPreference.urlId) ??
        items.find((project) => project.id === initialProjectPreference.storageId);
      if (persisted) return persisted.id;
      return [...items]
        .sort(
          (left, right) =>
            projectSortScore(left, next[left.id], null) -
            projectSortScore(right, next[right.id], null),
        )[0]?.id ?? null;
    });
  }, [initialProjectPreference]);

  const refreshProjects = useCallback(async () => {
    const items = await listProjects();
    setProjects(items);
    void loadProjectSignals(items);
    return items;
  }, [loadProjectSignals]);

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
    setSelectedVideoImageAssets(value.visual_selection.source_image_asset_ids ?? {});
    setSelectedFinalVideoJobId(value.visual_selection.source_video_job_id ?? "");
    const [imagePlan, videoPlan] = await Promise.all([
      getCompositionPlan(projectId, "IMAGE_ONLY").catch(() => null),
      getCompositionPlan(projectId, "VIDEO_PREFERRED").catch(() => null),
    ]);
    setImageCompositionPlan(imagePlan);
    setVideoCompositionPlan(videoPlan);
    const promptShots = value.project.script_json?.shots ?? [];
    const validShotIds = promptShots.map((shot) => shot.id);
    setTargetVideoShotIds((current) => {
      const retained = current.filter((shotId) => validShotIds.includes(shotId));
      return retained.length > 0 ? retained : validShotIds;
    });
    const promptResults = await Promise.all(
      promptShots.map(async (shot) => {
        try {
          return await getExternalImagePrompt(projectId, shot.id);
        } catch {
          return null;
        }
      }),
    );
    setExternalPrompts(
      Object.fromEntries(
        promptResults
          .filter((item): item is ExternalImagePromptBundle => item !== null)
          .map((item) => [item.shot_id, item]),
      ),
    );
    setShotPlanningDrafts(
      Object.fromEntries(
        promptResults
          .filter((item): item is ExternalImagePromptBundle => item !== null)
          .map((item) => [
            item.shot_id,
            {
              keyframe: String(item.source_fields.visual_description ?? ""),
              motion: String(item.source_fields.motion_description ?? ""),
            },
          ]),
      ),
    );
    const signal = summarizeProjectDetail(value);
    projectSignalsRef.current = { ...projectSignalsRef.current, [projectId]: signal };
    setProjectSignals(projectSignalsRef.current);
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
        if (items.length === 0) setSelectedId(null);
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
      setExternalPrompts({});
      setSelectedVideoImageAssets({});
      setSelectedFinalVideoJobId("");
      setTargetVideoShotIds([]);
      setImageCompositionPlan(null);
      setVideoCompositionPlan(null);
      return;
    }
    setDetail(null);
    setExternalPrompts({});
    setSelectedVideoImageAssets({});
    setSelectedFinalVideoJobId("");
    setTargetVideoShotIds([]);
    setImageCompositionPlan(null);
    setVideoCompositionPlan(null);
    setActiveJob(null);
    setError("");
    setGenerationRequestError(null);
    setImageRequestError(null);
    setAudioRequestError(null);
    refreshDetail(selectedId).catch((cause: unknown) => setError(readableError(cause)));
  }, [refreshDetail, selectedId]);

  useEffect(() => {
    if (selectedId && projects.some((project) => project.id === selectedId)) {
      persistSelectedProjectId(selectedId);
    }
  }, [projects, selectedId]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedId) {
      setBackgroundAudio(null);
      setBackgroundAudioEnabled(false);
      return;
    }
    setBackgroundAudio(null);
    setBackgroundAudioEnabled(false);
    setBackgroundLoading(true);
    getBackgroundAudio(selectedId)
      .then((asset) => {
        if (cancelled) return;
        setBackgroundAudio(asset);
        if (!asset) setBackgroundAudioEnabled(false);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(`背景音状态读取失败：${readableError(cause)}`);
      })
      .finally(() => {
        if (!cancelled) setBackgroundLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  useEffect(() => {
    if (!activeJob || (activeJob.status !== "QUEUED" && activeJob.status !== "RUNNING")) return;

    let cancelled = false;
    const timer = window.setInterval(() => {
      getJob(activeJob.id)
        .then(async (job) => {
          if (cancelled) return;
          const newlySucceeded =
            job.status === "SUCCEEDED" &&
            activeJob.status !== "SUCCEEDED" &&
            !handledSucceededJobsRef.current.has(job.id);
          setActiveJob(job);
          if (job.status === "SUCCEEDED" || job.status === "FAILED") {
            window.clearInterval(timer);
            await Promise.all([refreshDetail(job.project_id), refreshProjects()]);
            if (newlySucceeded) {
              handledSucceededJobsRef.current.add(job.id);
              if (isVideoJob(job)) {
                pendingNavigationRef.current = "composition";
                setNotice({
                  kind: "success",
                  message: "动态镜头已准备完成并设为当前版本；当前最终成片尚未包含这些新素材。",
                  action: "composition",
                  actionLabel: "前往成片合成",
                });
              } else if (job.job_type === "MEDIA_RERENDER" && jobHasFinalMedia(job)) {
                pendingNavigationRef.current = "result";
                setNotice({
                  kind: "success",
                  message: generationSuccessSummary(job),
                  action: "result",
                  actionLabel: "查看成片",
                });
              } else {
                setNotice({ kind: "success", message: generationSuccessSummary(job) });
              }
            }
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
    if (
      pendingNavigationRef.current !== "composition" ||
      !detail?.project.id ||
      detail.project.id !== selectedId
    ) return;
    pendingNavigationRef.current = null;
    scrollToComposition();
  }, [detail?.project.id, notice?.action, scrollToComposition, selectedId, videoCompositionPlan]);

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
    setAudioRequestError(null);
    setNotice(null);
    try {
      if (scriptProvider === "llamacpp") {
        const latestStatus = await refreshProviderStatus();
        const localQwenReady = latestStatus?.providers.some(
          (provider) => provider.provider_id === "llamacpp" && provider.available,
        );
        if (!localQwenReady) {
          setError("本地 Qwen 当前无法按需启动，未提交生成任务。请检查模型配置和 8081 端口后重新检查。");
          return;
        }
      }
      const job = await generateProject(
        selectedId,
        scriptProvider,
        desiredShotCount,
        mediaPolishOptions,
      );
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
    setImageRequestError(null);
    setAudioRequestError(null);
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
    setAudioRequestError(null);
    setNotice(null);
    try {
      const job = await renderRealImages(
        selectedId,
        scriptJob.id,
        undefined,
        mediaPolishOptions,
      );
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

  const startRealAudioGeneration = async () => {
    if (!selectedId || !scriptJob) return;
    setBusy("real-audio");
    setError("");
    setGenerationRequestError(null);
    setImageRequestError(null);
    setAudioRequestError(null);
    setNotice(null);
    try {
      const job = await renderRealAudio(
        selectedId,
        scriptJob.id,
        null,
        audioSpeaker,
        mediaPolishOptions,
        successfulVideoJob?.id ?? null,
        selectedVideoImageAssets,
      );
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const structured = generationErrorValue(cause.detail);
        if (structured) {
          setAudioRequestError(structured);
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

  const startVideoGeneration = async () => {
    const targetShotIds = targetVideoShotIds.filter((shotId) =>
      scriptShots.some((shot) => (shot.shot_id ?? shot.id) === shotId),
    );
    if (
      !selectedId ||
      targetShotIds.length === 0 ||
      (!sourceImageJob && !hasTargetVideoSource) ||
      (videoMode === "cloud-wan-2.7" && !hasExplicitTargetVideoSource) ||
      videoMode === "keyframe_motion"
    ) return;
    setBusy("video");
    setError("");
    setNotice(null);
    try {
      const job = await renderVideo(
        selectedId,
        sourceImageJob?.id ?? null,
        motionPreset,
        videoMode,
        selectedVideoImageAssets,
        targetShotIds,
      );
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      setError(`动态视频任务提交失败：${readableError(cause)}`);
    } finally {
      setBusy(null);
    }
  };

  const copyExternalPrompt = async (shotId: string) => {
    const bundle = externalPrompts[shotId];
    if (!bundle) return;
    try {
      await navigator.clipboard.writeText(bundle.prompt);
      setNotice({
        kind: "success",
        message: "已复制，可在 ChatGPT Images 等外部服务中生成后导回。",
      });
    } catch (cause) {
      setError(`复制外部生成提示词失败：${readableError(cause)}`);
    }
  };

  const saveShotPlanning = async (shotId: string, reset = false) => {
    if (!selectedId) return;
    const draft = shotPlanningDrafts[shotId];
    if (!draft && !reset) return;
    setBusy(`shot-planning-${shotId}`);
    setError("");
    try {
      await updateShotPlanning(selectedId, shotId, {
        keyframe_description: reset ? null : draft.keyframe,
        motion_description: reset ? null : draft.motion,
      });
      await refreshDetail(selectedId);
      setNotice({
        kind: "success",
        message: reset ? "已恢复原始 LLM 镜头规划。" : "制作层镜头规划已保存，后续任务将冻结有效值。",
      });
    } catch (cause) {
      setError(`镜头规划保存失败：${readableError(cause)}`);
    } finally {
      setBusy(null);
    }
  };

  const importExternalKeyframe = async (
    shotId: string,
    event: ChangeEvent<HTMLInputElement>,
  ) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!selectedId || !file) return;
    const sourceType = externalSourceTypes[shotId] ?? "AI_GENERATED";
    const providerHint = externalProviderHints[shotId] ?? "ChatGPT Images";
    setBusy(`external-image-${shotId}`);
    setError("");
    try {
      await uploadExternalImage(
        selectedId,
        shotId,
        file,
        sourceType,
        providerHint,
      );
      await refreshDetail(selectedId);
      setNotice({
        kind: "success",
        message: "外部关键帧已导入并设为该镜头的视频首帧。",
      });
    } catch (cause) {
      setError(`外部关键帧导入失败：${readableError(cause)}`);
    } finally {
      setBusy(null);
    }
  };

  const persistImageSelection = async (shotId: string, assetId: string) => {
    if (!selectedId) return;
    const next = { ...selectedVideoImageAssets };
    if (assetId) next[shotId] = assetId;
    else delete next[shotId];
    setBusy(`visual-selection-${shotId}`);
    setError("");
    try {
      const persisted = await updateVisualSelection(
        selectedId,
        next,
        selectedFinalVideoJobId || null,
      );
      setSelectedVideoImageAssets(persisted.source_image_asset_ids);
      setSelectedFinalVideoJobId(persisted.source_video_job_id ?? "");
      await refreshDetail(selectedId);
    } catch (cause) {
      setError(`关键帧选择保存失败：${readableError(cause)}`);
      await refreshDetail(selectedId);
    } finally {
      setBusy(null);
    }
  };

  const persistVideoSelection = async (videoJobId: string) => {
    if (!selectedId) return;
    setBusy(`visual-selection-video-${videoJobId}`);
    setError("");
    setNotice(null);
    try {
      const persisted = await updateVisualSelection(
        selectedId,
        selectedVideoImageAssets,
        videoJobId,
      );
      if (persisted.source_video_job_id !== videoJobId) {
        throw new Error("后端未确认目标动态视频版本，请重新加载后再试。");
      }
      setSelectedVideoImageAssets(persisted.source_image_asset_ids);
      setSelectedFinalVideoJobId(videoJobId);
      const refreshed = await refreshDetail(selectedId);
      if (refreshed.visual_selection.source_video_job_id !== videoJobId) {
        throw new Error("项目重读结果与刚选择的动态视频版本不一致。");
      }
      setNotice({
        kind: "success",
        message: `已将 Video Job ${videoJobId.slice(0, 8)} 设为当前动态视频版本。`,
        action: "composition",
        actionLabel: "查看成片计划",
      });
    } catch (cause) {
      setError(`动态视频来源保存失败：${readableError(cause)}`);
      await refreshDetail(selectedId);
    } finally {
      setBusy(null);
    }
  };

  const startSmartMediaRender = async (
    compositionMode: CompositionMode,
    plan: BestMediaPlan | null,
  ) => {
    if (!selectedId || plan?.status !== "READY") return;
    setBusy(`smart-media-render-${compositionMode}`);
    setError("");
    setNotice(null);
    try {
      const job = await smartRenderBestMedia(selectedId, compositionMode, mediaPolishOptions);
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      setError(`成片合成提交失败：${readableError(cause)}`);
    } finally {
      setBusy(null);
    }
  };

  const uploadProjectBackgroundAudio = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!selectedId || !file) return;
    setBusy("background-upload");
    setError("");
    try {
      const asset = await uploadBackgroundAudio(selectedId, file);
      setBackgroundAudio(asset);
      setBackgroundAudioEnabled(true);
      setNotice({ kind: "success", message: `背景音“${asset.original_filename}”已上传并通过媒体校验。` });
    } catch (cause) {
      setError(`背景音上传失败：${readableError(cause)}`);
    } finally {
      setBusy(null);
    }
  };

  const removeProjectBackgroundAudio = async () => {
    if (!selectedId || !backgroundAudio) return;
    setBusy("background-delete");
    setError("");
    try {
      await deleteBackgroundAudio(selectedId);
      setBackgroundAudio(null);
      setBackgroundAudioEnabled(false);
      setNotice({ kind: "success", message: "项目背景音已删除，后续任务将保持原有音频行为。" });
    } catch (cause) {
      setError(`删除背景音失败：${readableError(cause)}`);
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
  const audioProviderDescriptors = providersStatus?.audio_providers ?? [];
  const videoProviderDescriptors = providersStatus?.video_providers ?? [];
  const selectedProviderDescriptor = providerDescriptors.find(
    (provider) => provider.provider_id === scriptProvider,
  );
  const llamaAvailable = providerDescriptors.some(
    (provider) => provider.provider_id === "llamacpp" && provider.available,
  ) && !providerError;
  const llamaRunning = providerDescriptors.some(
    (provider) =>
      provider.provider_id === "llamacpp" && provider.runtime_state === "ONLINE",
  ) && !providerError;
  const realImageProviderDescriptor = imageProviderDescriptors.find(
    (provider) => provider.provider_id === REAL_IMAGE_PROVIDER_ID,
  );
  const realImageProviderConfigured = realImageProviderDescriptor?.configured !== false;
  const realAudioProviderDescriptor: AudioProviderStatus | undefined =
    audioProviderDescriptors.find(
      (provider) => provider.provider_id === REAL_AUDIO_PROVIDER_ID,
    );
  const realAudioProviderConfigured = realAudioProviderDescriptor?.configured !== false;
  const mockVideoProviderDescriptor: VideoProviderStatus | undefined =
    videoProviderDescriptors.find((provider) => provider.provider_id === "mock-video");
  const cloudVideoProviderDescriptor: VideoProviderStatus | undefined =
    videoProviderDescriptors.find((provider) => provider.provider_id === "cloud-wan-2.7");
  const selectedVideoProviderDescriptor =
    videoMode === "mock-video"
      ? mockVideoProviderDescriptor
      : videoMode === "cloud-wan-2.7"
        ? cloudVideoProviderDescriptor
        : undefined;
  const gpuHandoffRequired =
    llamaRunning ||
    realImageProviderDescriptor?.requires_gpu_handoff === true ||
    realAudioProviderDescriptor?.requires_gpu_handoff === true;
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
  const imageAssets = detail?.image_assets ?? [];
  const effectiveTargetVideoShotIds = targetVideoShotIds.filter((targetShotId) =>
    scriptShots.some((shot) => (shot.shot_id ?? shot.id) === targetShotId),
  );
  const hasExplicitTargetVideoSource =
    effectiveTargetVideoShotIds.length > 0 &&
    effectiveTargetVideoShotIds.every((shotId) => Boolean(selectedVideoImageAssets[shotId]));
  const exportJob = detail?.latest_export
    ? detail.recent_jobs.find((job) => job.id === detail.latest_export?.job_id) ?? null
    : null;
  const referencedScriptJobId =
    jobSourceScriptId(latestVisibleJob) ?? jobSourceScriptId(exportJob);
  const referencedScriptJob = referencedScriptJobId
    ? detail?.recent_jobs.find((job) => job.id === referencedScriptJobId) ?? null
    : null;
  const scriptJob =
    detail?.matching_script_job ??
    (referencedScriptJob?.status === "SUCCEEDED" ? referencedScriptJob : null) ??
    detail?.recent_jobs.find(
      (job) =>
        job.status === "SUCCEEDED" &&
        !isRealImageJob(job) &&
        !isRealAudioJob(job) &&
        recordValue(job.result_json?.script_trace) !== null,
    ) ??
    null;
  const scriptProviderUsed = jobScriptProvider(exportJob) ?? "未报告";
  const scriptModelUsed = jobModelId(exportJob) ?? jobModelId(scriptJob) ?? "未报告";
  const scriptSourceUsed = textValue(exportJob?.result_json?.script_source_type) ?? "未报告";
  const referencedImageJobId =
    jobSourceImageId(latestVisibleJob) ?? jobSourceImageId(exportJob);
  const referencedImageJob = referencedImageJobId
    ? detail?.recent_jobs.find((job) => job.id === referencedImageJobId) ?? null
    : null;
  const sourceImageJob =
    (referencedImageJob?.status === "SUCCEEDED" && isRealImageJob(referencedImageJob)
      ? referencedImageJob
      : null) ??
    detail?.recent_jobs.find(
      (job) => job.status === "SUCCEEDED" && isRealImageJob(job),
    ) ??
    null;
  const hasTargetVideoSource = Boolean(sourceImageJob) || hasExplicitTargetVideoSource;
  const sourceImageProviderUsed = jobImageProvider(sourceImageJob);
  const imageProviderUsed =
    textValue(exportJob?.result_json?.image_provider) ??
    sourceImageProviderUsed ??
    "未报告";
  const audioProviderUsed = jobAudioProvider(exportJob) ?? "未报告";
  const videoSourceUsed =
    textValue(exportJob?.result_json?.video_source_type) ??
    textValue(exportJob?.result_json?.source_type) ??
    "未报告";
  const exportDesiredShotCount = jobDesiredShotCount(exportJob);
  const exportActualShotCount = jobActualShotCount(exportJob, scriptShots.length);
  const exportRepairUsed = jobRepairUsed(exportJob);
  const exportDurationNormalization = jobDurationNormalization(exportJob);
  const exportIsRealAudio =
    exportJob?.status === "SUCCEEDED" && audioProviderUsed === REAL_AUDIO_PROVIDER_ID;
  const exportIsRealImage =
    exportJob?.status === "SUCCEEDED" &&
    (imageProviderUsed === REAL_IMAGE_PROVIDER_ID || sourceImageProviderUsed === REAL_IMAGE_PROVIDER_ID);
  const imageDisplayJob =
    (isRealImageJob(latestVisibleJob) ? latestVisibleJob : null) ??
    (isRealImageJob(exportJob) ? exportJob : null) ??
    sourceImageJob ??
    null;
  const generatedImageShots = jobImageShots(imageDisplayJob);
  const imageGenerationInProgress =
    isRealImageJob(activeJob) &&
    (activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING");
  const successfulVideoJobs =
    detail?.video_jobs.filter(
      (job) => isVideoJob(job) && job.status === "SUCCEEDED" && jobVideoShots(job).length > 0,
    ) ?? [];
  const successfulVideoJob =
    successfulVideoJobs.find((job) => job.id === selectedFinalVideoJobId) ?? null;
  const videoDisplayJob = selectVideoDisplayJob({
    selectedJob: successfulVideoJob,
    latestJob: isVideoJob(latestVisibleJob) ? latestVisibleJob : null,
  });
  const generatedVideoShots = jobVideoShots(videoDisplayJob);
  const videoGenerationInProgress =
    isVideoJob(activeJob) &&
    (activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING");
  const audioDisplayJob =
    (isRealAudioJob(latestVisibleJob) ? latestVisibleJob : null) ??
    (isRealAudioJob(exportJob) ? exportJob : null) ??
    detail?.recent_jobs.find((job) => isRealAudioJob(job)) ??
    null;
  const generatedAudioShots = jobAudioShots(audioDisplayJob);
  const audioGenerationInProgress =
    isRealAudioJob(activeJob) &&
    (activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING");
  const audioTimingShots = jobTimingPlan(audioDisplayJob)?.shots ?? [];
  const audioExtendedShotCount = audioTimingShots.filter(
    (timing) => (numberValue(timing.extended_by_seconds) ?? 0) > 0.0005,
  ).length;
  const exportAudioSpeaker = jobAudioSpeaker(exportJob);
  const exportAudioLanguage = jobAudioLanguage(exportJob);
  const exportSourceDuration = jobSourcePlannedDuration(exportJob);
  const exportRenderedDuration = jobRenderedPlannedDuration(exportJob);
  const exportAudioExtension = jobAudioExtensionSeconds(exportJob);
  const exportAudioGenerationSeconds = jobAudioGenerationSeconds(exportJob);
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
  const compositionAudio = imageCompositionPlan?.audio ?? videoCompositionPlan?.audio ?? null;
  const currentCompositionMode =
    imageCompositionPlan?.freshness === "CURRENT"
      ? "关键帧版"
      : videoCompositionPlan?.freshness === "CURRENT"
        ? "动态镜头版"
        : null;
  const dynamicPlanNeedsRecompose =
    (videoCompositionPlan?.available_video_shot_count ?? 0) > 0 &&
    videoCompositionPlan?.freshness !== "CURRENT";
  const filteredProjects = useMemo(() => {
    const query = projectSearch.trim().toLocaleLowerCase();
    return projects
      .filter((project) => {
        const signal = projectSignals[project.id];
        const matchesQuery = !query || project.title.toLocaleLowerCase().includes(query);
        const matchesFilter =
          projectFilter === "all" ||
          (projectFilter === "real" && signal?.realChain === true) ||
          (projectFilter === "mock" && signal !== undefined && !signal.realChain);
        return matchesQuery && matchesFilter;
      })
      .sort(
        (left, right) =>
          projectSortScore(left, projectSignals[left.id], selectedId) -
          projectSortScore(right, projectSignals[right.id], selectedId),
      );
  }, [projectFilter, projectSearch, projectSignals, projects, selectedId]);
  const projectTotalPages = Math.max(1, Math.ceil(filteredProjects.length / PROJECTS_PER_PAGE));
  const currentProjectPage = Math.min(projectPage, projectTotalPages);
  const visibleProjects = filteredProjects.slice(
    (currentProjectPage - 1) * PROJECTS_PER_PAGE,
    currentProjectPage * PROJECTS_PER_PAGE,
  );
  const selectedProjectVisible = selectedId
    ? filteredProjects.some((project) => project.id === selectedId)
    : false;
  const projectFilterChangedRef = useRef(false);
  useEffect(() => {
    projectFilterChangedRef.current = true;
    setProjectPage(1);
  }, [projectFilter, projectSearch]);
  useEffect(() => {
    setProjectPage((current) => Math.min(Math.max(1, current), projectTotalPages));
  }, [projectTotalPages]);
  useEffect(() => {
    if (projectFilterChangedRef.current) {
      projectFilterChangedRef.current = false;
      return;
    }
    if (!selectedId) return;
    const selectedIndex = filteredProjects.findIndex((project) => project.id === selectedId);
    if (selectedIndex >= 0) {
      setProjectPage(Math.floor(selectedIndex / PROJECTS_PER_PAGE) + 1);
    }
  }, [filteredProjects, selectedId]);
  const demoScriptState =
    scriptJob?.status === "SUCCEEDED"
      ? isRealScriptJob(scriptJob)
        ? "REAL SCRIPT"
        : "MOCK SCRIPT"
      : "未生成";
  const demoImageState =
    sourceImageJob?.status === "SUCCEEDED"
      ? "REAL IMAGE"
      : "未生成";
  const demoAudioIsReal =
    exportIsRealAudio ||
    (audioDisplayJob?.status === "SUCCEEDED" &&
      (audioDisplayJob.result_json?.mock_audio_fallback === false ||
        jobAudioProvider(audioDisplayJob) === REAL_AUDIO_PROVIDER_ID));
  const demoAudioState =
    demoAudioIsReal
      ? "REAL TTS"
      : audioDisplayJob?.status === "SUCCEEDED"
        ? "MOCK AUDIO"
        : "未生成";
  const demoVideoState = media ? "FINAL VIDEO READY" : "未生成";

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
          <div className="topbar-status">
            <a className="model-status-link" href="#project-section">
              模型状态
              <span>{providerChecking ? "检查中" : providerError ? "需检查" : "查看"}</span>
            </a>
            <div className={`health-pill ${healthError ? "is-offline" : ""}`}>
              <span className="health-dot" />
              {healthError ? "后端未连接" : displayHealth(health)}
            </div>
          </div>
        </nav>

        <div className="hero-copy" id="top">
          <div className="hero-heading-row">
            <div>
              <p className="kicker">AI ANIMATION WORKBENCH · M6 DEMO</p>
              <h1>纸鹤工坊</h1>
              <p className="hero-subtitle">把故事变成可审阅、可追溯、可播放的动漫短片。</p>
            </div>
            <div className="hero-project-context" aria-label="当前项目">
              <span className="context-label">当前项目</span>
              <strong>{selectedProject?.title ?? "尚未选择项目"}</strong>
              <span>{selectedProject ? `${scriptShots.length || "—"} 个镜头 · ${currentStage === "result" ? "成片已就绪" : "制作进行中"}` : "从一个故事开始"}</span>
            </div>
          </div>
          <p>
            一条清晰的制作链路：Qwen 生成结构化剧本，Animagine 生成关键帧，Qwen3-TTS 生成旁白，FFmpeg 完成运镜、字幕与 MP4 合成。
          </p>
          <div className="hero-actions">
            <a className="button button-primary" href="#workspace">开始创建 <span aria-hidden="true">↓</span></a>
            <span className="chain-summary" aria-label="模型链路"><b>Qwen</b><i>→</i><b>Animagine</b><i>→</i><b>Qwen3-TTS</b><i>→</i><b>FFmpeg</b></span>
          </div>
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
            {notice.action && (notice.action === "composition" || availableStages[notice.action]) && (
              <button
                className="notice-action"
                type="button"
                onClick={() => {
                  if (notice.action === "composition") scrollToComposition();
                  else if (notice.action) scrollToSection(notice.action);
                }}
              >
                {notice.actionLabel ?? "查看成片"}
              </button>
            )}
          </div>
        )}

        <section className="demo-status-summary" aria-labelledby="demo-status-title">
          <div className="demo-status-heading">
            <div>
              <p className="eyebrow">演示状态</p>
              <h2 id="demo-status-title">真实演示链路</h2>
            </div>
            <span className="demo-project-label">
              {selectedProject?.title ?? "尚未选择项目"}
            </span>
          </div>
          <div className="demo-chain" aria-label="真实模型链路">
            <span>Qwen3-4B</span><i aria-hidden="true">↓</i>
            <span>Animagine XL 4.0</span><i aria-hidden="true">↓</i>
            <span>Qwen3-TTS Serena</span><i aria-hidden="true">↓</i>
            <span>FFmpeg</span>
          </div>
          <div className="demo-status-grid">
            {[
              ["Script", demoScriptState],
              ["Image", demoImageState],
              ["Audio", demoAudioState],
              ["Final video", demoVideoState],
            ].map(([label, state]) => (
              <div className="demo-status-item" key={label}>
                <small>{label}</small>
                <strong className={state.startsWith("REAL") || state === "FINAL VIDEO READY" ? "is-ready" : ""}>
                  {state}
                </strong>
              </div>
            ))}
          </div>
          {media && (
            <button className="demo-result-link" type="button" onClick={() => scrollToSection("result")}>
              查看最终成片 →
            </button>
          )}
          <p className="demo-limit-note">
            当前版本：静态动漫关键帧 + FFmpeg 镜头运动；真实本地模型按阶段交接 GPU 运行。
          </p>
        </section>

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
              <div className="project-list-toolbar">
                <label className="project-search-label">
                  <span>搜索项目</span>
                  <input
                    type="search"
                    value={projectSearch}
                    onChange={(event) => setProjectSearch(event.target.value)}
                    placeholder="输入项目名称"
                    aria-label="搜索项目名称"
                  />
                </label>
                <div className="project-filters" role="group" aria-label="项目筛选">
                  {([
                    ["all", "全部"],
                    ["real", "真实成片"],
                    ["mock", "Mock / 测试"],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      className={projectFilter === value ? "is-active" : ""}
                      type="button"
                      aria-pressed={projectFilter === value}
                      onClick={() => setProjectFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <p className="project-list-hint">真实链路和最终成片优先显示</p>
              </div>
              {projects.length === 0 ? (
                <div className="empty-state">还没有项目，请先创建一个故事。</div>
              ) : filteredProjects.length === 0 ? (
                <div className="empty-state">没有匹配的项目，请调整搜索或筛选条件。</div>
              ) : (
                visibleProjects.map((project) => (
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
                          <small>
                            {projectStatus(project)} · {projectSignalLabel(projectSignals[project.id])}
                          </small>
                        </span>
                        <span className="project-badges" aria-label="项目类型">
                          {projectSignals[project.id]?.realChain && (
                            <em className="project-badge is-real">真实链路</em>
                          )}
                          {projectSignals[project.id]?.realChain && (
                            <em className="project-badge is-export">真实成片</em>
                          )}
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
              {selectedId && !selectedProjectVisible && projects.some((project) => project.id === selectedId) && (
                <p className="project-filtered-note" role="status">
                  当前项目已保留，但不符合当前搜索或筛选条件。
                </p>
              )}
              {projectTotalPages > 1 && (
                <nav className="project-pagination" aria-label="项目列表分页">
                  <button
                    type="button"
                    disabled={currentProjectPage === 1}
                    onClick={() => setProjectPage((current) => Math.max(1, current - 1))}
                  >
                    上一页
                  </button>
                  <span aria-live="polite">第 {currentProjectPage} / {projectTotalPages} 页</span>
                  <button
                    type="button"
                    disabled={currentProjectPage === projectTotalPages}
                    onClick={() =>
                      setProjectPage((current) => Math.min(projectTotalPages, current + 1))
                    }
                  >
                    下一页
                  </button>
                </nav>
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
                            ? "本地 Qwen 配置不可用"
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
                      setAudioRequestError(null);
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
                          setAudioRequestError(null);
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
                    <details
                      className="technical-details job-technical-details"
                      open={generationInProgress || latestVisibleJob.status === "FAILED"}
                    >
                      <summary>
                        <strong>运行与追溯</strong>
                        <span>{latestVisibleJob.status} · {latestVisibleJob.progress}%</span>
                      </summary>
                      <JobPanel
                        job={latestVisibleJob}
                        retrying={busy === "retry"}
                        onRetry={() => retryGeneration(latestVisibleJob)}
                        onViewResult={() => scrollToSection("result")}
                        configuredModelId={currentJobProviderDescriptor?.model_id}
                        configuredImageModelId={realImageProviderDescriptor?.model_id}
                        configuredAudioModelId={realAudioProviderDescriptor?.model_id}
                        actualShotCount={
                          latestVisibleJob.status === "SUCCEEDED" ? scriptShots.length : undefined
                        }
                      />
                    </details>
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

            <StageAccordion
              title="AI 剧本"
              summary={`${jobScriptProvider(scriptJob) ?? "Script Provider 未报告"} · ${scriptShots.length} 个镜头`}
              status={scriptJob?.status === "SUCCEEDED" ? "已生成" : "未生成"}
              open={currentStage === "project" || !media}
            >
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
            </StageAccordion>

            <StageAccordion
              title="动漫画面"
              summary={`${realImageProviderDescriptor?.display_name ?? "Animagine XL 4.0"} · ${jobImageCompletedCount(sourceImageJob ?? imageDisplayJob)}/${jobImageTotalCount(sourceImageJob ?? imageDisplayJob, scriptShots.length)} 张关键帧`}
              status={exportIsRealImage ? "真实成片" : sourceImageJob?.status === "SUCCEEDED" ? "已生成" : "未开始"}
              open={currentStage === "project" && !exportIsRealImage}
            >
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
                    (backgroundAudioEnabled && !backgroundAudio) ||
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
            </StageAccordion>

            <StageAccordion
              title="动态视频（可选）"
              summary={
                videoMode === "keyframe_motion"
                  ? "不生成 AI 视频 · 沿用关键帧动效"
                  : `${selectedVideoProviderDescriptor?.display_name ?? "VideoProvider"} · ${generatedVideoShots.length}/${scriptShots.length} 个视频片段`
              }
              status={
                videoMode === "keyframe_motion"
                  ? "已跳过"
                  : videoDisplayJob?.status === "SUCCEEDED"
                    ? videoDisplayJob.result_json?.video_source_type === "REAL_CLOUD_MODEL"
                      ? "真实云视频已完成"
                      : "Mock 已完成"
                    : videoGenerationInProgress
                      ? "处理中"
                      : "可选"
              }
              open={videoMode !== "keyframe_motion" || videoGenerationInProgress}
            >
              <section className="real-image-control optional-video-control" aria-labelledby="optional-video-title">
                <div className="real-image-heading">
                  <div>
                    <p className="eyebrow">M8-A2 · OPTIONAL VIDEO PROVIDER</p>
                    <h3 id="optional-video-title">选择关键帧之后的可选动态视频阶段</h3>
                  </div>
                  <span className={`visual-source-badge ${videoMode === "cloud-wan-2.7" ? "is-real" : "is-mock"}`}>
                    {videoMode === "cloud-wan-2.7" ? "Cloud · Real AI Video" : "安全默认 / Mock"}
                  </span>
                </div>
                <fieldset className="motion-preset-selector" disabled={busy !== null || generationInProgress}>
                  <legend>视频阶段模式</legend>
                  <label className={videoMode === "keyframe_motion" ? "is-selected" : ""}>
                    <input
                      type="radio"
                      name="video-mode"
                      value="keyframe_motion"
                      checked={videoMode === "keyframe_motion"}
                      onChange={() => setVideoMode("keyframe_motion")}
                    />
                    <span>
                      <strong>不生成 AI 视频</strong>
                      <small>继续使用现有 PNG 关键帧与 FFmpeg 镜头动效</small>
                    </span>
                  </label>
                  <label className={videoMode === "mock-video" ? "is-selected" : ""}>
                    <input
                      type="radio"
                      name="video-mode"
                      value="mock-video"
                      checked={videoMode === "mock-video"}
                      onChange={() => setVideoMode("mock-video")}
                    />
                    <span>
                      <strong>MockVideoProvider</strong>
                      <small>由已有 PNG 确定性生成测试 MP4，不是真实 AI 视频</small>
                    </span>
                  </label>
                  <label className={videoMode === "cloud-wan-2.7" ? "is-selected" : ""}>
                    <input
                      type="radio"
                      name="video-mode"
                      value="cloud-wan-2.7"
                      checked={videoMode === "cloud-wan-2.7"}
                      onChange={() => setVideoMode("cloud-wan-2.7")}
                    />
                    <span>
                      <strong>Wan 2.7 Cloud · 阿里云百炼</strong>
                      <small>真实云端 Image-to-Video · 需要后端 API Key · 可能产生费用</small>
                    </span>
                  </label>
                </fieldset>
                <div className="real-image-provider-line">
                  <strong>{selectedVideoProviderDescriptor?.display_name ?? "不生成 AI 视频"}</strong>
                  <span>Provider ID：{videoMode === "keyframe_motion" ? "none" : videoMode}</span>
                  <span>
                    Source type：{videoMode === "cloud-wan-2.7" ? "REAL_CLOUD_MODEL" : videoMode === "mock-video" ? "MOCK" : "FFMPEG_KEYFRAME_MOTION"}
                  </span>
                  <span>最终成片：生成后需在“成片合成”中明确选择动态镜头版</span>
                  {selectedVideoProviderDescriptor?.detail && (
                    <span className="image-provider-detail">{selectedVideoProviderDescriptor.detail}</span>
                  )}
                </div>
                {videoMode !== "keyframe_motion" && (
                  <fieldset
                    className="video-target-selector"
                    disabled={busy !== null || generationInProgress}
                  >
                    <legend>本次生成镜头</legend>
                    {scriptShots.map((shot, index) => {
                      const shotId = shot.shot_id ?? shot.id;
                      if (!shotId) return null;
                      return (
                        <label key={shotId}>
                          <input
                            type="checkbox"
                            checked={targetVideoShotIds.includes(shotId)}
                            onChange={(event) => {
                              setTargetVideoShotIds((current) =>
                                event.target.checked
                                  ? [...new Set([...current, shotId])]
                                  : current.filter((item) => item !== shotId),
                              );
                            }}
                          />
                          <span>
                            镜头 {index + 1}
                            <small>
                              {selectedVideoImageAssets[shotId]
                                ? "当前显式关键帧"
                                : sourceImageJob
                                  ? "Image Job 关键帧"
                                  : "缺少首帧"}
                            </small>
                          </span>
                        </label>
                      );
                    })}
                  </fieldset>
                )}
                {videoMode !== "keyframe_motion" && !hasTargetVideoSource && (
                  <p className="provider-warning">
                    请至少选择一个有关键帧来源的目标镜头。
                  </p>
                )}
                {videoMode === "cloud-wan-2.7" && !hasExplicitTargetVideoSource && (
                  <p className="provider-warning" role="status">
                    付费 Wan 任务只接受目标镜头当前显式选择的关键帧，不会回退旧 Image Job。
                  </p>
                )}
                {videoMode === "cloud-wan-2.7" && cloudVideoProviderDescriptor?.configured !== true && (
                  <p className="provider-warning" role="status">
                    云 Provider 配置不完整。请在后端配置 DASHSCOPE_API_KEY 和 DASHSCOPE_WORKSPACE_ID；密钥不会进入浏览器。
                  </p>
                )}
                {videoMode === "cloud-wan-2.7" && cloudVideoProviderDescriptor?.configured === true && (
                  <p className="rights-notice">提交后会调用真实阿里云 Wan 2.7 服务，并可能产生云端费用。</p>
                )}
                {videoDisplayJob?.status === "FAILED" && (
                  <p className="provider-warning" role="alert">
                    动态视频生成失败：{videoDisplayJob.error_message ?? "请查看任务技术详情。"}
                  </p>
                )}
                <div className="real-image-actions">
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={() => void startVideoGeneration()}
                    disabled={
                      videoMode === "keyframe_motion" ||
                      effectiveTargetVideoShotIds.length === 0 ||
                      !hasTargetVideoSource ||
                      (videoMode === "cloud-wan-2.7" && !hasExplicitTargetVideoSource) ||
                      busy !== null ||
                      generationInProgress ||
                      selectedVideoProviderDescriptor?.available !== true
                    }
                  >
                    {busy === "video"
                      ? videoMode === "cloud-wan-2.7"
                        ? "正在提交真实云视频任务…"
                        : "正在提交 Mock 视频任务…"
                      : videoGenerationInProgress
                        ? `正在生成视频片段 ${activeJob?.progress ?? 0}%`
                        : videoMode === "cloud-wan-2.7"
                          ? "生成 Wan 2.7 云视频"
                          : "生成 Mock 动态视频"}
                  </button>
                </div>
                {generatedVideoShots.length > 0 && (
                  <div className="optional-video-grid">
                    {generatedVideoShots.map((shot) => {
                      const videoUrl = mediaAssetUrl(shot.video_url);
                      return (
                        <article key={shot.shot_id}>
                          <strong>镜头 {shot.shot_index ?? shot.shot_id}</strong>
                          {videoUrl ? (
                            <video controls preload="metadata" src={videoUrl} />
                          ) : (
                            <p role="alert">视频媒体 URL 不可用。</p>
                          )}
                          <small>
                            {shot.source_type === "REAL_CLOUD_MODEL" ? "REAL CLOUD AI" : "MOCK"} · {shot.duration_seconds?.toFixed(1) ?? "—"} 秒
                          </small>
                        </article>
                      );
                    })}
                  </div>
                )}
              </section>
            </StageAccordion>

            <StageAccordion
              title="配音与成片"
              summary={`${audioSpeaker} · ${exportAudioGenerationSeconds?.toFixed(1) ?? "—"} 秒 TTS`}
              status={exportIsRealAudio ? "真实旁白已完成" : audioDisplayJob?.status === "SUCCEEDED" ? "已生成" : "未开始"}
              open={currentStage === "project" && !exportIsRealAudio}
            >
            <section className="real-audio-control" aria-labelledby="real-audio-title">
              <div className="real-image-heading">
                <div>
                  <p className="eyebrow">AUDIO PROVIDER</p>
                  <h3 id="real-audio-title">使用当前剧本生成 AI 旁白</h3>
                </div>
                <span className={`audio-source-badge ${exportIsRealAudio ? "is-real" : "is-mock"}`}>
                  当前成片：{exportIsRealAudio ? "真实 AI 旁白" : "Mock 音频"}
                </span>
              </div>
              <div className="real-image-provider-line">
                <strong>
                  当前 Audio Provider：
                  {realAudioProviderDescriptor?.display_name ?? "Qwen3-TTS 0.6B CustomVoice"}
                </strong>
                <span>Provider ID：{REAL_AUDIO_PROVIDER_ID}</span>
                <span>语言：Chinese</span>
                <span>
                  状态：
                  {realAudioProviderDescriptor
                    ? realAudioProviderDescriptor.available
                      ? "可启动"
                      : realAudioProviderDescriptor.configured
                        ? "等待 GPU 交接"
                        : "配置不完整"
                    : "等待后端报告"}
                </span>
                {realAudioProviderDescriptor?.detail && (
                  <span className="image-provider-detail">{realAudioProviderDescriptor.detail}</span>
                )}
              </div>
              <section className="media-polish-controls" aria-labelledby="media-polish-title">
                <div className="media-polish-heading">
                  <div>
                    <p className="eyebrow">成片设置</p>
                    <h4 id="media-polish-title">镜头运动与背景音</h4>
                  </div>
                  <span>新任务将冻结这些设置，重试沿用原快照</span>
                </div>
                <fieldset className="motion-preset-selector" disabled={busy !== null || generationInProgress}>
                  <legend>镜头运动模式</legend>
                  {([
                    ["static", "静态", "无平移缩放，仅淡入淡出"],
                    ["gentle_zoom", "轻柔缩放", "中心缓慢缩放，默认"],
                    ["cinematic_pan", "电影平移", "小幅横向移动"],
                  ] as const).map(([value, label, description]) => (
                    <label className={motionPreset === value ? "is-selected" : ""} key={value}>
                      <input
                        type="radio"
                        name="motion-preset"
                        value={value}
                        checked={motionPreset === value}
                        onChange={() => setMotionPreset(value)}
                      />
                      <span><strong>{label}</strong><small>{description}</small></span>
                    </label>
                  ))}
                </fieldset>
                <div className="background-audio-control">
                  <div className="background-audio-row">
                    <label className="toggle-control">
                      <input
                        type="checkbox"
                        checked={backgroundAudioEnabled}
                        disabled={!backgroundAudio || busy !== null || generationInProgress}
                        onChange={(event) => setBackgroundAudioEnabled(event.target.checked)}
                      />
                      <span>使用背景音</span>
                    </label>
                    <label className="button button-ghost background-upload-button">
                      {backgroundAudio ? "替换音频" : "上传音频"}
                      <input
                        className="visually-hidden"
                        type="file"
                        accept=".wav,.mp3,.m4a,.ogg,audio/wav,audio/mpeg,audio/mp4,audio/ogg"
                        disabled={busy !== null || generationInProgress}
                        onChange={(event) => void uploadProjectBackgroundAudio(event)}
                        aria-label="上传背景音乐或环境音"
                      />
                    </label>
                    {backgroundAudio && (
                      <button
                        className="button button-ghost"
                        type="button"
                        disabled={busy !== null || generationInProgress}
                        onClick={() => void removeProjectBackgroundAudio()}
                      >
                        {busy === "background-delete" ? "正在删除…" : "删除"}
                      </button>
                    )}
                  </div>
                  {backgroundLoading ? (
                    <p className="background-audio-meta">正在读取项目背景音…</p>
                  ) : backgroundAudio ? (
                    <p className="background-audio-meta">
                      <strong>{backgroundAudio.original_filename}</strong>
                      <span>{backgroundAudio.duration_seconds.toFixed(2)} 秒 · {backgroundAudio.format.toUpperCase()} · 用户上传</span>
                    </p>
                  ) : (
                    <p className="background-audio-meta">未上传背景音，成片保持当前音频行为。</p>
                  )}
                  <label className="background-volume-control">
                    <span>背景音量 <output>{Math.round(backgroundVolume * 100)}%</output></span>
                    <input
                      type="range"
                      min="0.02"
                      max="0.35"
                      step="0.01"
                      value={backgroundVolume}
                      disabled={!backgroundAudioEnabled || busy !== null || generationInProgress}
                      onChange={(event) => setBackgroundVolume(Number(event.target.value))}
                    />
                  </label>
                  <p className="rights-notice">仅上传您拥有使用权的 WAV、MP3、M4A 或 OGG 文件，最大 20MB。平台不声明音频版权归属。</p>
                </div>
              </section>
              <fieldset className="speaker-selector" disabled={busy !== null || generationInProgress}>
                <legend>整段成片使用同一个旁白音色</legend>
                <label className={audioSpeaker === "Serena" ? "is-selected" : ""}>
                  <input
                    type="radio"
                    name="audio-speaker"
                    value="Serena"
                    checked={audioSpeaker === "Serena"}
                    onChange={() => {
                      setAudioSpeaker("Serena");
                      setAudioRequestError(null);
                    }}
                  />
                  <span><strong>Serena（默认）</strong><small>年轻、温暖、节奏较快</small></span>
                </label>
                <label className={audioSpeaker === "Vivian" ? "is-selected" : ""}>
                  <input
                    type="radio"
                    name="audio-speaker"
                    value="Vivian"
                    checked={audioSpeaker === "Vivian"}
                    onChange={() => {
                      setAudioSpeaker("Vivian");
                      setAudioRequestError(null);
                    }}
                  />
                  <span><strong>Vivian</strong><small>稳重、明亮、节奏较慢</small></span>
                </label>
              </fieldset>
              <div className={`gpu-handoff-notice ${gpuHandoffRequired ? "needs-action" : "is-ready"}`} role="status">
                <strong>GPU 分阶段运行</strong>
                <span>
                  {gpuHandoffRequired
                    ? "检测到文本 Qwen、ComfyUI 或其他冲突状态。请自行停止相关模型并释放 GPU；平台不会结束外部进程。"
                    : "后端会在入队和执行前复查 8081、8188 与 GPU；TTS 只在独立有界子进程中顺序生成，不与 Qwen 或 ComfyUI 同驻。"}
                </span>
              </div>
              <dl className="real-image-facts">
                <div><dt>视觉追溯</dt><dd>{sourceImageJob ? `兼容旧链路 Job ${sourceImageJob.id.slice(0, 8)}` : "由后续 Composition 选择决定"}</dd></div>
                <div><dt>本次音色</dt><dd>{audioSpeaker}</dd></div>
                <div><dt>旁白进度</dt><dd>{audioDisplayJob ? `${jobAudioCompletedCount(audioDisplayJob)}/${jobAudioTotalCount(audioDisplayJob, scriptShots.length)}` : `0/${scriptShots.length}`}</dd></div>
                <div><dt>延长镜头</dt><dd>{audioDisplayJob ? `${audioExtendedShotCount} 个` : "生成后报告"}</dd></div>
                <div><dt>TTS 总耗时</dt><dd>{jobAudioGenerationSeconds(audioDisplayJob)?.toFixed(1) ?? "生成后报告"}{jobAudioGenerationSeconds(audioDisplayJob) !== null ? " 秒" : ""}</dd></div>
              </dl>
              {!scriptJob && (
                <p className="provider-warning">请先完成并保留一个与当前 ScriptV1 对应的成功剧本 Job。</p>
              )}
              {scriptJob && scriptShots.some((shot) => !shot.narration?.trim()) && (
                <p className="provider-warning">当前剧本存在空旁白镜头，无法提交真实 TTS。</p>
              )}
              {!realAudioProviderConfigured && (
                <p className="provider-warning">真实 Audio Provider 尚未配置完整，请检查独立 Qwen3-TTS 环境与固定模型文件。</p>
              )}
              <div className="real-image-actions">
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => void startRealAudioGeneration()}
                  disabled={
                    busy !== null ||
                    generationInProgress ||
                    !scriptJob ||
                    scriptShots.some((shot) => !shot.narration?.trim()) ||
                    gpuHandoffRequired ||
                    (backgroundAudioEnabled && !backgroundAudio) ||
                    !realAudioProviderConfigured
                  }
                >
                  {busy === "real-audio"
                    ? "正在提交真实旁白任务…"
                    : audioGenerationInProgress
                      ? `正在生成旁白 ${jobAudioCompletedCount(activeJob)}/${jobAudioTotalCount(activeJob, scriptShots.length)}`
                      : gpuHandoffRequired
                        ? "请先释放 GPU"
                        : "生成 AI 旁白"}
                </button>
                <button
                  className="button button-ghost"
                  type="button"
                  onClick={scrollToComposition}
                  disabled={busy !== null || generationInProgress}
                  aria-describedby="media-rerender-note"
                >
                  前往成片合成
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
              <p id="media-rerender-note" className="media-rerender-note">
                请在下方成片合成区域选择 IMAGE_ONLY 或 VIDEO_PREFERRED 后提交；这里不会创建任务。
              </p>
              {audioRequestError && (
                <FailureCard
                  detail={audioRequestError}
                  retrying={busy === "real-audio"}
                  onRetry={() => void startRealAudioGeneration()}
                />
              )}
            </section>
            </StageAccordion>

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
                  const availableImageAssets = imageAssets.filter(
                    (asset) => asset.shot_id === sourceShotId,
                  );
                  const selectedImageAsset = availableImageAssets.find(
                    (asset) => asset.asset_id === selectedVideoImageAssets[sourceShotId ?? ""],
                  );
                  const externalPrompt = sourceShotId
                    ? externalPrompts[sourceShotId]
                    : undefined;
                  const planningDraft = sourceShotId
                    ? shotPlanningDrafts[sourceShotId]
                    : undefined;
                  const externalSourceType = sourceShotId
                    ? externalSourceTypes[sourceShotId] ?? "AI_GENERATED"
                    : "AI_GENERATED";
                  const generatedAudio = generatedAudioShots.find(
                    (audio) =>
                      (sourceShotId && audio.shot_id === sourceShotId) ||
                      numberValue(audio.shot_index) === sequence,
                  );
                  const timing = audioTimingShots.find(
                    (item) =>
                      (sourceShotId && item.shot_id === sourceShotId) ||
                      numberValue(item.shot_index) === sequence,
                  );
                  const audioDuration =
                    numberValue(generatedAudio?.audio_duration_seconds) ??
                    numberValue(generatedAudio?.duration_seconds) ??
                    numberValue(timing?.audio_duration_seconds) ??
                    numberValue(timing?.audio_duration);
                  const sourceTimingDuration =
                    numberValue(timing?.source_shot_duration_seconds) ??
                    numberValue(timing?.source_duration_seconds) ??
                    numberValue(timing?.source_shot_duration) ??
                    shot.duration_seconds;
                  const renderedTimingDuration =
                    numberValue(timing?.rendered_shot_duration_seconds) ??
                    numberValue(timing?.rendered_duration_seconds) ??
                    numberValue(timing?.rendered_shot_duration);
                  const extendedBy = numberValue(timing?.extended_by_seconds) ?? 0;
                  const hasRealAudio =
                    Boolean(generatedAudio) &&
                    generatedAudio?.provider_id === REAL_AUDIO_PROVIDER_ID &&
                    (generatedAudio?.status === "SUCCEEDED" ||
                      generatedAudio?.status === "REUSED") &&
                    Boolean(textValue(generatedAudio?.audio_sha256));
                  const thumbnailUrl = imageAssetUrl(
                    selectedImageAsset?.image_url ??
                      textValue(generatedImage?.image_url) ??
                      undefined,
                  );
                  const imageStatus = selectedImageAsset
                    ? "SUCCEEDED"
                    : textValue(generatedImage?.status) ?? (thumbnailUrl ? "SUCCEEDED" : "PENDING");
                  const hasRealImage =
                    Boolean(thumbnailUrl) &&
                    (Boolean(selectedImageAsset) ||
                      generatedImage?.provider_id === REAL_IMAGE_PROVIDER_ID ||
                      isRealImageJob(imageDisplayJob));
                  const imageSourceLabel = selectedImageAsset
                    ? imageAssetLabel(selectedImageAsset)
                    : hasRealImage
                      ? "Animagine XL 4.0"
                      : isRealImageJob(imageDisplayJob)
                        ? "等待真实图片"
                        : "Mock 视觉";
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
                  const displayedImageStatus = selectedImageAsset
                    ? "外部素材已校验"
                    : isRealImageJob(imageDisplayJob)
                      ? imageStatusLabel
                      : "Mock 已准备";
                  return (
                    <article
                      className={`shot-card ${hasRealImage ? "has-real-image" : ""} ${hasRealAudio ? "has-real-audio" : ""}`}
                      key={shot.id ?? shot.shot_id ?? `${shot.title}-${index}`}
                    >
                      <div className="shot-art" data-shot={(index % 4) + 1}>
                        {hasRealImage && thumbnailUrl ? (
                          <ShotImage src={thumbnailUrl} sequence={sequence} title={shot.title} />
                        ) : (
                          <i />
                        )}
                        <span className="shot-number">{String(sequence).padStart(2, "0")}</span>
                        <small className={`shot-image-status status-${imageStatus.toLowerCase()}`}>
                            {selectedImageAsset?.source_type === "EXTERNAL_IMPORT"
                              ? "外部素材"
                              : hasRealImage
                                ? "真实模型"
                                : isRealImageJob(imageDisplayJob)
                                  ? imageStatusLabel
                                  : "Mock 视觉"}
                        </small>
                      </div>
                      <div className="shot-copy">
                        <div className="shot-provider-row">
                          <p className="eyebrow">{shot.duration_seconds}s · 图像 {displayedImageStatus}</p>
                          <span className={`visual-source-badge ${hasRealImage ? "is-real" : "is-mock"}`}>
                            {imageSourceLabel}
                          </span>
                        </div>
                        <h3>{shot.title}</h3>
                        <p>{shot.visual_description}</p>
                        <blockquote>“{shot.narration}”</blockquote>
                        <div className={`shot-audio-summary ${hasRealAudio ? "is-real" : "is-mock"}`}>
                          <span>{hasRealAudio ? `真实旁白 · ${jobAudioSpeaker(audioDisplayJob) ?? "未报告音色"}` : "音频 · Mock 或待生成"}</span>
                          {audioDuration !== null && <span>WAV {audioDuration.toFixed(2)} 秒</span>}
                          {renderedTimingDuration !== null && (
                            <span>
                              镜头 {sourceTimingDuration.toFixed(2)} → {renderedTimingDuration.toFixed(2)} 秒
                              {extendedBy > 0.0005 ? `（延长 ${extendedBy.toFixed(2)} 秒）` : ""}
                            </span>
                          )}
                        </div>
                        {hasRealAudio && (
                          <ShotAudioPlayer
                            src={audioAssetUrl(generatedAudio?.audio_url)}
                            sequence={sequence}
                            missingReason={generatedAudio?.audio_url_error?.summary}
                          />
                        )}
                        {sourceShotId && (
                          <section className="external-image-bridge" aria-label={`镜头 ${sequence} 外部关键帧`}>
                            <details className="visual-planning-inspector">
                              <summary>查看视觉规划</summary>
                              <div className="planning-layer">
                                <strong>① 原始故事</strong>
                                <p>{detail?.project.story}</p>
                              </div>
                              <div className="planning-layer">
                                <strong>② 当前结构化镜头</strong>
                                <dl>
                                  <div><dt>标题</dt><dd>{shot.title}</dd></div>
                                  <div><dt>角色</dt><dd>{scriptCharacters.filter((item) => !shot.character_ids || shot.character_ids.includes(item.id)).map((item) => item.name).join("、") || "未提供"}</dd></div>
                                  <div><dt>场景</dt><dd>{scriptScenes.find((item) => item.id === shot.scene_id)?.name ?? "见视觉描述"}</dd></div>
                                  <div><dt>旁白 / 时长</dt><dd>{shot.narration} · {shot.duration_seconds}s</dd></div>
                                  <div><dt>Visual</dt><dd>{shot.visual_description}</dd></div>
                                  <div><dt>Camera</dt><dd>{shot.camera ?? shot.camera_motion ?? "未提供"}</dd></div>
                                </dl>
                              </div>
                              {planningDraft && (
                                <div className="planning-layer planning-override">
                                  <strong>③ 制作层校正（不覆盖原始 ScriptV1）</strong>
                                  <label>
                                    静态首帧描述
                                    <textarea
                                      rows={4}
                                      value={planningDraft.keyframe}
                                      onChange={(event) => setShotPlanningDrafts((current) => ({
                                        ...current,
                                        [sourceShotId]: { ...planningDraft, keyframe: event.target.value },
                                      }))}
                                    />
                                  </label>
                                  <label>
                                    后续运动描述
                                    <textarea
                                      rows={4}
                                      value={planningDraft.motion}
                                      onChange={(event) => setShotPlanningDrafts((current) => ({
                                        ...current,
                                        [sourceShotId]: { ...planningDraft, motion: event.target.value },
                                      }))}
                                    />
                                  </label>
                                  <div className="planning-override-actions">
                                    <button
                                      className="button button-primary button-small"
                                      type="button"
                                      disabled={busy !== null}
                                      onClick={() => void saveShotPlanning(sourceShotId)}
                                    >
                                      {busy === `shot-planning-${sourceShotId}` ? "保存中…" : "保存制作校正"}
                                    </button>
                                    <button
                                      className="button button-ghost button-small"
                                      type="button"
                                      disabled={busy !== null}
                                      onClick={() => void saveShotPlanning(sourceShotId, true)}
                                    >
                                      恢复原始规划
                                    </button>
                                  </div>
                                </div>
                              )}
                              <div className="planning-layer">
                                <strong>④ 外部生成提示词</strong>
                                {externalPrompt ? (
                                  <>
                                    <small>{externalPrompt.adapter} · selected Shot {externalPrompt.shot_id}</small>
                                    <pre>{externalPrompt.prompt}</pre>
                                    <button
                                      className="button button-ghost button-small"
                                      type="button"
                                      onClick={() => void copyExternalPrompt(sourceShotId)}
                                    >
                                      复制外部生成提示词
                                    </button>
                                  </>
                                ) : (
                                  <p>当前镜头尚无可导出的结构化提示词。</p>
                                )}
                              </div>
                            </details>

                            <div className="external-image-controls">
                              <label>
                                素材来源
                                <select
                                  value={externalSourceType}
                                  onChange={(event) =>
                                    setExternalSourceTypes((current) => ({
                                      ...current,
                                      [sourceShotId]: event.target.value as ExternalImageSourceType,
                                    }))
                                  }
                                >
                                  <option value="AI_GENERATED">外部 AI 生成</option>
                                  <option value="HUMAN_CREATED">人工制作</option>
                                  <option value="OTHER">其他素材</option>
                                </select>
                              </label>
                              {externalSourceType === "AI_GENERATED" && (
                                <label>
                                  服务 / 模型来源（仅作来源提示）
                                  <input
                                    value={externalProviderHints[sourceShotId] ?? "ChatGPT Images"}
                                    onChange={(event) =>
                                      setExternalProviderHints((current) => ({
                                        ...current,
                                        [sourceShotId]: event.target.value,
                                      }))
                                    }
                                    placeholder="ChatGPT Images 或其他"
                                  />
                                </label>
                              )}
                              <label className="button button-ghost external-image-upload">
                                {busy === `external-image-${sourceShotId}`
                                  ? "正在校验并导入…"
                                  : "导入 / 替换关键帧"}
                                <input
                                  type="file"
                                  accept="image/png,image/jpeg,.png,.jpg,.jpeg"
                                  disabled={busy !== null || generationInProgress}
                                  onChange={(event) => void importExternalKeyframe(sourceShotId, event)}
                                />
                              </label>
                              <label>
                                视频首帧
                                <select
                                  value={selectedVideoImageAssets[sourceShotId] ?? ""}
                                  onChange={(event) =>
                                    void persistImageSelection(sourceShotId, event.target.value)
                                  }
                                >
                                  <option value="">
                                    {sourceImageJob
                                      ? "Image Job 默认关键帧（向后兼容）"
                                      : "请选择一个关键帧资产"}
                                  </option>
                                  {availableImageAssets.map((asset) => (
                                    <option key={asset.asset_id} value={asset.asset_id}>
                                      {imageAssetLabel(asset)} · {asset.width ?? "?"}×{asset.height ?? "?"}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <small>
                                {selectedImageAsset
                                  ? `当前选中：${imageAssetLabel(selectedImageAsset)} · ${selectedImageAsset.sha256.slice(0, 12)}…`
                                  : "当前使用来源 Image Job 的关键帧；选择资产后将显式传给 VideoProvider。"}
                              </small>
                            </div>
                          </section>
                        )}
                        <details className="technical-details shot-technical-details">
                          <summary>技术详情</summary>
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
                          {generatedAudio && (
                            <div>
                              <dt>真实旁白追溯</dt>
                              <dd>
                                {numberValue(generatedAudio.generation_seconds)?.toFixed(1) ?? "—"} 秒生成 · RTF {numberValue(generatedAudio.real_time_factor)?.toFixed(2) ?? "—"}
                              </dd>
                            </div>
                          )}
                          </dl>
                        </details>
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
                  <p>
                    {currentCompositionMode
                      ? `镜头已准备完成，当前${currentCompositionMode}成片已是最新。`
                      : "已有成片，但素材已更新，需要重新合成。"}
                  </p>
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

        {detail && scriptShots.length > 0 && (
          <section
            className="section composition-section"
            id="composition-section"
            ref={compositionSectionRef}
            tabIndex={-1}
            aria-labelledby="composition-title"
          >
            <div className="section-heading compact">
              <span className="section-number">04</span>
              <div>
                <p className="eyebrow">成片合成</p>
                <h2 id="composition-title">选择本次成片的视觉来源</h2>
              </div>
            </div>
            <div className="composition-summary-grid">
              <div><small>当前关键帧</small><strong>{imageCompositionPlan?.available_image_shot_count ?? 0} 个镜头</strong></div>
              <div><small>当前动态镜头</small><strong>{videoCompositionPlan?.available_video_shot_count ?? 0} 个镜头</strong></div>
              <div>
                <small>当前 Audio 来源</small>
                <strong>
                  {compositionAudio
                    ? `${compositionAudio.is_mock ? "MOCK" : "REAL"} · ${compositionAudio.provider}${compositionAudio.speaker ? ` · ${compositionAudio.speaker}` : ""}`
                    : "尚无兼容 Audio Job"}
                </strong>
                {compositionAudio && (
                  <small>Job {compositionAudio.job_id.slice(0, 8)} · 默认使用最新成功旁白</small>
                )}
              </div>
              <div>
                <small>当前成片状态</small>
                <strong className={currentCompositionMode ? "is-current" : "is-outdated"}>
                  {!detail.latest_export
                    ? "尚未生成"
                    : currentCompositionMode
                      ? `CURRENT · ${currentCompositionMode}`
                      : "OUTDATED · 需要重新合成"}
                </strong>
              </div>
            </div>
            {successfulVideoJobs.length > 0 && (
              <section className="video-version-panel" aria-labelledby="video-version-title">
                <div className="video-version-heading">
                  <div>
                    <p className="eyebrow">动态视频版本</p>
                    <h3 id="video-version-title">当前动态视频版本</h3>
                  </div>
                  <span>历史版本保留，仅显式选择决定当前版本</span>
                </div>
                <div className="video-version-list">
                  {successfulVideoJobs.map((job) => {
                    const shots = jobVideoShots(job);
                    const isCurrent = selectedFinalVideoJobId === job.id;
                    const presentation = describeVideoVersion({
                      jobId: job.id,
                      jobStatus: job.status,
                      selectedJobId: selectedFinalVideoJobId,
                      shots,
                      selectedImageAssetIds: selectedVideoImageAssets,
                    });
                    const isReal = job.result_json?.video_source_type === "REAL_CLOUD_MODEL";
                    return (
                      <article
                        className={`video-version-item ${isCurrent ? "is-current" : ""}`}
                        key={job.id}
                        aria-current={isCurrent ? "true" : undefined}
                      >
                        <div>
                          <strong>{isReal ? "Wan 2.7 Cloud" : "Mock Video"}</strong>
                          <span>{isReal ? "REAL" : "MOCK"} · Job {job.id.slice(0, 8)}</span>
                          <span>
                            {shots.length} 个动态镜头
                            {job.created_at ? ` · ${formatCheckedAt(job.created_at)}` : ""}
                          </span>
                        </div>
                        <div className="video-version-actions">
                          <div className="video-version-badges" aria-label="任务、采用状态与首帧兼容性">
                            <span className="video-version-state is-execution">
                              {presentation.executionStatus}
                            </span>
                            <span className={`video-version-state ${isCurrent ? "is-selected" : "is-history"}`}>
                              {presentation.selectionLabel}
                            </span>
                            <span className={`video-version-state is-lineage-${presentation.lineage.toLowerCase()}`}>
                              {presentation.lineageLabel}
                            </span>
                          </div>
                          <button
                            className="button button-ghost"
                            type="button"
                            aria-pressed={isCurrent}
                            disabled={isCurrent || busy !== null || generationInProgress}
                            onClick={() => void persistVideoSelection(job.id)}
                            title={
                              presentation.lineage === "STALE"
                                ? "可以设为当前用于追溯，但成片计划仍会拒绝使用首帧已变更的视频。"
                                : "将这个精确 Video Job 设为当前采用版本。"
                            }
                          >
                            {isCurrent
                              ? "当前版本"
                              : busy === `visual-selection-video-${job.id}`
                                ? "正在切换…"
                                : "设为当前"}
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </section>
            )}
            <div className="composition-plan-grid">
              {[
                {
                  mode: "IMAGE_ONLY" as const,
                  title: "关键帧成片计划",
                  description: "完全忽略所有 VIDEO_SHOT，使用当前关键帧与 FFmpeg 镜头运动。",
                  button: "使用关键帧合成成片",
                  plan: imageCompositionPlan,
                },
                {
                  mode: "VIDEO_PREFERRED" as const,
                  title: "动态镜头成片计划",
                  description: "优先使用已有动态镜头；缺少视频的镜头继续使用关键帧。",
                  button: "使用动态镜头合成成片",
                  plan: videoCompositionPlan,
                },
              ].map((item) => {
                const problem = item.plan?.problems[0];
                const warning = item.plan?.warnings[0];
                return (
                  <article className="composition-plan" key={item.mode}>
                    <header>
                      <div>
                        <h3>{item.title}</h3>
                        <p>{item.description}</p>
                      </div>
                      <span className={`composition-state is-${item.plan?.status.toLowerCase() ?? "loading"}`}>
                        {item.plan?.status ?? "读取中"}
                      </span>
                    </header>
                    <ul>
                      {item.plan?.shots.map((shot, index) => (
                        <li key={shot.shot_id}>
                          镜头{index + 1} · {shot.selected_type === "VIDEO_SHOT"
                            ? `${shot.is_mock ? "MOCK" : "REAL"} · ${shot.provider} Video${shot.source_job_id ? ` · Job ${shot.source_job_id.slice(0, 8)}` : ""}`
                            : `Image · ${shot.provider_hint ?? shot.provider}`}
                        </li>
                      ))}
                      {item.plan?.audio && (
                        <li>
                          音频 · {item.plan.audio.is_mock ? "MOCK" : "REAL"} · {item.plan.audio.provider}{item.plan.audio.speaker ? ` · ${item.plan.audio.speaker}` : ""}
                        </li>
                      )}
                    </ul>
                    {problem && (
                      <p className="provider-warning">{String(problem.message ?? "当前计划无法唯一确定。")}</p>
                    )}
                    {warning && (
                      <p className="composition-warning">{String(warning.message ?? "素材来源存在需要确认的警告。")}</p>
                    )}
                    {detail.latest_export && (
                      <p className={`composition-freshness is-${item.plan?.freshness.toLowerCase() ?? "outdated"}`}>
                        {item.plan?.freshness_reason ?? "无法确认当前成片来源快照。"}
                      </p>
                    )}
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={() => void startSmartMediaRender(item.mode, item.plan)}
                      disabled={
                        busy !== null ||
                        generationInProgress ||
                        item.plan?.status !== "READY" ||
                        (backgroundAudioEnabled && !backgroundAudio)
                      }
                    >
                      {busy === `smart-media-render-${item.mode}` ? "正在冻结来源快照…" : item.button}
                    </button>
                  </article>
                );
              })}
            </div>
            <p className="composition-note">
              高级来源设置仍保留在上方，用于指定特定关键帧或 Video Job；两个主按钮不会调用 VideoProvider。
            </p>
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
              <span className="section-number">05</span>
              <div>
                <p className="eyebrow">播放与下载</p>
                <h2 ref={resultTitleRef} tabIndex={-1}>
                  {exportIsRealAudio
                    ? "真实动漫配音成片"
                    : exportIsRealImage
                      ? "真实动漫成片"
                      : "Mock 视觉成片"}
                </h2>
              </div>
            </div>
            {!currentCompositionMode && (
              <p className="previous-export-notice">
                已有成片，但当前素材来源快照已经变化；下方旧成片仍可播放和下载，需要重新合成后才会标记为最新。
              </p>
            )}
            {currentCompositionMode === "关键帧版" && dynamicPlanNeedsRecompose && (
              <p className="previous-export-notice">
                当前关键帧版成片仍然有效，但已有动态镜头尚未合入；如需视频增强版，请使用“使用动态镜头合成成片”。
              </p>
            )}
            {imageGenerationInProgress && !exportIsRealImage && (
              <p className="previous-export-notice">
                下方仍是上一版 Mock 视觉成片；新的真实动漫画面尚未完成，不会提前标记为真实模型输出。
              </p>
            )}
            {audioGenerationInProgress && !exportIsRealAudio && (
              <p className="previous-export-notice">
                下方仍是上一版成片，真实 AI 旁白尚未完成；在新 Job 成功前不会把 Mock 音频标记为真实配音。
              </p>
            )}
            <div className="result-status-summary" aria-label="成片状态">
              <span className="result-ready">● 已生成</span>
              <span className={`result-provider-badge ${exportIsRealImage ? "is-real" : "is-mock"}`}>
                {exportIsRealImage ? "真实模型 · Animagine XL 4.0" : "Mock 视觉"}
              </span>
              <span className={`result-provider-badge ${exportIsRealAudio ? "is-real" : "is-mock"}`}>
                {exportIsRealAudio
                  ? `真实旁白 · Qwen3-TTS · ${exportAudioSpeaker ?? "未报告音色"}`
                  : "Mock 音频"}
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
                  图像耗时：{jobImageGenerationSeconds(sourceImageJob ?? exportJob)?.toFixed(1) ?? "未报告"} 秒
                </span>
              )}
              {exportIsRealAudio && <span>TTS 耗时：{exportAudioGenerationSeconds?.toFixed(1) ?? "未报告"} 秒</span>}
              {exportIsRealAudio && exportSourceDuration !== null && (
                <span>源计划：{exportSourceDuration.toFixed(2)} 秒</span>
              )}
              {exportIsRealAudio && exportRenderedDuration !== null && (
                <span>渲染计划：{exportRenderedDuration.toFixed(2)} 秒</span>
              )}
              {exportIsRealAudio && exportAudioExtension !== null && (
                <span>旁白延长：{exportAudioExtension.toFixed(2)} 秒</span>
              )}
            </div>
            <div className="result-grid">
              <div className="video-frame">
                <video controls preload="metadata" src={media.video} poster={media.poster}>
                  当前浏览器不支持 HTML5 视频，请下载 MP4 后播放。
                </video>
              </div>
              <aside className="export-info">
                <p className="eyebrow">EXPORT READY</p>
                <h3>{detail.project.title}</h3>
                <details className="technical-details export-technical-details">
                  <summary>技术详情与追溯</summary>
                  <dl>
                  <div><dt>时长</dt><dd>{detail.latest_export.duration_seconds?.toFixed(2) ?? "—"} 秒</dd></div>
                  <div><dt>镜头</dt><dd>{scriptShots.length} 个</dd></div>
                  <div><dt>Script Provider</dt><dd title={scriptProviderUsed}>{scriptProviderUsed}</dd></div>
                  <div><dt>Script Model</dt><dd title={scriptModelUsed}>{scriptModelUsed}</dd></div>
                  <div><dt>Script Source</dt><dd title={scriptSourceUsed}>{scriptSourceUsed}</dd></div>
                  <div><dt>Image Provider</dt><dd title={imageProviderUsed}>{imageProviderUsed}</dd></div>
                  <div><dt>视觉来源</dt><dd>{exportIsRealImage ? "真实本地模型" : "Mock 确定性保底"}</dd></div>
                  {exportIsRealImage && <div><dt>Base seed</dt><dd>{jobBaseSeed(sourceImageJob ?? exportJob) ?? "未报告"}</dd></div>}
                  {exportIsRealImage && <div><dt>真实关键帧</dt><dd>{jobImageCompletedCount(sourceImageJob ?? exportJob)}/{jobImageTotalCount(sourceImageJob ?? exportJob, scriptShots.length)} 张</dd></div>}
                  <div><dt>Audio Provider</dt><dd title={audioProviderUsed}>{audioProviderUsed}</dd></div>
                  <div><dt>音频来源</dt><dd>{exportIsRealAudio ? "真实本地 Qwen3-TTS" : "Mock 确定性保底"}</dd></div>
                  {exportIsRealAudio && <div><dt>旁白音色</dt><dd>{exportAudioSpeaker ?? "未报告"}</dd></div>}
                  {exportIsRealAudio && <div><dt>语言</dt><dd>{exportAudioLanguage ?? "未报告"}</dd></div>}
                  {exportIsRealAudio && exportSourceDuration !== null && <div><dt>源计划时长</dt><dd>{exportSourceDuration.toFixed(3)} 秒</dd></div>}
                  {exportIsRealAudio && exportRenderedDuration !== null && <div><dt>渲染计划时长</dt><dd>{exportRenderedDuration.toFixed(3)} 秒</dd></div>}
                  {exportIsRealAudio && exportAudioExtension !== null && <div><dt>旁白延长</dt><dd>{exportAudioExtension.toFixed(3)} 秒</dd></div>}
                  <div><dt>Video</dt><dd title={videoSourceUsed}>{videoSourceUsed}</dd></div>
                  <div>
                    <dt>SHA-256</dt>
                    <dd title={detail.latest_export.sha256}>{detail.latest_export.sha256?.slice(0, 12) ?? "—"}…</dd>
                  </div>
                  </dl>
                </details>
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
              {isRealAudioJob(latestVisibleJob)
                ? `正在生成真实旁白 ${jobAudioCompletedCount(latestVisibleJob)}/${jobAudioTotalCount(latestVisibleJob, scriptShots.length)}`
                : isRealImageJob(latestVisibleJob)
                ? `正在生成真实图片 ${jobImageCompletedCount(latestVisibleJob)}/${jobImageTotalCount(latestVisibleJob, scriptShots.length)}`
                : "正在生成短片"}
              {` · ${Math.max(0, Math.min(100, Math.round(latestVisibleJob.progress)))}%`}
            </span>
          ) : latestVisibleJob.status === "SUCCEEDED" && isRealAudioJob(latestVisibleJob) && !jobHasFinalMedia(latestVisibleJob) ? (
            <span>旁白已生成</span>
          ) : latestVisibleJob.status === "SUCCEEDED" && latestVisibleJob.job_type === "MEDIA_RERENDER" && jobHasFinalMedia(latestVisibleJob) && media ? (
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
        <span>纸鹤工坊 · 本地 AI 动漫制作工作台</span>
        <span>Mock 始终可辨识；Qwen、ComfyUI 与 Qwen3-TTS 在 8GB 显存下分阶段运行</span>
      </footer>
    </div>
  );
}

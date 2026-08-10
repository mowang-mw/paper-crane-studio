export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
export type ScriptProviderId = "mock" | "llamacpp";
export type ImageProviderId = "mock" | "comfyui-animagine-xl-4";
export type AudioProviderId = "mock" | "qwen3-tts-0.6b-customvoice";
export type VideoProviderId = "mock-video" | "cloud-wan-2.7";
export type VideoMode = "keyframe_motion" | VideoProviderId;
export type AudioSpeaker = "Serena" | "Vivian";
export type AudioLanguage = "Chinese";
export type DesiredShotCount = 3 | 4 | 5 | null;
export type MotionPreset = "static" | "gentle_zoom" | "cinematic_pan";
export type ScriptProviderRuntimeState =
  | "READY_TO_START"
  | "ONLINE"
  | "CONFIG_ERROR"
  | "PORT_CONFLICT"
  | "NOT_APPLICABLE";

export interface BackgroundAudioAsset {
  asset_id: string;
  original_filename: string;
  mime_type: string;
  format: "wav" | "mp3" | "m4a" | "ogg";
  duration_seconds: number;
  size_bytes: number;
  sha256: string;
  storage_path: string;
  source_type: "USER_UPLOAD";
  codec_name: string;
  sample_rate?: number | null;
  channels?: number | null;
  rights_notice: string;
}

export interface MediaPolishOptions {
  motionPreset: MotionPreset;
  backgroundAudioEnabled: boolean;
  backgroundVolume: number;
}

export interface ScriptProviderStatus {
  provider_id: ScriptProviderId;
  display_name: string;
  available: boolean;
  configured: boolean | null;
  model_id: string | null;
  source_type: string;
  server_version?: string | null;
  runtime_state: ScriptProviderRuntimeState | null;
  detail?: string | null;
}

export interface ProvidersStatus {
  default_script_provider: ScriptProviderId | null;
  default_image_provider: ImageProviderId | null;
  default_audio_provider: AudioProviderId | null;
  default_video_provider: "none" | VideoProviderId | null;
  checked_at: string | null;
  providers: ScriptProviderStatus[];
  image_providers: ImageProviderStatus[];
  audio_providers: AudioProviderStatus[];
  video_providers: VideoProviderStatus[];
}

export interface ImageProviderStatus {
  provider_id: ImageProviderId;
  display_name: string;
  available: boolean;
  configured: boolean | null;
  model_id: string | null;
  source_type: string;
  detail?: string | null;
  requires_gpu_handoff?: boolean;
}

export interface AudioProviderStatus {
  provider_id: AudioProviderId;
  display_name: string;
  available: boolean;
  configured: boolean | null;
  model_id: string | null;
  source_type: string;
  detail?: string | null;
  requires_gpu_handoff?: boolean;
  speakers?: AudioSpeaker[];
  default_speaker?: AudioSpeaker;
  language?: AudioLanguage;
}

export interface VideoProviderStatus {
  provider_id: VideoProviderId;
  display_name: string;
  available: boolean;
  configured: boolean | null;
  model_id: string | null;
  source_type: "MOCK" | string;
  detail?: string | null;
  requires_gpu_handoff?: boolean;
  runtime_state?: "READY_TO_USE" | "CONFIG_ERROR";
  requires_api_key?: boolean;
  may_incur_cost?: boolean;
}

export interface ScriptCharacter {
  id: string;
  name: string;
  role: string;
  appearance: string;
  personality: string;
  costume: string;
  consistency_prompt: string;
}

export interface ScriptScene {
  id: string;
  name: string;
  description: string;
  time: string;
  lighting: string;
  consistency_prompt: string;
}

export interface StructuredScriptShot {
  id: string;
  index: number;
  title: string;
  scene_id: string;
  character_ids: string[];
  visual_description: string;
  narration: string;
  duration_seconds: number;
  camera: string;
  image_prompt: string;
  negative_prompt?: string | null;
}

export interface StructuredScript {
  schema_version: "script.v1";
  title: string;
  synopsis: string;
  characters: ScriptCharacter[];
  scenes: ScriptScene[];
  shots: StructuredScriptShot[];
}

export interface GenerationRequestSnapshot {
  project_id?: string;
  output?: { width?: number; height?: number; fps?: number };
  script_provider?: ScriptProviderId;
  source_script_job_id?: string;
  reuse_script_from_job_id?: string;
  image_provider?: ImageProviderId;
  source_image_job_id?: string;
  source_image_asset_id?: string;
  source_image_asset_ids?: Record<string, string>;
  source_video_job_id?: string;
  source_video_asset_ids?: Record<string, string>;
  source_audio_job_id?: string;
  parent_job_id?: string;
  audio_provider?: AudioProviderId;
  video_provider?: VideoProviderId;
  video_options?: Record<string, unknown>;
  speaker?: AudioSpeaker;
  language?: AudioLanguage;
  base_seed?: number;
  image_options?: Record<string, unknown>;
  desired_shot_count?: DesiredShotCount;
  story_char_count?: number;
  retry_of_job_id?: string;
  resumed_from_stage?: "MEDIA_RENDER";
  motion_preset?: MotionPreset;
  background_audio?: Record<string, unknown>;
  media_only?: boolean;
  [key: string]: unknown;
}

export interface ScriptGenerationTrace {
  provider_id?: string;
  source_type?: string;
  model?: string;
  validation_warnings?: ScriptValidationWarnings;
  [key: string]: unknown;
}

export interface ScriptValidationWarnings {
  unused_scene_ids: string[];
  unused_character_ids: string[];
}

export interface DurationNormalization {
  normalized?: boolean;
  applied?: boolean;
  reason?: string;
  original_durations?: number[];
  normalized_durations?: number[];
  final_durations?: number[];
  original_total?: number;
  normalized_total?: number;
  original_total_seconds?: number;
  final_total_seconds?: number;
  [key: string]: unknown;
}

export interface GenerationAttemptError {
  code?: string;
  stage?: string;
  summary?: string;
  message?: string;
  msg?: string;
  field?: string;
  location?: string | string[];
  loc?: Array<string | number>;
  path?: string;
  [key: string]: unknown;
}

export interface GenerationErrorDetail {
  code?: string;
  stage?: string;
  summary?: string;
  message?: string;
  story_char_count?: number;
  desired_shot_count?: DesiredShotCount;
  first_attempt_errors?: Array<GenerationAttemptError | string>;
  repair_attempt_errors?: Array<GenerationAttemptError | string>;
  attempt_errors?: Array<GenerationAttemptError | string>;
  suggestions?: string[];
  provider_id?: string;
  model_id?: string;
  raw_response_path?: string;
  repair_response_path?: string;
  validation_report_path?: string;
  shot_id?: string;
  failed_shot_id?: string;
  shot_index?: number;
  failed_shot_index?: number;
  completed_images?: number;
  image_completed_count?: number;
  completed_image_count?: number;
  total_images?: number;
  image_total_count?: number;
  total_image_count?: number;
  audio_completed_count?: number;
  completed_audio_count?: number;
  completed_audios?: number;
  audio_total_count?: number;
  total_audio_count?: number;
  total_audios?: number;
  reusable_audio_count?: number;
  completed_audio_reusable?: boolean;
  speaker?: AudioSpeaker | string;
  language?: AudioLanguage | string;
  audio_provider?: string;
  retryable?: boolean;
  requires_qwen_shutdown?: boolean;
  requires_gpu_handoff?: boolean;
  oom?: boolean;
  log_path?: string;
  log_paths?: string[] | Record<string, string>;
  [key: string]: unknown;
}

export type ImageShotStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | string;

export interface GeneratedImageShot {
  shot_id: string;
  shot_index?: number;
  status?: ImageShotStatus;
  provider_id?: string;
  model_id?: string;
  image_url?: string;
  image_path?: string;
  image_asset_id?: string;
  width?: number;
  height?: number;
  seed?: number;
  generation_seconds?: number;
  image_sha256?: string;
  warnings?: string[];
  [key: string]: unknown;
}

export type AudioShotStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "REUSED" | string;

export interface GeneratedAudioShot {
  shot_id: string;
  shot_index?: number;
  status?: AudioShotStatus;
  provider_id?: string;
  model_id?: string;
  speaker?: AudioSpeaker | string;
  language?: AudioLanguage | string;
  text?: string;
  audio_path?: string;
  audio_url?: string;
  audio_asset_id?: string;
  audio_url_error?: {
    code: "AUDIO_ASSET_URL_MISSING" | string;
    summary: string;
  };
  sample_rate?: number;
  channels?: number;
  duration_seconds?: number;
  audio_duration_seconds?: number;
  generation_seconds?: number;
  real_time_factor?: number;
  audio_sha256?: string;
  warnings?: string[];
  reused?: boolean;
  [key: string]: unknown;
}

export interface GeneratedVideoShot {
  shot_id: string;
  shot_index?: number;
  status?: JobStatus | string;
  provider_id?: VideoProviderId | string;
  source_type?: string;
  video_path?: string;
  video_url?: string;
  video_asset_id?: string;
  source_image_asset_id?: string;
  source_image_provider_id?: string;
  source_image_source_type?: string;
  duration_seconds?: number;
  width?: number;
  height?: number;
  fps?: number;
  video_sha256?: string;
  mock?: boolean;
  ai_video_generated?: boolean;
  metadata?: Record<string, unknown>;
  video_url_error?: { code: string; summary: string };
  [key: string]: unknown;
}

export interface MediaTimingShot {
  shot_id: string;
  shot_index?: number;
  source_shot_duration?: number;
  source_shot_duration_seconds?: number;
  source_duration_seconds?: number;
  audio_duration?: number;
  audio_duration_seconds?: number;
  lead_in_seconds?: number;
  lead_out_seconds?: number;
  rendered_shot_duration?: number;
  rendered_shot_duration_seconds?: number;
  rendered_duration_seconds?: number;
  extended_by_seconds?: number;
  extension_reason?: string | null;
  [key: string]: unknown;
}

export interface MediaTimingPlan {
  shots?: MediaTimingShot[];
  source_planned_duration_seconds?: number;
  source_total_duration_seconds?: number;
  rendered_planned_duration_seconds?: number;
  rendered_total_duration_seconds?: number;
  audio_extension_seconds?: number;
  extended_by_seconds?: number;
  fps?: number;
  [key: string]: unknown;
}

export interface GenerationResult {
  stage?: string;
  script_provider?: string;
  script_source_type?: string;
  script_model_id?: string;
  script_trace?: ScriptGenerationTrace;
  script_validation_warnings?: ScriptValidationWarnings;
  generation_error?: GenerationErrorDetail;
  desired_shot_count?: DesiredShotCount;
  story_char_count?: number;
  actual_shot_count?: number;
  final_shot_count?: number;
  repair_used?: boolean;
  duration_normalization?: DurationNormalization;
  planned_duration_seconds?: number;
  encoded_duration_seconds?: number;
  duration_delta_seconds?: number;
  duration_tolerance_seconds?: number;
  duration_validation?: "passed_exactly" | "passed_with_media_tolerance" | string;
  resumed_from_stage?: "MEDIA_RENDER";
  resumed_from_job_id?: string;
  script_provider_calls_during_resume?: number;
  media_reused?: boolean;
  reencoded?: boolean;
  image_provider?: string;
  image_model_id?: string;
  image_shots?: GeneratedImageShot[];
  completed_images?: number;
  image_completed_count?: number;
  completed_image_count?: number;
  total_images?: number;
  image_total_count?: number;
  total_image_count?: number;
  current_shot_id?: string;
  current_shot_index?: number;
  image_generation_seconds?: number;
  image_generation_total_seconds?: number;
  generation_seconds_total?: number;
  base_seed?: number;
  script_source_job_id?: string;
  source_script_job_id?: string;
  source_image_job_id?: string;
  source_audio_job_id?: string;
  source_video_job_id?: string;
  source_video_asset_ids?: Record<string, string>;
  parent_job_id?: string;
  audio_provider?: string;
  audio_model_id?: string;
  speaker?: AudioSpeaker | string;
  language?: AudioLanguage | string;
  audio_shots?: GeneratedAudioShot[];
  audio_completed_count?: number;
  completed_audio_count?: number;
  audio_total_count?: number;
  total_audio_count?: number;
  audio_generation_seconds?: number;
  audio_generation_total_seconds?: number;
  tts_generation_seconds?: number;
  current_audio_shot_id?: string;
  current_audio_shot_index?: number;
  timing_plan?: MediaTimingPlan;
  timing_plan_path?: string;
  source_planned_duration_seconds?: number;
  rendered_planned_duration_seconds?: number;
  audio_extension_seconds?: number;
  extended_by_seconds?: number;
  video_source_type?: string;
  video_provider?: string;
  video_shots?: GeneratedVideoShot[];
  video_completed_count?: number;
  video_total_count?: number;
  mock_video_fallback?: boolean;
  final_media_consumes_video?: boolean;
  media_only?: boolean;
  source_type?: string;
  [key: string]: unknown;
}

export interface HealthStatus {
  status?: string;
  service?: string | { status?: string };
  database?: string | { status?: string; available?: boolean };
  tools?: Record<string, unknown>;
  data_dir?: string;
  stage?: string;
  [key: string]: unknown;
}

export interface Project {
  id: string;
  title: string;
  story: string;
  status?: string;
  workflow_status?: string;
  script_json?: StructuredScript | null;
  created_at?: string;
  updated_at?: string;
}

export type ExternalImageSourceType = "AI_GENERATED" | "HUMAN_CREATED" | "OTHER";

export interface ExternalImagePromptBundle {
  shot_id: string;
  shot_title: string;
  adapter: "external-natural-language-v1";
  prompt: string;
  source_fields: Record<string, unknown>;
  lineage: Record<string, unknown>;
}

export interface ImageAssetRecord {
  asset_id: string;
  project_id: string;
  shot_id: string | null;
  database_shot_id: string | null;
  asset_type: "KEYFRAME_IMAGE";
  provider_id: string;
  source_type: string;
  generation_mode?: string | null;
  external_source_type?: ExternalImageSourceType | string | null;
  provider_hint?: string | null;
  original_filename?: string | null;
  sha256: string;
  width?: number | null;
  height?: number | null;
  size_bytes?: number | null;
  imported_at?: string | null;
  exported_prompt?: ExternalImagePromptBundle | null;
  image_url: string;
}

export interface Shot {
  id: string;
  project_id: string;
  shot_index: number;
  title: string;
  visual_description: string;
  narration: string;
  duration_seconds: number;
  provider_id: string;
  parameters_json?: Record<string, unknown> | string | null;
}

export interface GenerationJob {
  id: string;
  project_id: string;
  job_type?: string;
  status: JobStatus;
  progress: number;
  provider_id?: string;
  request_json?: GenerationRequestSnapshot;
  result_json?: GenerationResult | null;
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ExportRecord {
  id: string;
  project_id: string;
  job_id?: string;
  file_path?: string;
  manifest_path?: string;
  duration_seconds?: number;
  sha256?: string;
  video_url?: string;
  media_url?: string;
  download_url?: string;
  manifest_url?: string;
  poster_url?: string;
  created_at?: string;
}

export interface VisualSelection {
  source_image_asset_ids: Record<string, string>;
  source_video_job_id: string | null;
}

export interface BestMediaVisualSelection {
  shot_id: string;
  selected_type: "VIDEO_SHOT" | "IMAGE" | "LEGACY_IMAGE";
  asset_id: string | null;
  source_job_id: string | null;
  provider: string;
  provider_hint: string | null;
  source_type: string;
  is_mock: boolean;
  priority_class: string;
  selection_reason: string;
  source_image_asset_id: string | null;
}

export interface BestMediaAudioSelection {
  job_id: string;
  provider: string;
  source_type: string;
  is_mock: boolean;
  source_script_job_id: string | null;
  source_image_job_id: string | null;
  speaker: string | null;
  reason: string;
}

export interface BestMediaPlan {
  mode: CompositionMode | "BEST_AVAILABLE";
  status: "READY" | "AMBIGUOUS" | "BLOCKED";
  priority: string[];
  shots: BestMediaVisualSelection[];
  audio: BestMediaAudioSelection | null;
  problems: Array<Record<string, unknown>>;
  warnings: Array<Record<string, unknown>>;
  available_image_shot_count: number;
  available_video_shot_count: number;
  freshness: "NO_EXPORT" | "CURRENT" | "OUTDATED";
  freshness_reason: string;
}

export type CompositionMode = "IMAGE_ONLY" | "VIDEO_PREFERRED";

export interface ProjectDetail {
  project: Project;
  shots: Shot[];
  recent_jobs: GenerationJob[];
  video_jobs: GenerationJob[];
  latest_export: ExportRecord | null;
  image_assets: ImageAssetRecord[];
  visual_selection: VisualSelection;
}

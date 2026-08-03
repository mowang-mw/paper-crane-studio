export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
export type ScriptProviderId = "mock" | "llamacpp";
export type ImageProviderId = "mock" | "comfyui-animagine-xl-4";
export type AudioProviderId = "mock" | "qwen3-tts-0.6b-customvoice";
export type AudioSpeaker = "Serena" | "Vivian";
export type AudioLanguage = "Chinese";
export type DesiredShotCount = 3 | 4 | 5 | null;

export interface ScriptProviderStatus {
  provider_id: ScriptProviderId;
  display_name: string;
  available: boolean;
  configured: boolean | null;
  model_id: string | null;
  source_type: string;
  server_version?: string | null;
  detail?: string | null;
}

export interface ProvidersStatus {
  default_script_provider: ScriptProviderId | null;
  default_image_provider: ImageProviderId | null;
  default_audio_provider: AudioProviderId | null;
  checked_at: string | null;
  providers: ScriptProviderStatus[];
  image_providers: ImageProviderStatus[];
  audio_providers: AudioProviderStatus[];
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
  parent_job_id?: string;
  audio_provider?: AudioProviderId;
  speaker?: AudioSpeaker;
  language?: AudioLanguage;
  base_seed?: number;
  image_options?: Record<string, unknown>;
  desired_shot_count?: DesiredShotCount;
  story_char_count?: number;
  retry_of_job_id?: string;
  resumed_from_stage?: "MEDIA_RENDER";
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
  created_at?: string;
}

export interface ProjectDetail {
  project: Project;
  shots: Shot[];
  recent_jobs: GenerationJob[];
  latest_export: ExportRecord | null;
}

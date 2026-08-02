export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
export type ScriptProviderId = "mock" | "llamacpp";
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
  checked_at: string | null;
  providers: ScriptProviderStatus[];
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
  [key: string]: unknown;
}

export interface GenerationResult {
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
  audio_provider?: string;
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

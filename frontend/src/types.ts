export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";

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
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
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

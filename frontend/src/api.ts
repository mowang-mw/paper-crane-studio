import type {
  ExportRecord,
  GenerationJob,
  HealthStatus,
  Project,
  ProjectDetail,
  Shot,
} from "./types";

const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE = (configuredBase || "http://127.0.0.1:8000/api").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as {
        detail?: string | { message?: string };
        message?: string;
      };
      if (typeof payload.detail === "string") message = payload.detail;
      else if (payload.detail?.message) message = payload.detail.message;
      else if (payload.message) message = payload.message;
    } catch {
      const text = await response.text().catch(() => "");
      if (text) message = text;
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function unwrapProject(payload: unknown): Project {
  const value = payload as { project?: Project } & Project;
  return value.project ?? value;
}

function unwrapJob(payload: unknown): GenerationJob {
  const value = payload as {
    job?: GenerationJob;
    job_id?: string;
    project_id?: string;
    status?: GenerationJob["status"];
    progress?: number;
  } & Partial<GenerationJob>;
  if (value.job) return value.job;
  return {
    ...value,
    id: value.id ?? value.job_id ?? "",
    project_id: value.project_id ?? "",
    status: value.status ?? "QUEUED",
    progress: value.progress ?? 0,
  } as GenerationJob;
}

export async function getHealth(): Promise<HealthStatus> {
  return request<HealthStatus>("/health");
}

export async function listProjects(): Promise<Project[]> {
  const payload = await request<Project[] | { items?: Project[]; projects?: Project[] }>("/projects");
  if (Array.isArray(payload)) return payload;
  return payload.items ?? payload.projects ?? [];
}

export async function createProject(input: { title: string; story: string }): Promise<Project> {
  return unwrapProject(
    await request<unknown>("/projects", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteProject(projectId: string): Promise<void> {
  await request<void>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const payload = (await request<unknown>(`/projects/${encodeURIComponent(projectId)}`)) as {
    project?: Project;
    shots?: Shot[];
    recent_jobs?: GenerationJob[];
    jobs?: GenerationJob[];
    latest_export?: ExportRecord | null;
    export?: ExportRecord | null;
  } & Project;
  const project = payload.project ?? payload;
  return {
    project,
    shots: payload.shots ?? [],
    recent_jobs: payload.recent_jobs ?? payload.jobs ?? [],
    latest_export: payload.latest_export ?? payload.export ?? null,
  };
}

export async function generateProject(projectId: string): Promise<GenerationJob> {
  return unwrapJob(
    await request<unknown>(`/projects/${encodeURIComponent(projectId)}/generate`, {
      method: "POST",
    }),
  );
}

export async function getJob(jobId: string): Promise<GenerationJob> {
  return unwrapJob(await request<unknown>(`/jobs/${encodeURIComponent(jobId)}`));
}

export async function retryJob(jobId: string): Promise<GenerationJob> {
  return unwrapJob(
    await request<unknown>(`/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
    }),
  );
}

function absoluteMediaUrl(value: string | undefined): string | null {
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return value;
  if (!value.startsWith("/")) return null;
  const apiUrl = new URL(API_BASE, window.location.origin);
  return new URL(value, apiUrl.origin).toString();
}

export function exportUrls(projectId: string, record: ExportRecord): {
  video: string;
  download: string;
  manifest: string;
} {
  const project = encodeURIComponent(projectId);
  const exportId = encodeURIComponent(record.id);
  const video =
    absoluteMediaUrl(record.video_url ?? record.media_url) ??
    `${API_BASE}/projects/${project}/exports/${exportId}/video`;
  return {
    video,
    download: absoluteMediaUrl(record.download_url) ?? video,
    manifest:
      absoluteMediaUrl(record.manifest_url) ??
      `${API_BASE}/projects/${project}/exports/${exportId}/manifest`,
  };
}

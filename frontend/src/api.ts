import type {
  DesiredShotCount,
  ExportRecord,
  GenerationJob,
  HealthStatus,
  ImageProviderId,
  ImageProviderStatus,
  Project,
  ProjectDetail,
  ProvidersStatus,
  ScriptProviderId,
  ScriptProviderStatus,
  Shot,
} from "./types";

const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE = (configuredBase || "http://127.0.0.1:8000/api").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function errorMessage(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => {
        if (!isRecord(item)) return null;
        return optionalText(item.summary) ?? optionalText(item.message) ?? optionalText(item.msg);
      })
      .filter((item): item is string => item !== null);
    if (messages.length > 0) return messages.join("；");
  }
  if (isRecord(value)) {
    const nested = isRecord(value.generation_error) ? value.generation_error : value;
    return (
      optionalText(nested.summary) ??
      optionalText(nested.message) ??
      optionalText(value.message) ??
      fallback
    );
  }
  return fallback;
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
    let detail: unknown;
    try {
      const payload = (await response.json()) as { detail?: unknown; message?: unknown };
      detail = payload.detail ?? payload;
      message = errorMessage(detail, errorMessage(payload.message, message));
    } catch {
      const text = await response.text().catch(() => "");
      if (text) message = text;
    }
    throw new ApiError(message, response.status, detail);
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScriptProviderId(value: unknown): value is ScriptProviderId {
  return value === "mock" || value === "llamacpp";
}

function isImageProviderId(value: unknown): value is ImageProviderId {
  return value === "mock" || value === "comfyui-animagine-xl-4";
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeProvider(value: unknown): ScriptProviderStatus | null {
  if (!isRecord(value) || !isScriptProviderId(value.provider_id)) return null;
  const fallbackName = value.provider_id === "mock" ? "Mock 离线保底" : "本地 Qwen（llama.cpp）";
  return {
    provider_id: value.provider_id,
    display_name: optionalText(value.display_name) ?? fallbackName,
    available: value.available === true,
    configured: typeof value.configured === "boolean" ? value.configured : null,
    model_id: optionalText(value.model_id),
    source_type: optionalText(value.source_type) ?? "UNKNOWN",
    server_version: optionalText(value.server_version),
    detail: optionalText(value.detail),
  };
}

function normalizeImageProvider(value: unknown): ImageProviderStatus | null {
  if (!isRecord(value) || !isImageProviderId(value.provider_id)) return null;
  const fallbackName =
    value.provider_id === "mock" ? "Mock 视觉" : "Animagine XL 4.0（ComfyUI）";
  return {
    provider_id: value.provider_id,
    display_name: optionalText(value.display_name) ?? fallbackName,
    available: value.available === true,
    configured: typeof value.configured === "boolean" ? value.configured : null,
    model_id: optionalText(value.model_id),
    source_type: optionalText(value.source_type) ?? "UNKNOWN",
    detail: optionalText(value.detail),
    requires_gpu_handoff: value.requires_gpu_handoff === true,
  };
}

export async function getHealth(): Promise<HealthStatus> {
  return request<HealthStatus>("/health");
}

export async function getProviders(): Promise<ProvidersStatus> {
  const payload = await request<unknown>("/providers");
  if (!isRecord(payload)) {
    throw new Error("Provider 状态响应格式无效");
  }
  const providers = Array.isArray(payload.providers)
    ? payload.providers
        .map(normalizeProvider)
        .filter((item): item is ScriptProviderStatus => item !== null)
    : [];
  const imageProviders = Array.isArray(payload.image_providers)
    ? payload.image_providers
        .map(normalizeImageProvider)
        .filter((item): item is ImageProviderStatus => item !== null)
    : [];
  return {
    default_script_provider: isScriptProviderId(payload.default_script_provider)
      ? payload.default_script_provider
      : null,
    default_image_provider: isImageProviderId(payload.default_image_provider)
      ? payload.default_image_provider
      : null,
    checked_at: optionalText(payload.checked_at),
    providers,
    image_providers: imageProviders,
  };
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

export async function generateProject(
  projectId: string,
  scriptProvider: ScriptProviderId,
  desiredShotCount: DesiredShotCount,
): Promise<GenerationJob> {
  return unwrapJob(
    await request<unknown>(`/projects/${encodeURIComponent(projectId)}/generate`, {
      method: "POST",
      body: JSON.stringify({
        script_provider: scriptProvider,
        desired_shot_count: desiredShotCount,
      }),
    }),
  );
}

export async function renderRealImages(
  projectId: string,
  sourceScriptJobId: string,
  baseSeed?: number,
): Promise<GenerationJob> {
  return unwrapJob(
    await request<unknown>(`/projects/${encodeURIComponent(projectId)}/render-real-images`, {
      method: "POST",
      body: JSON.stringify({
        source_script_job_id: sourceScriptJobId,
        image_provider: "comfyui-animagine-xl-4",
        ...(baseSeed === undefined ? {} : { base_seed: baseSeed }),
      }),
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

export function imageAssetUrl(value: string | undefined): string | null {
  return absoluteMediaUrl(value);
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

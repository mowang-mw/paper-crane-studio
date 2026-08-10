import type { GeneratedVideoShot, GenerationJob, JobStatus } from "./types";

export type VideoLineageState = "MATCHED" | "STALE" | "UNKNOWN";

export interface VideoVersionPresentation {
  executionStatus: JobStatus;
  isCurrent: boolean;
  lineage: VideoLineageState;
  selectionLabel: "当前采用" | "可用版本" | "历史版本";
  lineageLabel: "首帧匹配" | "首帧已变更" | "首帧关系待确认";
}

export function describeVideoVersion(input: {
  jobId: string;
  jobStatus: JobStatus;
  selectedJobId: string;
  shots: GeneratedVideoShot[];
  selectedImageAssetIds: Record<string, string>;
}): VideoVersionPresentation;

export function selectVideoDisplayJob(input: {
  selectedJob: GenerationJob | null;
  latestJob: GenerationJob | null;
}): GenerationJob | null;

export function describeVideoVersion({
  jobId,
  jobStatus,
  selectedJobId,
  shots,
  selectedImageAssetIds,
}) {
  const isCurrent = selectedJobId === jobId;
  const hasStaleLineage = shots.some((shot) => {
    const currentImageId = selectedImageAssetIds[shot.shot_id];
    return Boolean(
      currentImageId &&
      shot.source_image_asset_id &&
      currentImageId !== shot.source_image_asset_id,
    );
  });
  const allLineageMatched =
    shots.length > 0 &&
    shots.every((shot) => {
      const currentImageId = selectedImageAssetIds[shot.shot_id];
      return Boolean(
        currentImageId &&
        shot.source_image_asset_id &&
        currentImageId === shot.source_image_asset_id,
      );
    });
  const lineage = hasStaleLineage
    ? "STALE"
    : allLineageMatched
      ? "MATCHED"
      : "UNKNOWN";

  return {
    executionStatus: jobStatus,
    isCurrent,
    lineage,
    selectionLabel: isCurrent
      ? "当前采用"
      : lineage === "STALE"
        ? "历史版本"
        : "可用版本",
    lineageLabel:
      lineage === "STALE"
        ? "首帧已变更"
        : lineage === "MATCHED"
          ? "首帧匹配"
          : "首帧关系待确认",
  };
}

export function selectVideoDisplayJob({ selectedJob, latestJob }) {
  if (latestJob && latestJob.status !== "SUCCEEDED") return latestJob;
  return selectedJob ?? latestJob ?? null;
}

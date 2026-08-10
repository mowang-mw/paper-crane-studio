import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { describeVideoVersion, selectVideoDisplayJob } from "../src/video-version.js";

const selectedImages = { shot1: "image-b" };
const matchingShot = [{ shot_id: "shot1", source_image_asset_id: "image-b" }];

test("multiple jobs can match the current first frame while only one is selected", () => {
  const selected = describeVideoVersion({
    jobId: "job-b",
    jobStatus: "SUCCEEDED",
    selectedJobId: "job-b",
    shots: matchingShot,
    selectedImageAssetIds: selectedImages,
  });
  const available = describeVideoVersion({
    jobId: "job-c",
    jobStatus: "SUCCEEDED",
    selectedJobId: "job-b",
    shots: matchingShot,
    selectedImageAssetIds: selectedImages,
  });
  assert.equal(selected.lineage, "MATCHED");
  assert.equal(available.lineage, "MATCHED");
  assert.equal(selected.selectionLabel, "当前采用");
  assert.equal(available.selectionLabel, "可用版本");
});

test("switching the persisted id changes selection without changing lineage", () => {
  const jobA = describeVideoVersion({
    jobId: "job-a",
    jobStatus: "SUCCEEDED",
    selectedJobId: "job-b",
    shots: matchingShot,
    selectedImageAssetIds: selectedImages,
  });
  const jobB = describeVideoVersion({
    jobId: "job-b",
    jobStatus: "SUCCEEDED",
    selectedJobId: "job-b",
    shots: matchingShot,
    selectedImageAssetIds: selectedImages,
  });
  assert.equal(jobA.selectionLabel, "可用版本");
  assert.equal(jobB.selectionLabel, "当前采用");
  assert.equal(jobA.lineageLabel, "首帧匹配");
  assert.equal(jobB.lineageLabel, "首帧匹配");
});

test("a stale successful job remains successful and may still be selected", () => {
  const stale = describeVideoVersion({
    jobId: "job-old",
    jobStatus: "SUCCEEDED",
    selectedJobId: "job-old",
    shots: [{ shot_id: "shot1", source_image_asset_id: "image-a" }],
    selectedImageAssetIds: selectedImages,
  });
  assert.equal(stale.executionStatus, "SUCCEEDED");
  assert.equal(stale.isCurrent, true);
  assert.equal(stale.selectionLabel, "当前采用");
  assert.equal(stale.lineageLabel, "首帧已变更");
});

test("persisted selection drives the preview after completed jobs", () => {
  const selectedJob = { id: "job-a", status: "SUCCEEDED" };
  const newerJob = { id: "job-b", status: "SUCCEEDED" };
  assert.equal(
    selectVideoDisplayJob({ selectedJob, latestJob: newerJob }),
    selectedJob,
  );
});

test("an unfinished latest job remains visible for progress and errors", () => {
  const selectedJob = { id: "job-a", status: "SUCCEEDED" };
  for (const status of ["QUEUED", "RUNNING", "FAILED"]) {
    const latestJob = { id: "job-b", status };
    assert.equal(
      selectVideoDisplayJob({ selectedJob, latestJob }),
      latestJob,
    );
  }
});

test("polling only refetches and does not write visual selection", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const pollingStart = appSource.indexOf("getJob(activeJob.id)");
  const pollingEnd = appSource.indexOf("}, [activeJob, refreshDetail, refreshProjects]);");
  const pollingSource = appSource.slice(pollingStart, pollingEnd);
  assert.ok(pollingStart >= 0 && pollingEnd > pollingStart);
  assert.doesNotMatch(pollingSource, /updateVisualSelection|persistVideoSelection/);
  assert.match(appSource, /selectedFinalVideoJobId === job\.id/);
  assert.doesNotMatch(appSource, /清除当前动态版本/);
});

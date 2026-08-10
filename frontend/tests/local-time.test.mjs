import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { formatLocalDateTime, parseApiUtcDateTime } from "../src/local-time.js";

test("naive API datetime is interpreted as UTC before timezone display", () => {
  const parsed = parseApiUtcDateTime("2026-08-10T03:43:51");
  assert.equal(parsed?.toISOString(), "2026-08-10T03:43:51.000Z");
  assert.equal(
    formatLocalDateTime("2026-08-10T03:43:51", { timeZone: "Asia/Shanghai" }),
    "2026/8/10 11:43:51",
  );
});

test("explicit UTC and offset datetime remain absolute instants", () => {
  assert.equal(
    parseApiUtcDateTime("2026-08-10T03:43:51Z")?.toISOString(),
    "2026-08-10T03:43:51.000Z",
  );
  assert.equal(
    parseApiUtcDateTime("2026-08-10T11:43:51+08:00")?.toISOString(),
    "2026-08-10T03:43:51.000Z",
  );
});

test("video version UI uses exact job id and does not expose clear selection", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(appSource, /selectedFinalVideoJobId === job\.id/);
  assert.match(appSource, /persistVideoSelection\(job\.id\)/);
  assert.doesNotMatch(appSource, /清除当前动态版本/);
});

import { readFile } from "node:fs/promises";
import process from "node:process";

const reportPath = process.argv[2];
if (!reportPath) {
  throw new Error(
    "usage: node scripts/assert-playwright-results.mjs <report.json>",
  );
}

const minimumTests = Number.parseInt(
  process.env.PLAYWRIGHT_MIN_TESTS ?? "5",
  10,
);
if (!Number.isInteger(minimumTests) || minimumTests <= 0) {
  throw new Error("PLAYWRIGHT_MIN_TESTS must be a positive integer");
}

const report = JSON.parse(await readFile(reportPath, "utf8"));
const observed = [];

function collect(suites) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        observed.push({
          file: spec.file ?? suite.file ?? "unknown",
          title: spec.title ?? "unknown",
          test,
        });
      }
    }
    collect(suite.suites);
  }
}

collect(report.suites);
if (observed.length < minimumTests) {
  throw new Error(
    `Playwright executed ${observed.length} tests; expected at least ${minimumTests}`,
  );
}

const rejected = observed.filter(({ test }) => {
  const finalResult = test.results?.at(-1);
  return (
    test.expectedStatus !== "passed" ||
    test.status !== "expected" ||
    finalResult?.status !== "passed"
  );
});
if (rejected.length > 0) {
  const details = rejected
    .map(
      ({ file, title, test }) =>
        `${file}: ${title} (expected=${test.expectedStatus}, status=${test.status}, final=${test.results?.at(-1)?.status ?? "missing"})`,
    )
    .join("\n");
  throw new Error(
    `Playwright report contains skipped or non-passing tests:\n${details}`,
  );
}

process.stdout.write(`Validated ${observed.length} passing Playwright tests\n`);

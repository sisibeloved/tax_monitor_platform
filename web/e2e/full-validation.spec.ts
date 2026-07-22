import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const entryUrl = process.env.PLAYWRIGHT_ENTRY_URL ?? ".";

const capabilityNames = [
  "季度应计提所得税准确性检查",
  "递延所得税计提/转回准确性检查",
  "当年累计税负率异常监测",
  "潜在纳税调增税务成本",
  "纳税调增科目准确性检查",
  "所得税退税进度监控及入账科目准确性检查",
] as const;

test("shows the six-capability management dashboard", async ({ page }) => {
  await page.goto(entryUrl);

  await expect(
    page.getByRole("heading", {
      name: "集团所得税风险监测驾驶舱",
      level: 2,
    }),
  ).toBeVisible();
  const scopeText = page.getByText(
    /2026年第2季度 · \d+家公司 · 5项已运行 \/ 1项待完善/,
  );
  await expect(scopeText).toBeVisible();
  const companyCount = Number(
    (await scopeText.textContent())?.match(/(\d+)家公司/)?.[1],
  );
  expect(companyCount).toBeGreaterThan(0);
  await expect(page.getByText("已运行能力", { exact: true })).toBeVisible();
  await expect(page.getByText("有示警公司")).toBeVisible();
  await expect(page.getByText("真实请求成功率")).toBeVisible();

  for (const name of capabilityNames) {
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  }
  await expect(page.getByText("待完善", { exact: true })).toHaveCount(1);
  await expect(page.getByText(`当前 ${companyCount} 家`)).toBeVisible();

  const currentTaxCard = page.locator(".ant-card").filter({
    hasText: capabilityNames[0],
  });
  const alertCount = Number(
    await currentTaxCard.locator(".ant-statistic-content-value").textContent(),
  );
  expect(alertCount).toBeGreaterThan(0);

  await page.getByLabel("结果状态").first().click();
  await page.getByText("示警", { exact: true }).last().click();
  await expect(page.getByText(`当前 ${alertCount} 家`)).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出明细" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(
    /^所得税风险监测_季度所得税_2026Q2_\d{14}\.csv$/,
  );
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const csv = await readFile(downloadPath as string, "utf8");
  expect(csv.startsWith("\uFEFF")).toBe(true);
  expect(csv).toContain('"公司代码","公司名称","结果状态"');
  expect(csv).toContain(',"示警",');
  expect(csv).not.toContain(',"正常",');
});

test("shows real-source results or explicit blocks for potential tax cost", async ({
  page,
}) => {
  await page.goto(entryUrl);

  await page.getByLabel("监测能力").first().click();
  await page.getByText("潜在纳税调增税务成本", { exact: true }).last().click();

  await expect(page.getByRole("combobox", { name: "结果状态" })).toBeVisible();
  await expect(page.getByLabel("搜索公司")).toBeVisible();
  await expect(page.getByText(/当前 \d+ 家/).last()).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
});

test("shows the updated deferred-tax formula results and inputs", async ({
  page,
}) => {
  const report = JSON.parse(
    await readFile("public/real-validation-latest.json", "utf8"),
  ) as {
    monitor_summary: {
      deferred_tax: { ALERT: number; CLEAR: number; BLOCKED: number };
    };
  };
  const expected = report.monitor_summary.deferred_tax;
  await page.goto(entryUrl);

  const deferredCard = page.locator(".ant-card").filter({
    hasText: capabilityNames[1],
  });
  await expect(deferredCard).toContainText(
    new RegExp(`示警\\s*${expected.ALERT}\\s*家`),
  );
  await expect(deferredCard).toContainText(
    new RegExp(`正常\\s*${expected.CLEAR}`),
  );
  await expect(deferredCard).toContainText(
    new RegExp(`阻断\\s*${expected.BLOCKED}`),
  );
  await deferredCard.getByRole("button", { name: "查看公司明细" }).click();

  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  for (const label of [
    "可弥补以前年度亏损",
    "损益表累计利润总额",
    "递延所得税计税基础",
    "递延所得税税率",
    "系统累计递延所得税",
    "SAP累计已计提",
    "应计提/转回",
  ]) {
    await expect(table.getByText(label, { exact: false }).first()).toBeVisible();
  }
});

test("keeps controls and results usable on a mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(entryUrl);

  await expect(
    page.getByRole("heading", {
      name: "集团所得税风险监测驾驶舱",
      level: 2,
    }),
  ).toBeVisible();
  await expect(page.getByLabel("整体运行情况")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "监测能力" })).toBeVisible();
  await expect(page.getByLabel("搜索公司")).toBeVisible();
  await expect(page.getByRole("button", { name: "导出明细" })).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
});

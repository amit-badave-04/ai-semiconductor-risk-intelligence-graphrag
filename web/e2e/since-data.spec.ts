import { readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

// docs/specs/m2a-answer-integrity.md P4: "Since our data" in a real browser. The fixture server's /api/freshness is the real
// no-monitor payload (since_data null; faking a monitor there would change the header line every spec pins), so each spec here
// answers /api/freshness itself with the sample the Python test pins to the server's own keys.

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const repoRoot = process.env.REPO_ROOT ?? path.resolve(import.meta.dirname, "..", "..");
const sample = JSON.parse(readFileSync(path.join(repoRoot, "tests", "data", "freshness", "since_data.sample.json"), "utf-8")) as {
  rules: Record<string, unknown>[];
};
const BASE = {
  configured: true, enabled: true, status: "ok", checked_at: new Date().toISOString(), snapshot_as_of: "2026-09-24",
  last_error_at: null, next_check_at: null, pending_count: 0, pending_filings: [], federal_register: null, unresolved: [], duration_s: 1,
};

async function serve(page: Page, since: unknown): Promise<string[]> {
  const requests: string[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/freshness") requests.push(r.url());
  });
  await page.route("**/api/freshness", (route) => route.fulfill({ json: { ...BASE, since_data: since } }));
  return requests;
}

for (const scheme of ["light", "dark"] as const) {
  test(`Freshness page: Since our data has no axe violation, ${scheme} scheme`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: scheme });
    const w = await watch(page, PREVIEW);
    await serve(page, sampleFull());
    await page.goto(`${PREVIEW}/v2/freshness/`);
    await expect(page.getByRole("region", { name: "Since our data" })).toBeVisible();
    await expect(page.getByRole("link", { name: /^Open the filing on SEC.gov/ })).toHaveCount(6);
    await expect(page.getByRole("link", { name: /^Open the rule on FederalRegister.gov/ })).toHaveCount(2);
    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    await testInfo.attach(`axe-since-${scheme}.json`, { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
    expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
    expect(await problems(w)).toEqual([]);
  });
}

function sampleFull(): unknown {
  return JSON.parse(readFileSync(path.join(repoRoot, "tests", "data", "freshness", "since_data.sample.json"), "utf-8"));
}

test("Freshness page: Since our data has no axe violation in forced colours", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active" });
  await serve(page, sampleFull());
  await page.goto(`${PREVIEW}/v2/freshness/`);
  await expect(page.getByRole("region", { name: "Since our data" })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
});

test("at 320 px a 300-character title with no break opportunity stays inside the screen", async ({ page }) => {
  const long = "x".repeat(300);
  await serve(page, { ...(sampleFull() as object), rules: [{ ...sample.rules[0], title: long }] });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(`${PREVIEW}/v2/freshness/`);
  await expect(page.getByRole("region", { name: "Since our data" })).toContainText(long);
  await page.addStyleTag({ content: "*{font-family:Verdana,sans-serif !important;letter-spacing:0.6px !important}" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("the Company page shows its own filings and the BIS line, after one /api/freshness request", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  const requests = await serve(page, sampleFull());
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const block = page.getByRole("region", { name: "New filings since our data" });
  await expect(block).toBeVisible();
  await expect(block.getByText("showing 5 of 7")).toBeVisible();
  await expect(block.getByRole("link", { name: /^Open the filing on SEC.gov/ })).toHaveCount(5);
  await expect(block.getByText(/^Nvidia filed an? 8-K/)).toHaveCount(5);
  await expect(block).not.toContainText("TSMC");
  const line = block.getByRole("link", { name: "2 new BIS rules since our data \u2014 see Freshness" });
  await expect(line).toHaveAttribute("href", /\/freshness\/$/);
  await page.waitForLoadState("networkidle");
  expect(requests).toHaveLength(1);
  expect(await problems(w)).toEqual([]);
});

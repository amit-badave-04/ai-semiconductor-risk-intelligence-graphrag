import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { isPageApiRequest, problems, watch } from "./support.ts";

// R25 / I2 gate "C1 pages pass axe": the company page with data (NVDA), and its not-found state (the fixture server serves only
// NVDA's dossier, so /v2/company/AMD/ is a real 404 from the dossier route). Same tags as a11y.spec.ts; the full violation list
// is attached to the test, and the assertion is on all of it, not only on serious or critical.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/** What Chromium itself logs for the one 404 these tests cause on purpose: one console error and one failed response, for the
 *  AMD dossier. Nothing else is filtered. */
function withoutTheExpected404(list: string[], ticker: string): string[] {
  return list.filter(
    (p) =>
      p !== `failed response: 404 ${PREVIEW}/api/company/${ticker}/dossier` &&
      !(p.startsWith("console error: Failed to load resource") && p.includes("404")),
  );
}

/** A dossier with every chip kind (the fixture's has none of metrics, edges and rules): the xbrl metric, two edge chunks, a rule's
 *  chunk and the rule itself, so axe's WCAG 2.2 checks (target size included) see the chips as they stack in each block. */
const CHUNK = "0001045810-26-000021:I.1:0320";
const EVERY_CHIP_DOSSIER = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  data_as_of: "2026-09-30",
  filings: [{ accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null }],
  metrics: [{ metric: "revenue", value: 130497000000, unit: "USD", period_start: "2025-01-27", period_end: "2026-01-25" }],
  active_risks: [],
  edges: [{ source: "Nvidia", relation: "DEPENDS_ON", target: "TSMC", status: "Active", quote: "We depend on TSMC.", chunk_ids: [CHUNK, "0001045810-26-000021:I.1A:0042"] }],
  rules: [{ source: "Nvidia", relation: "MENTIONED_IN", target: "Export rule", status: null, quote: null, chunk_ids: [CHUNK], rule_id: "2026-01234", date: "2026-03-01", kind: "Rule" }],
};

for (const scheme of ["light", "dark"] as const) {
  test.describe(`company page, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });

    test("with data: every block, no axe violation", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      await page.goto(`${PREVIEW}/v2/company/NVDA/`);
      await expect(page.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Filings", exact: true })).toContainText("10-K");
      await expect(page.getByRole("region", { name: "Active risks" })).toContainText("Fixture risk summary");
      await expect(page.getByRole("region", { name: "Government rules" })).toContainText(
        "linked to the company by keyword matching: actions by the government, not statements the company made", // RD23
      );
      await expect(page.getByRole("navigation", { name: "Companies" }).getByRole("link")).toHaveCount(13);
      // R25 (owner 2026-11-08): the recent risk changes, each kind under its pinned label, a pair that could not be compared
      const changes = page.getByRole("region", { name: "Compared risk disclosures" });
      await expect(changes).toContainText("10-K 2025-02-26 \u2192 10-K 2026-02-25");
      for (const label of ["No matching risk factor found in the earlier filing", "No longer appears as a separate risk factor", "Not matched", "Reworded"]) {
        await expect(changes.getByText(label, { exact: true })).toBeVisible();
      }
      // owner 2026-11-08: each passage of a reworded risk names its side by that side's filing
      await expect(changes).toContainText("10-K 2025-02-26: \u201cFixture older wording of a passage.\u201d");
      await expect(changes).toContainText("10-K 2026-02-25: \u201cFixture newer wording of a passage.\u201d");
      // R25: a filing-backed passage's chunk id is the evidence chip (a button), not text
      await expect(changes.locator('button[data-id="0001045810-26-000021:I.1:0320"]')).toHaveCount(1);
      await expect(changes.locator("[data-cite]")).toHaveCount(0);
      await expect(changes).toContainText("comparison not available (low_text_yield).");
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-company-data.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });

    test("R25: a chip opens the evidence drawer from the keyboard; Escape closes it and focus returns to the chip", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      const evidenceGets: string[] = [];
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (path.startsWith("/api/evidence/")) evidenceGets.push(path);
      });
      await page.goto(`${PREVIEW}/v2/company/NVDA/`);
      const changes = page.getByRole("region", { name: "Compared risk disclosures" });
      const chip = changes.locator('button[data-id="0001045810-26-000021:I.1:0320"]');
      await chip.focus();
      await page.keyboard.press("Enter");
      const drawer = page.getByRole("complementary", { name: "Evidence" });
      await expect(drawer).toBeVisible();
      await expect(drawer.getByRole("button", { name: "Close evidence" })).toBeFocused();
      await expect(drawer).toContainText("Fixture excerpt");
      expect(evidenceGets).toEqual(["/api/evidence/0001045810-26-000021%3AI.1%3A0320"]);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-company-drawer.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(drawer).toHaveCount(0);
      await expect(chip).toBeFocused();
      expect(await problems(w)).toEqual([]);
    });

    for (const width of [1440, 390]) {
      test(`R25 every chip kind at ${width} px: buttons with their labels, no axe violation, no sideways scroll`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 900 });
        await page.route("**/api/company/NVDA/dossier", (route) => route.fulfill({ json: EVERY_CHIP_DOSSIER }));
        await page.goto(`${PREVIEW}/v2/company/NVDA/`);
        const metricChip = page.getByRole("region", { name: "Key numbers" }).getByRole("button", { name: "$130.5B, Revenue, FY2026 (ended Jan 25, 2026): $130,497,000,000. Open source" });
        await expect(metricChip).toBeVisible();
        await expect(page.getByRole("region", { name: "Relationships" }).locator("button[data-id]")).toHaveCount(2);
        const rules = page.getByRole("region", { name: "Government rules" });
        await expect(rules.locator("button[data-id]")).toHaveCount(1); // decision 2: the rule stamp only, no passage chip
        await expect(rules.getByRole("button", { name: "Government rule 2026-01234" })).toBeVisible();
        await expect(rules).toContainText("US export-control rules come from the Federal Register"); // RD23
        await expect(page.locator("main [data-cite]")).toHaveCount(0);
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
        await testInfo.attach(`axe-company-chips-${width}.json`, { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
        expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
        const sideways = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(await sideways()).toBeLessThanOrEqual(0);
        // a wider font than this machine's (CI's Linux fonts are wider): the tables must still fit
        await page.addStyleTag({ content: "*{font-family:Verdana,sans-serif !important;letter-spacing:0.6px !important}" });
        expect(await sideways()).toBeLessThanOrEqual(0);
      });
    }

    test("not found (the dossier answers 404): the not-found view, no axe violation", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      await page.goto(`${PREVIEW}/v2/company/AMD/`);
      await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-company-notfound.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(withoutTheExpected404(await problems(w), "AMD")).toEqual([]);
    });
  });
}

test("an unknown ticker makes no request to the API at all", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (request) => {
    if (isPageApiRequest(request.url())) requests.push(request.url());
  });
  for (const path of ["XXXX", "nvda"]) {
    await page.goto(`${PREVIEW}/v2/company/${path}/`);
    await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
  }
  // The server decides what an encoded slash is before the app runs (the router-level case, /company/..%2F..%2Fapi%2Fadmin%2Fstate,
  // is in pages/__tests__/company.test.tsx); whatever it answers, the app must not call the API for it.
  const encoded = await page.goto(`${PREVIEW}/v2/company/..%2F..%2Fapi%2Fadmin%2Fstate/`);
  await page.waitForLoadState("networkidle");
  test.info().annotations.push({ type: "encoded-path-status", description: `${encoded?.status()}` });
  expect(requests).toEqual([]);
});

test("keyboard: Tab reaches the switcher links, Enter opens one, and focus lands on the heading", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await expect(page.getByRole("region", { name: "Filings", exact: true })).toBeVisible();
  const companies = page.getByRole("navigation", { name: "Companies" });
  await page.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" }).focus();
  // RD19: the switcher comes first, right under the name (the reports and actions follow once the dossier is in)
  await page.keyboard.press("Tab");
  await expect(companies.getByRole("link", { name: "Nvidia (NVDA)" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(companies.getByRole("link", { name: "AMD (AMD)" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(`${PREVIEW}/v2/company/AMD/`);
  // the fixture server has no AMD dossier: the not-found view replaces the page and its h1 keeps the focus
  await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeFocused();
  expect(withoutTheExpected404(await problems(w), "AMD")).toEqual([]);
});

test("the risk changes are asked for once, only after the dossier is ok (R25)", async ({ page }) => {
  const asked: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/company/")) asked.push(pathname);
  });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await expect(page.getByRole("region", { name: "Compared risk disclosures" })).toContainText("Fixture older wording");
  expect(asked).toEqual(["/api/company/NVDA/dossier", "/api/company/NVDA/risk-changes"]);
  asked.length = 0;
  await page.goto(`${PREVIEW}/v2/company/AMD/`);
  await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
  expect(asked).toEqual(["/api/company/AMD/dossier"]);
});

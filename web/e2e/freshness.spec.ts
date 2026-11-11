import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { NAV_FRESHNESS, PAGES, PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

// R26 / I2 gate "C1 pages pass axe": the Freshness page over the fixture server, whose /api/freshness is the real "disabled"
// payload (the monitor is off). Same tags as a11y.spec.ts; the assertion is on the whole violation list.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`freshness page, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });

    test("renders the disabled payload with no axe violation", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      const responses: string[] = [];
      page.on("response", (r) => {
        if (new URL(r.url()).pathname === "/api/freshness") responses.push(`${r.status()}`);
      });
      await page.goto(`${PREVIEW}/v2/freshness/`);
      await expect(page.getByRole("heading", { level: 1, name: "Freshness" })).toBeVisible();
      await expect(page.getByRole("main").getByText("Data status unavailable", { exact: true })).toBeVisible(); // RD27: RD3's words
      await expect(page.getByRole("banner")).toContainText(NAV_FRESHNESS); // the shell line (R26, worded by RD3)
      await expect(page.getByRole("list", { name: "Freshness details" })).toContainText("Off");
      expect(responses).toEqual(["200"]);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-freshness.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });
  });
}

// R26 (owner 2026-11-08: yes): every page carries the freshness line in the header the way the legacy header does: it reads
// /api/freshness (index.html:639), once per page load, and /api/stats is not asked for it. A deep link has zero failed requests
// and zero console errors. The Freshness page joins the header's request.
for (const { path } of PAGES) {
  test(`R26 shell line: /v2/${path} shows the line in the banner after one /api/freshness request`, async ({ page }) => {
    const w = await watch(page, PREVIEW);
    const freshnessRequests: string[] = [];
    page.on("request", (r) => {
      if (new URL(r.url()).pathname === "/api/freshness") freshnessRequests.push(r.url());
    });
    await page.goto(`${PREVIEW}/v2/${path}`);
    await expect(page.getByRole("banner")).toContainText(NAV_FRESHNESS);
    await page.waitForLoadState("networkidle");
    expect(freshnessRequests).toHaveLength(1);
    expect(await problems(w)).toEqual([]);
  });
}

// The real shape of a healthy production payload (monitor.py status_payload): the header link names the data date (RD3).
test("R26 shell line (RD3): a healthy production payload reads 'Data as of <date>' in the header link", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.route("**/api/freshness", (route) =>
    route.fulfill({
      json: {
        configured: true,
        enabled: true,
        status: "ok",
        checked_at: new Date().toISOString(),
        snapshot_as_of: "2026-09-30",
        last_error_at: null,
        next_check_at: new Date(Date.now() + 3600000).toISOString(),
        pending_count: 0,
        pending_filings: [],
        federal_register: null,
        unresolved: [],
        duration_s: 1.4,
      },
    }),
  );
  await page.goto(`${PREVIEW}/v2/method/`);
  // RD3: one date style, and no count when nothing newer is waiting
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Freshness: Data as of Sep 30, 2026", exact: true })).toBeVisible();
  await expect(page.getByRole("banner")).not.toContainText("Data date unknown");
  expect(await problems(w)).toEqual([]);
});

// RD3 review: at 1024 px (the smallest one-row header, lg) the longest wording of the line keeps the header to one row, so it
// never spills over the page.
test("the longest freshness line keeps the header to one row at 1024 px", async ({ page }) => {
  await page.route("**/api/freshness", (route) =>
    route.fulfill({ json: { status: "stale", checked_at: new Date().toISOString(), snapshot_as_of: "2026-09-30", pending_count: 12 } }),
  );
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(`${PREVIEW}/v2/method/`);
  const link = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /^Freshness: Data as of Sep 30, 2026; the check/ });
  await expect(link).toBeVisible();
  const header = await page.getByRole("banner").boundingBox();
  const howItWorks = await page.getByRole("link", { name: "How it works" }).boundingBox();
  expect(header?.height).toBeLessThanOrEqual(54); // 3.5rem at the 15 px root, plus the 1 px border
  expect((howItWorks?.y ?? 0) + (howItWorks?.height ?? 0)).toBeLessThanOrEqual(header?.height ?? 0);
});

// docs/specs/m5b-redesign.md RD27: a full payload in words fits a 320 px screen under a wider font, and the page stays readable
// in forced colours.
const FULL = {
  configured: true, enabled: true, status: "ok", checked_at: "2026-09-29T11:45:00Z", snapshot_as_of: "2026-09-24",
  last_error_at: "2026-09-28T01:00:00Z", next_check_at: "2026-09-29T17:45:00Z", pending_count: 2,
  pending_filings: [{ ticker: "NVDA", cik: 1, form: "10-Q", filing_date: "2026-09-20", accession_no: "0001045810-26-000045", period_of_report: null }],
  federal_register: { graph_count: 1234, live_count: 1240, new_since: 6 }, unresolved: ["TSM"], duration_s: 4.2,
};
for (const width of [390, 320]) {
  test(`RD27 at ${width} px the details fit the screen under a wider font`, async ({ page }) => {
    await page.route("**/api/freshness", (route) => route.fulfill({ json: FULL }));
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${PREVIEW}/v2/freshness/`);
    await expect(page.getByRole("list", { name: "Freshness details" })).toContainText("1234 in our data, 1240 published, 6 not yet in our data");
    await page.addStyleTag({ content: "*{font-family:Verdana,sans-serif !important;letter-spacing:0.6px !important}" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

// R4 review T7: values the page shows as the server wrote them, with no place to break, still wrap inside 320 px.
const UNBROKEN = {
  ...FULL,
  status: "paused_by_admin_until_the_next_quarterly_filing_window_opens",
  unresolved: ["TSM_ADR_LISTING_NOT_RESOLVED_IN_THE_FILER_INDEX"],
  pending_filings: [{ ...FULL.pending_filings[0], ticker: "NVDA_CLASS_A_COMMON_STOCK_PENDING", accession_no: "0001045810-26-000045" }],
};
test("RD27 at 320 px values with no break opportunity wrap inside the screen", async ({ page }) => {
  await page.route("**/api/freshness", (route) => route.fulfill({ json: UNBROKEN }));
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(`${PREVIEW}/v2/freshness/`);
  await expect(page.getByRole("list", { name: "Freshness details" })).toContainText("paused_by_admin_until_the_next_quarterly_filing_window_opens");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("RD27 forced colours: no axe violation on the details", async ({ page }, testInfo) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.route("**/api/freshness", (route) => route.fulfill({ json: FULL }));
  await page.goto(`${PREVIEW}/v2/freshness/`);
  await expect(page.getByRole("list", { name: "Freshness details" })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  await testInfo.attach("axe-freshness-forced.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
  expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
});

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
      await expect(page.getByRole("main").getByText("freshness check unavailable")).toBeVisible();
      await expect(page.getByRole("banner")).toContainText(NAV_FRESHNESS); // the shell line (R26, worded by RD3)
      await expect(page.getByRole("list", { name: "Freshness fields" })).toContainText("disabled");
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
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Data as of Sep 30, 2026", exact: true })).toBeVisible();
  await expect(page.getByRole("banner")).not.toContainText("Data date unknown");
  expect(await problems(w)).toEqual([]);
});

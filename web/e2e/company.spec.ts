import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

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

for (const scheme of ["light", "dark"] as const) {
  test.describe(`company page, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });

    test("with data: every block, no axe violation", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      await page.goto(`${PREVIEW}/v2/company/NVDA/`);
      await expect(page.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Filings" })).toContainText("10-K");
      await expect(page.getByRole("region", { name: "Active risks" })).toContainText("Fixture risk summary");
      await expect(page.getByRole("region", { name: "Federal Register rules" })).toContainText(
        "keyword matches to external Federal Register rules",
      );
      await expect(page.getByRole("navigation", { name: "Companies" }).getByRole("link")).toHaveCount(13);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-company-data.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });

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
    if (new URL(request.url()).pathname.startsWith("/api/")) requests.push(request.url());
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
  await expect(page.getByRole("region", { name: "Filings" })).toBeVisible();
  const companies = page.getByRole("navigation", { name: "Companies" });
  await page.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" }).focus();
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

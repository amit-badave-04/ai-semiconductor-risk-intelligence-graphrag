import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PAGES, PREVIEW } from "./servers.ts";

// B3 / P3: axe on the five pages, in both colour schemes, WCAG 2.2 AA tags (docs/specs/m5b-frontend.md Acceptance); zero
// serious or critical violations. Automated checks cannot detect every WCAG failure, so the manual pass (A11) stays.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`axe, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });
    for (const { path, heading } of PAGES) {
      test(`/v2/${path} has no serious or critical violation`, async ({ page }) => {
        await page.goto(`${PREVIEW}/v2/${path}`);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
        const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
        expect(blocking.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      });
    }
  });
}

test("the not-found view has no serious or critical violation either", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/nope/`);
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
});

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { isPageApiRequest, problems, watch } from "./support.ts";

// R27 / D7 / I2 gate "C1 pages pass axe": the Method & Evidence page. Same tags as a11y.spec.ts; the assertion is on the whole
// violation list. The page makes no API request.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`method page, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });

    test("renders the pinned copy with no axe violation", async ({ page }, testInfo) => {
      const w = await watch(page, PREVIEW);
      const apiRequests: string[] = [];
      page.on("request", (r) => {
        if (isPageApiRequest(r.url())) apiRequests.push(r.url());
      });
      await page.goto(`${PREVIEW}/v2/method/`);
      await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeVisible();
      await expect(page.getByRole("main")).toContainText("keyword matches to external Federal Register rules");
      await expect(page.getByRole("region", { name: "How reliable are change claims?" })).toContainText("6 of 12");
      // the two static links of the copy open elsewhere and are safe; the one other link is the route link to Freshness (R27)
      const external = page.getByRole("main").locator('a[target="_blank"]');
      await expect(external).toHaveCount(2);
      for (const link of await external.all()) {
        await expect(link).toHaveAttribute("rel", "noopener noreferrer");
      }
      await expect(page.getByRole("main").getByRole("link", { name: "Freshness" })).toHaveAttribute("href", "/v2/freshness/");
      await expect(page.getByRole("heading", { name: "Earlier accuracy figures" })).toHaveCount(0);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-method.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(apiRequests).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });
  });
}

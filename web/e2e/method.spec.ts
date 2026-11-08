import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

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
        if (new URL(r.url()).pathname.startsWith("/api/")) apiRequests.push(r.url());
      });
      await page.goto(`${PREVIEW}/v2/method/`);
      await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeVisible();
      await expect(page.getByRole("main")).toContainText("keyword matches to external Federal Register rules");
      await expect(page.getByRole("region", { name: "How reliable are change claims?" })).toContainText("6 of 12");
      for (const link of await page.getByRole("main").getByRole("link").all()) {
        await expect(link).toHaveAttribute("rel", "noopener noreferrer");
      }
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-method.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(apiRequests).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });
  });
}

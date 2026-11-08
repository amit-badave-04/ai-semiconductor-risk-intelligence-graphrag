import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
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
      await expect(page.getByText("freshness check unavailable")).toBeVisible();
      await expect(page.getByRole("list", { name: "Freshness fields" })).toContainText("disabled");
      expect(responses).toEqual(["200"]);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach("axe-freshness.json", { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      expect(await problems(w)).toEqual([]);
    });
  });
}

import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md section 7 (CLS at most 0.02): on the Company page, what is on screen before the dossier arrives (the
// name and the company switcher) does not move when the reports, the actions and the sections arrive. Found by Lighthouse at R3's
// gates (the reports and actions were inserted above the switcher).
for (const width of [412, 1280]) {
  test(`at ${width} px a late dossier does not move the switcher`, async ({ page }) => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/company/NVDA/dossier", async (route) => {
      await gate;
      await route.continue();
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${PREVIEW}/v2/company/NVDA/`);
    const switcher = page.getByRole("navigation", { name: "Companies" });
    await expect(switcher).toBeVisible();
    const before = await switcher.boundingBox();
    release();
    await expect(page.getByRole("navigation", { name: "On this page" })).toBeVisible();
    expect(await switcher.boundingBox()).toEqual(before);
  });
}

import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md section 7 (CLS at most 0.02 on the five pages) and RD3: on a phone the header keeps its height when
// the freshness line arrives late, so nothing below it moves. Found by Lighthouse at R3's gates (mobile CLS 0.29 on every page:
// the freshness link, arriving after a slow /api/freshness, pushed the second row of links onto a new row).
for (const width of [360, 412, 600]) {
  test(`at ${width} px a late freshness line changes neither the header's height nor where main starts`, async ({ page }) => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/freshness", async (route) => {
      await gate;
      await route.continue();
    });
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${PREVIEW}/v2/method/`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const before = await page.evaluate(() => [document.querySelector("header")?.getBoundingClientRect().height, document.getElementById("main")?.getBoundingClientRect().top]);
    release();
    await expect(page.locator("#shell-freshness a")).toBeVisible();
    const after = await page.evaluate(() => [document.querySelector("header")?.getBoundingClientRect().height, document.getElementById("main")?.getBoundingClientRect().top]);
    expect(after).toEqual(before);
  });
}

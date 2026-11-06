import { expect, test } from "@playwright/test";
import { PAGES, PREVIEW } from "./servers.ts";

// R30(2): nothing moves. The computed transition duration is 0s and the animation name is none on EVERY element of each page
// (the built-CSS declaration scan is tests/test_dist_contract.py; this is the same rule seen by the browser).

for (const { path, heading } of PAGES) {
  test(`/v2/${path}: no element has a transition or an animation`, async ({ page }) => {
    await page.goto(`${PREVIEW}/v2/${path}`);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    const moving = await page.evaluate(() => {
      const found: string[] = [];
      for (const el of document.querySelectorAll("*")) {
        const style = getComputedStyle(el);
        const durations = style.transitionDuration.split(",").map((d) => d.trim());
        if (durations.some((d) => d !== "0s")) found.push(`${el.tagName} transition ${style.transitionDuration}`);
        if (style.animationName !== "none") found.push(`${el.tagName} animation ${style.animationName}`);
      }
      return { found, checked: document.querySelectorAll("*").length };
    });
    expect(moving.checked).toBeGreaterThan(10);
    expect(moving.found).toEqual([]);
  });
}

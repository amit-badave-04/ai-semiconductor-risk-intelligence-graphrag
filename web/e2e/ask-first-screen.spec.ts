import { expect, test, type Locator, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD4 and RD4a: before any question, the headline, the labelled question box and the cost line are
// on the first screen without scrolling, at 1440x900 and at 390x844, in that order, and nothing shifts while the examples and
// stats arrive (CLS 0.02 or less, the spec's budget).

const COST_LINE = /^Free to use, up to \d+ live questions every \d+ minutes \(\d+ a day\)\.$/;
const CLS_BUDGET = 0.02;

async function bottomOf(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box, "the element is rendered").not.toBeNull();
  return (box?.y ?? 0) + (box?.height ?? 0);
}

async function watchLayoutShift(page: Page): Promise<void> {
  await page.addInitScript(() => {
    type Source = { node?: Node; previousRect: DOMRectReadOnly; currentRect: DOMRectReadOnly };
    const w = window as unknown as { __cls: number; __moved: string[] };
    w.__cls = 0;
    w.__moved = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as unknown as { value: number; hadRecentInput: boolean; sources?: Source[] }[]) {
        if (entry.hadRecentInput) continue;
        w.__cls += entry.value;
        for (const s of entry.sources ?? []) {
          const el = s.node instanceof Element ? s.node : s.node?.parentElement;
          const what = `${el?.tagName.toLowerCase() ?? "?"}${el?.id ? `#${el.id}` : ""} "${(el?.textContent ?? "").trim().slice(0, 30)}"`;
          w.__moved.push(`${entry.value.toFixed(4)} ${what} y ${Math.round(s.previousRect.y)}->${Math.round(s.currentRect.y)} x ${Math.round(s.previousRect.x)}->${Math.round(s.currentRect.x)}`);
        }
      }
    }).observe({ type: "layout-shift", buffered: true });
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`the headline, the question box and the cost line are on the first screen at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await watchLayoutShift(page);
    // the slow case, made deterministic: everything the page fills in after its first paint arrives late
    for (const path of ["**/api/freshness", "**/api/stats", "**/api/examples"]) {
      await page.route(path, async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 700));
        await route.continue();
      });
    }
    await page.goto(`${PREVIEW}/v2/`);
    const heading = page.getByRole("heading", { level: 1, name: "Ask about 13 chip and AI companies." });
    const box = page.getByRole("textbox", { name: "Your question" });
    const cost = page.getByText(COST_LINE);
    await expect(heading).toBeVisible();
    await expect(box).toBeVisible();
    await expect(cost).toBeVisible();
    await page.waitForLoadState("networkidle");

    for (const [name, locator] of [["headline", heading], ["question box", box], ["cost line", cost]] as const) {
      expect(await bottomOf(locator), `${name} is above the fold`).toBeLessThanOrEqual(viewport.height);
    }
    expect(await bottomOf(heading)).toBeLessThan(await bottomOf(box));
    expect(await bottomOf(box)).toBeLessThan(await bottomOf(cost));
    const { cls, moved } = await page.evaluate(() => {
      const w = window as unknown as { __cls: number; __moved: string[] };
      return { cls: w.__cls, moved: w.__moved };
    });
    expect(cls, `layout shift while the page loads; what moved: ${moved.join(" | ")}`).toBeLessThanOrEqual(CLS_BUDGET);
  });
}

// RD1: a 3 px focus ring. The textarea draws none of its own inside the box, so the box shows it while the textarea has focus.
test("the question box shows the 3 px focus ring while the reader types in it", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  const box = page.getByRole("textbox", { name: "Your question" });
  await page.keyboard.press("Tab"); // keyboard focus first, so the ring is a focus-visible one
  await box.focus();
  const outline = await box.evaluate((el) => {
    const style = getComputedStyle(el.parentElement as HTMLElement);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  expect(outline).toEqual({ width: "3px", style: "solid" });
});

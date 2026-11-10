import { readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { firstStarter } from "./support.ts";

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

// Accessibility review of baba14d: a focused control draws the global 3 px outline (RD1), which forced-colours mode keeps; a
// box-shadow ring alone disappears there.
test("the Ask button, a starter and the More options toggle show the 3 px outline when focused from the keyboard", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  const controls = [
    page.getByRole("button", { name: "Ask", exact: true }),
    page.getByRole("button", { name: /More options/ }),
    page.getByRole("region", { name: "Or try an example" }).getByRole("listitem").first().getByRole("button"),
  ];
  for (const control of controls) {
    await control.waitFor();
    await control.focus();
    await page.keyboard.press("Shift"); // a key press, so the browser treats the focus as keyboard focus
    const outline = await control.evaluate((el) => {
      const style = getComputedStyle(el);
      return { width: style.outlineWidth, style: style.outlineStyle };
    });
    expect(outline, await control.textContent() ?? "").toEqual({ width: "3px", style: "solid" });
  }
});

// RD4, SR3, SR9 in a real browser: a specimen stamp opens a real example (the evidence recorded from the live service, served
// here by route), and with the evidence drawer open one Escape closes only the popover.
const RECORDED = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../src/test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, unknown>;
};

async function serveRecordedSpecimens(page: Page): Promise<string[]> {
  const asked: string[] = [];
  for (const [id, payload] of Object.entries(RECORDED.payloads)) {
    await page.route(`**/api/evidence/${encodeURIComponent(id)}`, async (route) => {
      asked.push(id);
      await route.fulfill({ json: payload });
    });
  }
  return asked;
}

test("a specimen stamp opens its real example, fetched once by its public id, and axe finds nothing serious with it open", async ({ page }) => {
  const asked = await serveRecordedSpecimens(page);
  await page.goto(`${PREVIEW}/v2/`);
  const stamp = page.getByRole("button", { name: "reported number" });
  await expect(stamp).toHaveAttribute("aria-expanded", "false");
  expect((await stamp.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(24); // WCAG 2.5.8
  await stamp.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Nvidia revenue, FY2026 (ended Jan 25, 2026)");
  await expect(dialog).toContainText("$215.9B");
  await expect(stamp).toHaveAttribute("aria-expanded", "true");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(stamp).toBeFocused();
  await stamp.click();
  await expect(page.getByRole("dialog")).toContainText("$215.9B");
  expect(asked).toEqual(["xbrl:1045810:revenue:2026-01-25"]); // the second opening is served from the page's own cache
});

test("with the evidence drawer open, one Escape closes the specimen popover and leaves the drawer open (SR9)", async ({ page }) => {
  await serveRecordedSpecimens(page);
  await page.goto(`${PREVIEW}/v2/`);
  await firstStarter(page).click();
  await page.locator("#answer [data-chip-key]").first().click();
  const drawer = page.getByRole("complementary", { name: "Evidence" });
  await expect(drawer).toBeVisible();
  await page.getByRole("button", { name: "filing passage" }).click();
  await expect(page.getByRole("dialog")).toContainText("Nvidia 10-K");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(drawer).toBeVisible();
  await page.keyboard.press("Escape"); // the next one is the drawer's
  await expect(drawer).toBeHidden();
});

// RD6: on a wide screen the sample sits beside the question box; on a phone it follows it.
test("what an answer looks like sits beside the box at 1440 px and below it at 390 px", async ({ page }) => {
  const sample = page.getByRole("region", { name: "What an answer looks like" });
  const box = page.getByRole("textbox", { name: "Your question" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${PREVIEW}/v2/`);
  const [s, b] = [await sample.boundingBox(), await box.boundingBox()];
  expect(s && b && s.x >= b.x + b.width).toBe(true);
  expect(s && b && s.y < b.y + b.height).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  const [s2, b2] = [await sample.boundingBox(), await box.boundingBox()];
  expect(s2 && b2 && s2.y >= b2.y + b2.height).toBe(true);
});

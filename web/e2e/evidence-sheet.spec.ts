import { expect, test, type Locator, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { firstStarter } from "./support.ts";

// docs/specs/m5b-redesign.md RD16 in a real browser: below 900 px the evidence panel is a bottom sheet and the page is padded by
// its height, so nothing that can take focus hides under it (WCAG 2.4.11) and the chip that opened it is scrolled above it; from
// 900 px it is a full-height column beside the page, which makes room for it (nothing, the header included, is covered). Its
// header keeps a visible Close of at least 44 px. No sideways scroll at any size.
const drawer = (page: Page) => page.getByRole("complementary", { name: "Evidence" });
const chips = (page: Page) => page.locator("#answer [data-chip-key]");

async function openLastChip(page: Page, width: number, height: number): Promise<Locator> {
  await page.setViewportSize({ width, height });
  await page.goto(`${PREVIEW}/v2/`);
  const question = (await firstStarter(page).innerText()).trim();
  const box = page.getByRole("textbox", { name: "Your question" });
  await box.fill(question);
  await box.press("Control+Enter");
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  const chip = chips(page).last();
  await chip.scrollIntoViewIfNeeded();
  await chip.click();
  await expect(drawer(page)).toBeVisible();
  return chip;
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  if (found === null) throw new Error("no box");
  return found;
}

const noSidewaysScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

for (const [width, height] of [
  [390, 844],
  [899, 700],
] as const) {
  test(`at ${width} x ${height} it is a bottom sheet, the page makes room and the chip stays above it`, async ({ page }) => {
    const chip = await openLastChip(page, width, height);
    const sheet = await box(drawer(page));
    expect(sheet.x).toBe(0);
    expect(sheet.width).toBeCloseTo(width, 0);
    expect(sheet.y + sheet.height).toBeCloseTo(height, 0);
    expect(sheet.height).toBeLessThanOrEqual(height * 0.6);
    const padding = await page.evaluate(() => parseFloat(getComputedStyle(document.body).paddingBottom));
    expect(padding).toBeCloseTo(sheet.height, 0);
    const opener = await box(chip);
    expect(opener.y + opener.height).toBeLessThanOrEqual(sheet.y + 1); // scrolled to the edge: sub-pixel rounding
    const close = await box(drawer(page).getByRole("button", { name: "Close evidence" }));
    expect(close.height).toBeGreaterThanOrEqual(44);
    expect(await noSidewaysScroll(page)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(chip).toBeFocused();
    expect(await page.evaluate(() => parseFloat(getComputedStyle(document.body).paddingBottom))).toBe(0);
  });
}

for (const [width, height] of [
  [900, 700],
  [1024, 768],
  [1280, 800],
  [1000, 480],
] as const) {
  test(`at ${width} x ${height} it is a full-height column beside the page, covering nothing`, async ({ page }) => {
    const chip = await openLastChip(page, width, height);
    const column = await box(drawer(page));
    expect(column.y).toBe(0);
    expect(column.x + column.width).toBeCloseTo(width, 0);
    expect(column.height).toBeCloseTo(height, 0);
    for (const target of [chip, page.locator("main"), page.getByRole("banner"), page.getByRole("link", { name: "How it works" })]) {
      const b = await box(target);
      expect(b.x + b.width).toBeLessThanOrEqual(column.x + 1);
    }
    const answer = await box(page.locator("#answer"));
    expect(answer.width).toBeGreaterThan(300); // the Ask page's two columns become one while the panel is open
    const close = drawer(page).getByRole("button", { name: "Close evidence" });
    await expect(close).toBeInViewport();
    expect((await box(close)).height).toBeGreaterThanOrEqual(44);
    expect(await noSidewaysScroll(page)).toBe(true);
  });
}

test("on a short screen the header stays put while the evidence scrolls", async ({ page }) => {
  await openLastChip(page, 1000, 480);
  const body = drawer(page).locator("[data-evidence-body]");
  await expect(body).toContainText("id: ");
  await body.evaluate((element) => element.scrollTo(0, element.scrollHeight));
  await expect(drawer(page).getByRole("button", { name: "Close evidence" })).toBeInViewport();
});

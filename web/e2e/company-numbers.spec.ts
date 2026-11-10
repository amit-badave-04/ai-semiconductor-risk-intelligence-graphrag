import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD20, RD22, D-R2 and section 7 item 4 in a real browser, over the fixture server's NVDA (four years,
// a stale capex row), TSM (TWD) and ASML (EUR, no R&D): the table scrolls inside its own region and the page never scrolls
// sideways, even with a wider font; every figure stamp, the rules link and the switch are at least 24 x 24 px; the switch's two
// states stay apart in forced colours; no axe violation in either scheme.
const WIDE_FONT = "*{font-family:Verdana,sans-serif !important;letter-spacing:0.6px !important}";
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const numbers = (page: Page) => page.getByRole("region", { name: "Key numbers", exact: true });

test("NVDA: four fiscal years, a stale metric in words, and the exact values on the switch", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const table = numbers(page).getByRole("table", { name: "Key numbers" });
  await expect(table.getByRole("columnheader")).toHaveText(["Metric", "FY2026 (ended Jan 25, 2026)", "FY2025 (ended Jan 26, 2025)", "FY2024 (ended Jan 28, 2024)", "FY2023 (ended Jan 29, 2023)"]);
  await expect(numbers(page)).toContainText("Not in our data for these years. The latest we hold is FY2012.");
  await expect(numbers(page)).toContainText("$215.9B");
  await numbers(page).getByRole("switch", { name: "Show exact values" }).click();
  await expect(numbers(page)).toContainText("$215,938,000,000");
  await expect(page.getByRole("region", { name: "At a glance" })).toContainText("$215.9B");
});

test("TSM and ASML: their own currency, never a dollar sign", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/TSM/`);
  await expect(numbers(page)).toContainText("NT$3.8T");
  await page.goto(`${PREVIEW}/v2/company/ASML/`);
  await expect(numbers(page)).toContainText("€32.7B");
  expect(await numbers(page).innerText()).not.toMatch(/(^|[^T])\$/);
  await expect(page.getByRole("region", { name: "At a glance" })).toContainText("Not in our data for these years.");
});

for (const width of [390, 320]) {
  test(`at ${width} px with a wider font the table scrolls inside its region and the page never sideways`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${PREVIEW}/v2/company/NVDA/`);
    await expect(numbers(page)).toBeVisible();
    await page.addStyleTag({ content: WIDE_FONT });
    const scroller = numbers(page).getByRole("region", { name: "Key numbers table" });
    expect(await scroller.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await scroller.focus();
    await expect(scroller).toBeFocused();
  });
}

test("every figure stamp, the rules link and the switch are at least 24 x 24 px on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const glance = page.getByRole("region", { name: "At a glance" });
  await expect(glance).toBeVisible();
  const targets = [
    ...(await glance.locator("button[data-chip-key]").all()),
    ...(await numbers(page).locator("button[data-chip-key]").all()),
    glance.getByRole("link", { name: "2 government rules listed below" }),
    numbers(page).getByRole("switch", { name: "Show exact values" }),
  ];
  expect(targets.length).toBeGreaterThan(10);
  for (const target of targets) {
    const box = await target.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(24);
  }
});

test("the rules link of At a glance focuses the Government rules heading", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await page.getByRole("region", { name: "At a glance" }).getByRole("link", { name: "2 government rules listed below" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Government rules" })).toBeFocused();
});

test("in forced colours the switch keeps an edge and its thumb moves between the two states", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const control = numbers(page).getByRole("switch", { name: "Show exact values" });
  const edge = () => control.evaluate((el) => getComputedStyle(el).borderTopStyle);
  const thumbX = () => control.evaluate((el) => (el.firstElementChild as HTMLElement).getBoundingClientRect().left);
  expect(await edge()).not.toBe("none");
  const off = await thumbX();
  await control.click();
  await expect(control).toBeChecked();
  expect(await edge()).not.toBe("none");
  expect(Math.abs((await thumbX()) - off)).toBeGreaterThanOrEqual(10);
});

for (const scheme of ["light", "dark"] as const) {
  test(`no axe violation on the Key numbers and At a glance, ${scheme} scheme, both switch states`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(`${PREVIEW}/v2/company/NVDA/`);
    await expect(numbers(page)).toBeVisible();
    for (const state of ["compact", "exact"]) {
      if (state === "exact") await numbers(page).getByRole("switch", { name: "Show exact values" }).click();
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await testInfo.attach(`axe-company-numbers-${scheme}-${state}.json`, { body: JSON.stringify(results.violations, null, 1), contentType: "application/json" });
      expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
    }
  });
}

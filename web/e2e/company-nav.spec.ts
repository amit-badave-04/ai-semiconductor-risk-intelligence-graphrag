import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD19 and RD21 in a real browser: the reports in words under the name; one row of in-page links that
// scrolls sideways on a phone and never wraps (the page itself never scrolls sideways, even with a wider font); following a link
// or opening the page at a section focuses that section's heading; any other hash is ignored without an error.
const nav = (page: Page) => page.getByRole("navigation", { name: "On this page" });
const WIDE_FONT = "*{font-family:Verdana,sans-serif !important;letter-spacing:0.6px !important}";

test("the header states the reports in words, and a 20-F filer files no 10-Q", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await expect(page.getByText("10-K for the fiscal year ended Jan 25, 2026, filed Feb 25, 2026")).toBeVisible();
  await expect(page.getByText("10-Q filed Aug 27, 2026")).toBeVisible();
  await page.goto(`${PREVIEW}/v2/company/TSM/`);
  await expect(page.getByText("Files an annual report on Form 20-F; no quarterly 10-Q")).toBeVisible();
  await expect(page.getByText("20-F for the fiscal year ended Dec 31, 2025, filed Apr 16, 2026")).toBeVisible();
});

for (const width of [390, 320]) {
  test(`at ${width} px the links stay on one row that scrolls itself and fits the screen`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${PREVIEW}/v2/company/NVDA/`);
    await page.addStyleTag({ content: WIDE_FONT });
    const links = nav(page).getByRole("link");
    await expect(links).toHaveCount(5);
    const tops = await links.evaluateAll((all) => all.map((a) => Math.round(a.getBoundingClientRect().top)));
    expect(new Set(tops).size).toBe(1);
    expect(await nav(page).locator("ul").evaluate((ul) => ul.scrollWidth > ul.clientWidth)).toBe(true);
    // the row itself fits the screen (the page-wide check is with the Key numbers table, R3 slice 4, which replaces the old one)
    expect(await nav(page).evaluate((n) => n.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
    for (const link of await links.all()) {
      const box = await link.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    }
    const last = links.last();
    await last.focus();
    await expect(last).toBeInViewport();
    // the focus outline (3 px, 2 px offset) fits inside the scrolling row (R3 review A3)
    expect(await nav(page).locator("ul").evaluate((ul) => parseFloat(getComputedStyle(ul).paddingTop))).toBeGreaterThanOrEqual(5);
  });
}

test("following a link focuses its section's heading and puts the section in the address", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await nav(page).getByRole("link", { name: "Filings" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Filings" })).toBeFocused();
  expect(new URL(page.url()).hash).toBe("#filings");
  await nav(page).getByRole("link", { name: "Government rules" }).press("Enter");
  await expect(page.getByRole("heading", { level: 2, name: "Government rules" })).toBeFocused();
});

test("a page opened at a section focuses its heading; Back returns to the previous section", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/#relationships`);
  await expect(page.getByRole("heading", { level: 2, name: "Relationships" })).toBeFocused();
  await nav(page).getByRole("link", { name: "Filings" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Filings" })).toBeFocused();
  await page.goBack();
  await expect(page.getByRole("heading", { level: 2, name: "Relationships" })).toBeFocused();
});

for (const hash of ["#a,b", "#:has(*)", '#"]', "#%E0%A4%A"]) {
  test(`the hash ${hash} is ignored without an error`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${PREVIEW}/v2/company/NVDA/${hash}`);
    await expect(nav(page)).toBeVisible();
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("with the evidence sheet open on a phone, a heading the navigation focuses is above the sheet", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await page.getByRole("region", { name: "Key numbers" }).locator("[data-chip-key]").first().click();
  const sheet = page.getByRole("complementary", { name: "Evidence" });
  await expect(sheet).toBeVisible();
  await nav(page).getByRole("link", { name: "Filings" }).click();
  const heading = page.getByRole("heading", { level: 2, name: "Filings" });
  await expect(heading).toBeFocused();
  const h = await heading.boundingBox();
  const s = await sheet.boundingBox();
  expect((h?.y ?? 0) + (h?.height ?? 0)).toBeLessThanOrEqual((s?.y ?? 0) + 1);
});

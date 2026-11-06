import { expect, test } from "@playwright/test";
import { PAGES, PREVIEW } from "./servers.ts";

// A7: on a route change focus moves to the page's h1, document.title names the page, and header, a named nav and main exist.

test.describe("landmarks and titles, per page", () => {
  for (const { path, heading } of PAGES) {
    test(`/v2/${path}`, async ({ page }) => {
      await page.goto(`${PREVIEW}/v2/${path}`);
      await expect(page.getByRole("banner")).toHaveCount(1);
      await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(1);
      await expect(page.getByRole("main")).toHaveCount(1);
      await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
      await expect(page).toHaveTitle(`${heading} · semigraph`);
    });
  }
});

test("a route change moves focus to the new h1 and the title follows", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByRole("heading", { level: 1, name: "Ask" })).not.toBeFocused();
  const nav = page.getByRole("navigation", { name: "Main" });
  for (const { path, heading } of PAGES.slice(1)) {
    await nav.getByRole("link", { name: heading.startsWith("Nvidia") ? "Company" : heading }).click();
    await expect(page).toHaveURL(`${PREVIEW}/v2/${path}`);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeFocused();
    await expect(page).toHaveTitle(`${heading} · semigraph`);
  }
});

test("the skip link is the first tab stop and moves to main", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(`${PREVIEW}/v2/#main`);
});

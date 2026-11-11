import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD27 and the R2 review's A3 in a real browser: Method opened at its reliability section focuses that
// heading; "How it works" still lands on the h1; a crafted hash is ignored without an error; the plain lead comes first.
test("opened at #method-reliability, focus is on that heading", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/method/#method-reliability`);
  await expect(page.getByRole("heading", { level: 2, name: "How reliable are change claims?" })).toBeFocused();
});

test("'How it works' lands on the h1, and the plain lead comes before the technical explanation", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "How it works" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
  await expect(page.getByRole("main").locator("p").first()).toHaveText(/^Answers are written from passages of the companies' SEC filings/);
});

// R4 review T4: the same rule when the route changes in the page (Layout's route focus), not only on a direct load.
async function routeTo(page: import("@playwright/test").Page, url: string) {
  await page.evaluate((target) => {
    window.history.pushState(null, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, url);
}

test("a route change to Method with #method-reliability focuses that heading", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await routeTo(page, "/v2/method/#method-reliability");
  await expect(page.getByRole("heading", { level: 2, name: "How reliable are change claims?" })).toBeFocused();
});

for (const hash of ['#method-reliability"]', "#:has(*)", "#key-numbers", "#method-reliabilityX"]) {
  test(`a route change to Method with the hash ${hash} focuses the h1, without an error`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${PREVIEW}/v2/`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await routeTo(page, `/v2/method/${hash}`);
    await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
    expect(errors).toEqual([]);
  });
}

for (const hash of ['#method-reliability"]', "#:has(*)", "#key-numbers"]) {
  test(`the hash ${hash} on Method is ignored without an error`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${PREVIEW}/v2/method/${hash}`);
    await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeVisible();
    expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe("H2");
    expect(errors).toEqual([]);
  });
}

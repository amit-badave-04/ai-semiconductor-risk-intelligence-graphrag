import { expect, test } from "@playwright/test";
import { FLIPPED, PAGES, PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

// R12 / P5 / P6: ONE dist, served by the real ui_v2 router over fakes. Every deep link answers, with zero console errors, zero
// securitypolicyviolation events, every request to the page's own origin, and every asset answered 200.

async function open(page: import("@playwright/test").Page, origin: string, url: string) {
  const w = await watch(page, origin);
  const response = await page.goto(origin + url);
  return { w, response };
}

test.describe("preview server (UI_V2_PREVIEW): the app at /v2/", () => {
  for (const { path, heading } of PAGES) {
    test(`/v2/${path} renders ${heading}`, async ({ page }) => {
      const { w, response } = await open(page, PREVIEW, `/v2/${path}`);
      expect(response?.status()).toBe(200);
      expect(response?.headers()["x-robots-tag"]).toBe("noindex");
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      expect(await problems(w)).toEqual([]);
      const assets = w.assetResponses;
      expect(assets.some((a) => a.url.endsWith(".js")), "an entry script loaded").toBe(true);
      expect(assets.some((a) => a.url.endsWith(".css")), "a stylesheet loaded").toBe(true);
      expect(assets.filter((a) => a.status !== 200)).toEqual([]);
    });
  }

  test("an unknown /v2/ path renders the not-found view with status 200", async ({ page }) => {
    const { w, response } = await open(page, PREVIEW, "/v2/nope/");
    expect(response?.status()).toBe(200);
    await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
    expect(await problems(w)).toEqual([]);
  });

  test("an unknown company renders the not-found view", async ({ page }) => {
    const { w } = await open(page, PREVIEW, "/v2/company/XXXX/");
    await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
    expect(await problems(w)).toEqual([]);
  });

  test("the old page is still at / and /legacy", async ({ page }) => {
    for (const url of ["/", "/legacy"]) {
      const response = await page.goto(PREVIEW + url);
      expect(response?.status()).toBe(200);
      expect(response?.headers()["x-robots-tag"]).toBeUndefined();
      await expect(page.getByRole("heading", { level: 1 })).toContainText("AI Semiconductor Risk Intelligence");
    }
  });

  test("following the navigation stays in the app and keeps the /v2 prefix", async ({ page }) => {
    const { w } = await open(page, PREVIEW, "/v2/");
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Freshness" }).click();
    await expect(page).toHaveURL(`${PREVIEW}/v2/freshness/`);
    await expect(page.getByRole("heading", { level: 1, name: "Freshness" })).toBeFocused();
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Company" }).click();
    await expect(page).toHaveURL(`${PREVIEW}/v2/company/NVDA/`);
    expect(await problems(w)).toEqual([]);
  });

  test("a reload on a deep link keeps working (the server answers the app, not a 404)", async ({ page }) => {
    const { w } = await open(page, PREVIEW, "/v2/method/");
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeVisible();
    expect(await problems(w)).toEqual([]);
  });
});

test.describe("flipped server (UI_V2): the same dist at /", () => {
  for (const { path, heading } of PAGES) {
    test(`/${path} renders ${heading}`, async ({ page }) => {
      const { w, response } = await open(page, FLIPPED, `/${path}`);
      expect(response?.status()).toBe(200);
      expect(response?.headers()["x-robots-tag"]).toBeUndefined();
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      expect(await problems(w)).toEqual([]);
      expect(w.assetResponses.some((a) => a.url.includes("/v2/assets/")), "assets are still under /v2/assets/").toBe(true);
      expect(w.assetResponses.filter((a) => a.status !== 200)).toEqual([]);
    });
  }

  test("/v2/ stays an alias and the old page stays at /legacy", async ({ page }) => {
    const { w } = await open(page, FLIPPED, "/v2/company/NVDA/");
    await expect(page.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeVisible();
    expect(await problems(w)).toEqual([]);
    await page.goto(`${FLIPPED}/legacy`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("AI Semiconductor Risk Intelligence");
  });

  test("following the navigation stays at the root (no /v2 prefix)", async ({ page }) => {
    const { w } = await open(page, FLIPPED, "/");
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workspace" }).click();
    await expect(page).toHaveURL(`${FLIPPED}/workspace/`);
    expect(await problems(w)).toEqual([]);
  });

  test("an API path that is not a page is still a JSON 404", async ({ request }) => {
    const response = await request.get(`${FLIPPED}/api/nope`, { headers: { accept: "text/html" } });
    expect(response.status()).toBe(404);
    expect(response.headers()["content-type"]).toContain("application/json");
  });
});

import { expect, test } from "@playwright/test";
import { PREVIEW, SITE_KEY } from "./servers.ts";
import { CLOUDFLARE, problems, stubTurnstile, watch, type TurnstileStubState } from "./support.ts";

// R33(3), R8, S5 in a real browser: with a site key the shell loads the Cloudflare script from challenges.cloudflare.com ONLY
// (stubbed at the network layer, so the CSP is exercised for real), renders one widget that a route change leaves alone, and
// without a key nothing third-party is ever requested.

const stub = (page: import("@playwright/test").Page) =>
  page.evaluate(() => {
    const s = (window as unknown as { __turnstileStub: TurnstileStubState }).__turnstileStub;
    return { renders: s.renders, removes: s.removes, resets: s.resets, live: s.live };
  });

test("with a site key one widget is drawn from the Cloudflare script, with zero violations and no other origin", async ({ page }) => {
  const requested = await stubTurnstile(page);
  const w = await watch(page, SITE_KEY, [CLOUDFLARE]);
  await page.goto(`${SITE_KEY}/v2/`);
  await expect(page.locator("[data-widget]")).toHaveCount(1);
  expect(requested).toHaveLength(1);
  expect(requested[0]).toBe("https://challenges.cloudflare.com/turnstile/v0/api.js?onload=semigraphTurnstileReady&render=explicit");
  expect(await problems(w)).toEqual([]);
});

test("a route change does not render, remove or reset the widget, and a token is single-use", async ({ page }) => {
  await stubTurnstile(page);
  await page.goto(`${SITE_KEY}/v2/`);
  await expect(page.locator("[data-widget]")).toHaveCount(1);
  const before = await stub(page);
  await page.getByRole("link", { name: "How it works" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
  expect(await stub(page)).toEqual(before);
  await expect(page.locator("[data-widget]")).toHaveCount(1);
});

test("without a site key no third-party script is requested and no widget exists", async ({ page }) => {
  const requested = await stubTurnstile(page);
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByRole("heading", { level: 1, name: "Ask" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(requested).toEqual([]);
  await expect(page.locator("[data-widget]")).toHaveCount(0);
  expect(await problems(w)).toEqual([]);
});

// Fail closed: a spec on the site-key server that forgets stubTurnstile must not reach the real Cloudflare. playwright.config.ts
// maps the host to nothing in the browser itself, so the script request dies on name resolution (and a stubbed one is fulfilled
// by page.route before any resolution, which the first test above proves).
test("a page that was not stubbed fails to resolve challenges.cloudflare.com instead of loading the real script", async ({ page }) => {
  const failures: string[] = [];
  const succeeded: string[] = [];
  page.on("requestfailed", (request) => {
    if (request.url().startsWith(CLOUDFLARE)) failures.push(request.failure()?.errorText ?? "");
  });
  page.on("requestfinished", (request) => {
    if (request.url().startsWith(CLOUDFLARE)) succeeded.push(request.url());
  });
  await page.goto(`${SITE_KEY}/v2/`);
  await expect.poll(() => failures.length).toBeGreaterThan(0);
  expect(failures[0]).toContain("ERR_NAME_NOT_RESOLVED");
  expect(succeeded).toEqual([]);
  await expect(page.locator("[data-widget]")).toHaveCount(0);
});

import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

// The watcher the other specs lean on must not be vacuous: here it is shown to SEE each kind of problem it reports.

test("the watcher sees the CSP violation a request to another origin causes (connect-src 'self')", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/`);
  await page.evaluate(() => fetch("https://example.invalid/x").catch(() => undefined));
  await expect.poll(async () => (await w.cspViolations()).length).toBeGreaterThan(0);
  expect((await problems(w)).some((p) => p.startsWith("CSP violation: connect-src"))).toBe(true);
});

test("the watcher sees a request to another origin (the CSP blocks it before the network, so a page without one is used)", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto("about:blank");
  await page.evaluate(() => fetch("https://example.invalid/x").catch(() => undefined));
  await expect.poll(() => w.foreignRequests.length).toBeGreaterThan(0);
  expect(await problems(w)).toContain("request to another origin: https://example.invalid/x");
});

test("the watcher sees a console error, a page error and a failed response", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/`);
  await page.evaluate(() => {
    console.error("boom");
    setTimeout(() => {
      throw new Error("late failure");
    }, 0);
  });
  await page.evaluate(() => fetch("/v2/missing.png").then(() => undefined));
  await expect.poll(() => w.pageErrors.length).toBeGreaterThan(0);
  const found = await problems(w);
  expect(found).toContain("console error: boom");
  expect(found.some((p) => p.startsWith("page error: late failure"))).toBe(true);
  expect(found.some((p) => p.startsWith("failed response: 404") && p.endsWith("/v2/missing.png"))).toBe(true);
});

test("a clean page has no problems", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByRole("heading", { level: 1, name: "Ask" })).toBeVisible();
  expect(await problems(w)).toEqual([]);
});

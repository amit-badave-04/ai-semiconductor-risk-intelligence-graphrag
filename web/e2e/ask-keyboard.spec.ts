import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// A1 / A3 / A4 in a real browser: the question box and the strategy select have labels, Ctrl or Cmd with Enter asks, a chip is a
// real button reached by Tab, the status region is a polite live region and the streamed answer is not one, and axe finds nothing
// serious on the page with an answer and with the drawer open. Axe also runs the `label` rule on the controls of the Ask page.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
// The first packaged example is cached from the start and replays the same recorded answer every time (a live answer is cached
// after its first ask, so a second ask of it would not be live).
async function cachedQuestion(page: Page): Promise<string> {
  const first = page.locator("#examples button").first();
  await expect(first).toBeVisible();
  return (await first.innerText()).trim();
}

test("every control of the Ask page is found by its label (A4)", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByLabel("Your question")).toBeVisible();
  await expect(page.getByLabel("Retrieval strategy")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeVisible();
  const results = await new AxeBuilder({ page }).withRules(["label", "select-name"]).analyze();
  expect(results.violations.map((v) => v.id)).toEqual([]);
});

test("Ctrl+Enter asks from the question box and a plain Enter does not", async ({ page }) => {
  let asks = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/ask")) asks += 1;
  });
  await page.goto(`${PREVIEW}/v2/`);
  await page.getByLabel("Your question").fill(await cachedQuestion(page));
  await page.getByLabel("Your question").press("Enter");
  await page.waitForTimeout(200);
  expect(asks).toBe(0);
  await page.getByLabel("Your question").press("Control+Enter");
  await expect(page.getByRole("status")).toHaveText("served from cache \u2014 no model call");
  expect(asks).toBe(1);
});

test("a chip is a real button reached by Tab, the status is a polite live region and the answer is not one", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await page.getByLabel("Your question").fill(await cachedQuestion(page));
  await page.getByLabel("Your question").press("Control+Enter");
  await expect(page.getByRole("status")).toHaveText("served from cache \u2014 no model call");
  const chip = page.locator("#answer button[data-chip-key]").first();
  await expect(chip).toHaveAttribute("type", "button");
  await chip.focus();
  await expect(chip).toBeFocused();
  expect(await chip.evaluate((el) => (el as HTMLButtonElement).tabIndex)).toBeGreaterThanOrEqual(0);
  await expect(page.getByRole("status")).toHaveAttribute("aria-live", "polite");
  expect(await page.locator("#answer").evaluate((el) => !!el.closest("[aria-live]"))).toBe(false);
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`axe on the answered page, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });
    test("no serious or critical violation with an answer, its badges and the drawer open", async ({ page }) => {
      await page.goto(`${PREVIEW}/v2/`);
      await page.getByLabel("Your question").fill(await cachedQuestion(page));
      await page.getByLabel("Your question").press("Control+Enter");
      await expect(page.getByRole("status")).toHaveText("served from cache \u2014 no model call");
      const before = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      await page.locator("#answer button[data-chip-key]").first().click();
      await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
      const open = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      for (const results of [before, open]) {
        const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
        expect(blocking.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      }
    });
  });
}

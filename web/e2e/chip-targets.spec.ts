import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { firstStarter } from "./support.ts";

// docs/specs/m5b-redesign.md section 7 item 4 (R2 review A1): every chip is a target of at least 24 x 24 CSS px, in the answer's
// text and in Sources (N), on a phone and on a wide screen, in both colour schemes.
async function answered(page: Page) {
  await page.goto(`${PREVIEW}/v2/`);
  const question = (await firstStarter(page).innerText()).trim();
  const box = page.getByRole("textbox", { name: "Your question" });
  await box.fill(question);
  await box.press("Control+Enter");
  await expect(page.getByRole("region", { name: /^Sources \(\d+\)$/ })).toBeVisible();
}

for (const scheme of ["light", "dark"] as const) {
  for (const [width, height] of [
    [390, 844],
    [1280, 800],
  ] as const) {
    test(`every chip is at least 24 x 24 px at ${width} x ${height} (${scheme})`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width, height });
      await answered(page);
      const sources = page.getByRole("region", { name: /^Sources \(\d+\)$/ }).locator("[data-chip-key]");
      for (const chips of [page.locator("#answer [data-chip-key]"), sources]) {
        expect(await chips.count()).toBeGreaterThan(0);
        for (const chip of await chips.all()) {
          const b = await chip.boundingBox();
          expect(b?.height ?? 0).toBeGreaterThanOrEqual(24);
          expect(b?.width ?? 0).toBeGreaterThanOrEqual(24);
        }
      }
    });
  }
}

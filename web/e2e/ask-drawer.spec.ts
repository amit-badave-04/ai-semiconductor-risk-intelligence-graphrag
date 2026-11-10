import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { firstStarter, problems, watch } from "./support.ts";

// R24 / A1 / A2 / A5 in a real browser: the evidence drawer is a non-modal complementary landmark named Evidence, closed it is out
// of the tab order and the accessibility tree, open it takes focus on the close button, Escape closes it, a second chip works while
// it is open, focus returns to the chip, and when a new answer replaced the chip it goes to the answer heading. The chips of the
// recorded answers cite I.1A:0001-0040 and the XBRL revenue fact, which the fixture server resolves from hand-written rows.
// The server caches every live answer it gives, so a second live ask of the same question and strategy is served from the cache.
// These specs ask the first packaged example, which is cached from the start and replays the same recorded answer every time.
const box = (page: Page) => page.getByRole("textbox", { name: "Your question" });
const drawer = (page: Page) => page.locator("aside[aria-label='Evidence']");
const chips = (page: Page) => page.locator("#answer [data-chip-key]");

async function answered(page: Page) {
  const first = firstStarter(page);
  await expect(first).toBeVisible();
  const question = (await first.innerText()).trim();
  await box(page).fill(question);
  await box(page).press("Control+Enter");
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  await expect(chips(page).first()).toBeVisible();
}

test("closed it is inert and out of the accessibility tree; open it is a named complementary landmark with the evidence", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/`);
  await expect(drawer(page)).toHaveAttribute("inert", "");
  await expect(drawer(page)).toHaveAttribute("aria-hidden", "true");
  await expect(page.getByRole("complementary", { name: "Evidence" })).toHaveCount(0);
  await answered(page);
  await chips(page).first().click();
  const landmark = page.getByRole("complementary", { name: "Evidence" });
  await expect(landmark).toBeVisible();
  await expect(landmark.getByRole("button", { name: "Close evidence" })).toBeFocused();
  await expect(landmark).toContainText("id: ");
  await expect(landmark).toContainText("Open the filing on SEC.gov"); // RD17 wording
  expect(await problems(w)).toEqual([]);
});

test("Escape closes it and focus returns to the chip that opened it; Enter and Space open a chip", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await answered(page);
  const first = chips(page).first();
  await first.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(drawer(page)).toHaveAttribute("inert", "");
  await expect(first).toBeFocused();
  await page.keyboard.press("Space");
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
});

test("a second chip is clicked while the drawer is open: the page stays operable and the content is the newer one", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await answered(page);
  await chips(page).nth(0).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  const firstId = await chips(page).nth(0).getAttribute("data-id");
  const secondIndex = await page.evaluate((id) => Array.from(document.querySelectorAll("#answer [data-chip-key]")).findIndex((c) => c.getAttribute("data-id") !== id), firstId);
  expect(secondIndex).toBeGreaterThan(0);
  const secondId = await chips(page).nth(secondIndex).getAttribute("data-id");
  await chips(page).nth(secondIndex).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText(`id: ${secondId}`);
  await expect(page.getByRole("complementary", { name: "Evidence" })).not.toContainText(`id: ${firstId}`);
});

test("when a new answer replaced the opener, closing the drawer puts focus on the answer heading, never on the body", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await answered(page);
  await chips(page).first().click();
  await expect(page.getByRole("button", { name: "Close evidence" })).toBeFocused();
  // a new ask while the drawer stays open (Ctrl+Enter asks from the question box only, accessibility review of baba14d)
  await box(page).press("Control+Enter");
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  await expect(chips(page).first()).toBeVisible();
  await page.getByRole("button", { name: "Close evidence" }).click();
  await expect(drawer(page)).toHaveAttribute("inert", "");
  // RD10: the answer is headed by its question, which is now the focus target (ANSWER_HEADING_ID)
  await expect(page.locator("#ask-heading")).toBeFocused();
  await expect(page.locator("#ask-heading")).not.toHaveText("Your question");
});

test("a route change closes the drawer without taking focus back: the new page's h1 has it", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await answered(page);
  await chips(page).first().click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  await page.getByRole("link", { name: "How it works" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
  await expect(drawer(page)).toHaveAttribute("inert", "");
});

test("a chip id that is not in the grammar makes no evidence request", async ({ page }) => {
  const evidence: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/evidence/")) evidence.push(request.url());
  });
  await page.goto(`${PREVIEW}/v2/`);
  await answered(page);
  await chips(page).first().click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText("id: ");
  expect(evidence).toHaveLength(1);
  expect(evidence[0]).toMatch(/\/api\/evidence\/[0-9A-Za-z%.\-_]+$/);
});

import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD19 and RD25 in a real browser: "Ask about <company>" opens Ask with the approved question in the
// box and sends nothing, and a reload does not fill the box again; "Compare filings" focuses the risk changes; both are 24 px
// targets; the active risks carry the D-R4 label.

test("'Ask about Nvidia' opens Ask with the question in the box, sends nothing, and a reload leaves the box empty", async ({ page }) => {
  const asks: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/ask")) asks.push(request.method());
  });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await page.getByRole("link", { name: "Ask about Nvidia" }).click();
  // the router writes Ask as the basename, as the brand link does
  await expect.poll(() => new URL(page.url()).pathname).toMatch(/^\/v2\/?$/);
  const box = page.getByRole("textbox", { name: "Your question" });
  await expect(box).toHaveValue("What risks does Nvidia describe in its latest annual report?");
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Your question" })).toHaveValue("");
  expect(asks).toEqual([]);
});

test("'Compare filings' focuses the risk changes; both actions are 24 px targets", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  for (const name of ["Ask about Nvidia", "Compare filings"]) {
    const box = await page.getByRole("link", { name }).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(24);
  }
  await page.getByRole("link", { name: "Compare filings" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Compared risk disclosures" })).toBeFocused();
});

test("the active risks say they are summaries written by semigraph", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await expect(page.getByRole("region", { name: "Active risks" })).toContainText("Summaries written by semigraph, not quotes");
});

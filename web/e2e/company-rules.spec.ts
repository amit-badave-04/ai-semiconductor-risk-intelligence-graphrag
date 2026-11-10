import { expect, test } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// docs/specs/m5b-redesign.md RD23, RD24 and D-R3 in a real browser, over the fixture server's NVDA, TSM and ASML: stacked rule
// stamps keep a 24 px target and their border and words in forced colours; ASML (no rule linked) shows the owner's sentence;
// relationships name no relation, link the companies that have a page and quote the filing.

test("ASML shows the D-R3 sentence and no rule", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/ASML/`);
  const rules = page.getByRole("region", { name: "Government rules" });
  await expect(rules).toContainText("No export-control rule in our data was linked to ASML by keyword matching. This is not a finding that none applies.");
  await expect(rules.getByRole("listitem")).toHaveCount(0);
});

test("stacked rule stamps are 24 px targets with space between them, and keep their border and words in forced colours", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const stamps = page.getByRole("region", { name: "Government rules" }).getByRole("button");
  await expect(stamps).toHaveCount(2);
  const boxes = [];
  for (const stamp of await stamps.all()) {
    const box = await stamp.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    expect(await stamp.evaluate((el) => getComputedStyle(el).borderTopStyle)).not.toBe("none");
    await expect(stamp).toContainText("Government rule");
    boxes.push(box);
  }
  expect((boxes[1]?.y ?? 0) - ((boxes[0]?.y ?? 0) + (boxes[0]?.height ?? 0))).toBeGreaterThanOrEqual(4);
});

test("relationships name no relation, link a covered company and quote the filing", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  const relations = page.getByRole("region", { name: "Relationships" });
  await expect(relations).not.toContainText(/DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH|SUPPLIES_TO/);
  await expect(relations.getByRole("link", { name: "TSMC" })).toHaveAttribute("href", "/v2/company/TSM/");
  await expect(relations.getByText("SK Hynix")).toBeVisible();
  await expect(relations.getByRole("link", { name: "SK Hynix" })).toHaveCount(0);
  await expect(relations.locator("blockquote").first()).toContainText("Fixture quote");
  await expect(relations.getByRole("heading", { level: 3, name: "Competitors named in its filings" })).toBeVisible();
});

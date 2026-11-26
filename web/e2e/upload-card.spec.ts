import { readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// M2 R19 (docs/specs/m2-search.md, decision 009) in a real browser: the upload card, driven by the recorded job events
// (tests/data/sse/job_ready.sse: received ... keyword_ready, embedding, ready). The upload POST and the job stream are answered by
// the test itself (page.route), so this spec never takes the fixture server's single upload slot, which workspace.spec.ts relies
// on; only the workspace create goes to the server.
const WORKSPACE = `${PREVIEW}/v2/workspace/`;
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const JOB_ID = "00112233445566ff";
const SSE = readFileSync(path.resolve(import.meta.dirname, "../../tests/data/sse/job_ready.sse"), "utf-8");
const FRAMES = SSE.split("\n\n").filter((frame) => frame !== "");
/** The first `count` frames of the recorded stream, as the wire has them. */
const frames = (count: number) => `${FRAMES.slice(0, count).join("\n\n")}\n\n`;
const file = (name: string) => ({ name, mimeType: "text/plain", buffer: Buffer.from("fixture text") });

async function stubUpload(page: Page, jobStream: string) {
  await page.route(/\/api\/workspace\/[0-9a-f]{32}\/documents$/, (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ job_id: JOB_ID, document_id: "0123456789ab", version: 1 }),
        })
      : route.fallback(),
  );
  await page.route(/\/api\/workspace\/[0-9a-f]{32}\/jobs\/[0-9a-f]{16}$/, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: jobStream }),
  );
}

async function openWorkspace(page: Page) {
  await page.goto(WORKSPACE);
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
}

test("an upload shows what was found, keyword search ready and the end summary, with zero violations", async ({ page }) => {
  await stubUpload(page, frames(FRAMES.length));
  await openWorkspace(page);
  await page.locator('input[type="file"]').setInputFiles(file("memo.txt"));
  const card = page.locator("[data-job-card]");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Form: not recognised. You can still ask about this file.");
  await expect(card).toContainText("12 passages to search");
  await expect(card).toContainText("Keyword search is ready. You can ask questions now.");
  await expect(card).toContainText("All 12 passages can be searched by keyword and by meaning.");
  await expect(card.getByRole("button")).toHaveCount(0);
  const result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(result.violations).toEqual([]);
});

test("while search by meaning is prepared the card shows progress and the stop button posts the cancel", async ({ page }) => {
  // the stream ends at the second embedding event (the client re-reads it): the card stays in its running state
  await stubUpload(page, frames(9));
  const cancels: string[] = [];
  await page.route(/\/api\/workspace\/[0-9a-f]{32}\/jobs\/[0-9a-f]{16}\/cancel$/, (route) => {
    cancels.push(route.request().headers()["x-workspace-token"] ?? "");
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ cancelled: true }) });
  });
  await openWorkspace(page);
  await page.locator('input[type="file"]').setInputFiles(file("big.txt"));
  const card = page.locator("[data-job-card]");
  await expect(card).toContainText("Preparing search by meaning: 6 of 12 passages");
  await expect(card).toContainText("Search by meaning: less than a minute left (estimate)");
  await card.getByRole("button", { name: "Stop preparing search by meaning" }).click();
  await expect.poll(() => cancels.length).toBe(1);
  const token = await page.evaluate(() => JSON.parse(window.sessionStorage.getItem("semigraph:workspace") ?? "{}").token as string);
  expect(token.length).toBeGreaterThan(8);
  expect(cancels[0]).toBe(token);
});

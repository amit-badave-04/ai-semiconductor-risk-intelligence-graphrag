import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// A10 (docs/specs/m5b-frontend.md:623): error and refusal text is announced through the A3 status region and sits next to the
// control that caused it. Every refusal of the recorded subset (tests/data/sse/refusals.json: the real routes' status, detail and
// Retry-After, over fakes) is replayed at the network layer, so the page meets the exact bytes the server sends.
//   * The nine POST /api/ask refusals: the detail lands in the Ask card's status region (<output>, role status, polite), and that
//     region's container also holds the Ask button.
//   * job_not_found (GET of a job's stream): the page does not show the server's detail for a job that is gone; it shows its own
//     words for it (CANNOT_WATCH_TEXT of lib/jobs.ts, the legacy page's). That text lands in the upload's row and in the workspace
//     status region, whose container also holds the file input. The upload POST is answered here too, so this spec never takes the
//     fixture server's one upload slot (workspace.spec.ts runs its uploads one after another on it).

const repoRoot = process.env.REPO_ROOT ?? path.resolve(import.meta.dirname, "..", "..");

interface Refusal {
  name: string;
  request: Record<string, unknown>;
  status: number;
  detail: string;
  retry_after: string | null;
}

const RECORDED = JSON.parse(readFileSync(path.join(repoRoot, "tests", "data", "sse", "refusals.json"), "utf-8")) as { refusals: Refusal[] };
const ASK_REFUSALS = RECORDED.refusals.filter((r) => r.request.POST === "/api/ask");
const JOB_REFUSALS = RECORDED.refusals.filter((r) => typeof r.request.GET === "string" && String(r.request.GET).includes("/jobs/"));
/** What the page says for a job whose stream is gone (lib/jobs.ts CANNOT_WATCH_TEXT, worded by the deck's jobs.cannotWatch since R4;
 *  the first test pins the three together). */
const CANNOT_WATCH_TEXT = "could not watch this upload";
const JOBS_SOURCE = readFileSync(path.join(repoRoot, "web", "src", "lib", "jobs.ts"), "utf-8");
const DECK_SOURCE = readFileSync(path.join(repoRoot, "web", "src", "copy", "deck.ts"), "utf-8");
const FAKE_JOB_ID = "0123456789abcdef";

function fulfilment(refusal: Refusal) {
  const headers: Record<string, string> = {};
  if (refusal.retry_after !== null) headers["Retry-After"] = refusal.retry_after;
  return { status: refusal.status, contentType: "application/json", headers, body: JSON.stringify({ detail: refusal.detail }) };
}

/**
 * A10's "next to its control", bounded: the smallest element that holds both the message and its control lies inside the page
 * content and does not hold the page's h1. A message moved up to page level (a toast on <body>, a line in <main> or the layout)
 * has the page itself as that element, and fails.
 */
async function nextToItsControl(message: Locator, control: Locator): Promise<boolean> {
  const handle = await control.elementHandle();
  if (handle === null) return false;
  return message.evaluate((el, target) => {
    let shared: Element | null = el.parentElement;
    while (shared !== null && !shared.contains(target)) shared = shared.parentElement;
    const h1 = document.querySelector("main h1");
    return shared !== null && h1 !== null && !shared.contains(h1) && (document.querySelector("main")?.contains(shared) ?? false);
  }, handle);
}

/** Whether `first` comes before `second` in document order. */
async function precedes(first: Locator, second: Locator): Promise<boolean> {
  const handle = await second.elementHandle();
  if (handle === null) return false;
  return first.evaluate((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0, handle);
}

test("the recorded refusals: nine for POST /api/ask and the gone job", () => {
  expect(ASK_REFUSALS.map((r) => r.name)).toEqual([
    "bad_strategy",
    "as_of_without_workspace",
    "bot_check_failed",
    "workspace_not_found",
    "rate_limited",
    "busy",
    "paused",
    "retrieval_only",
    "draining",
  ]);
  expect(JOB_REFUSALS.map((r) => r.name)).toEqual(["job_not_found"]);
  expect(ASK_REFUSALS.length + JOB_REFUSALS.length).toBe(RECORDED.refusals.length);
  expect(JOBS_SOURCE).toContain("export const CANNOT_WATCH_TEXT = deck.jobs.cannotWatch;");
  expect(DECK_SOURCE).toContain(`cannotWatch: ${JSON.stringify(CANNOT_WATCH_TEXT)},`);
});

async function askAndMeet(page: Page, refusal: Refusal): Promise<void> {
  await page.route("**/api/ask", (route) => route.fulfill(fulfilment(refusal)));
  await page.goto(`${PREVIEW}/v2/`);
  await page.getByRole("textbox", { name: "Your question" }).fill("A question the server refuses.");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
}

for (const refusal of ASK_REFUSALS) {
  test(`${refusal.name} (${refusal.status}): announced in the Ask card's status region, next to the Ask button`, async ({ page }) => {
    await askAndMeet(page, refusal);
    const askCard = page.locator("section", { has: page.getByRole("button", { name: "Ask", exact: true }) });
    const region = askCard.getByRole("status");
    await expect(region).toContainText(refusal.detail);
    await expect(region).toHaveAttribute("aria-live", "polite");
    const button = askCard.getByRole("button", { name: "Ask", exact: true });
    await expect(button).toBeEnabled(); // the refusal ended the ask: the control that caused it is usable again
    expect(await nextToItsControl(region, button)).toBe(true);
    // under the controls and above the answer: the line the button leads to, not a line after the answer
    expect(await precedes(button, region)).toBe(true);
    expect(await precedes(region, page.locator("#answer"))).toBe(true); // the answer has its own section after the card (RD10)
    // exactly one region says it: the error is not repeated elsewhere on the page
    await expect(page.getByRole("status").filter({ hasText: refusal.detail })).toHaveCount(1);
  });
}

for (const refusal of JOB_REFUSALS) {
  test(`${refusal.name} (${refusal.status}): the upload's row and the workspace status region say it, next to the file input`, async ({ page }) => {
    await page.route("**/api/workspace/*/documents", (route) =>
      route.request().method() === "POST"
        ? route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ job_id: FAKE_JOB_ID }) })
        : route.continue(),
    );
    let jobGets = 0;
    await page.route(`**/api/workspace/*/jobs/${FAKE_JOB_ID}`, (route) => {
      jobGets += 1;
      return route.fulfill(fulfilment(refusal));
    });
    await page.goto(`${PREVIEW}/v2/workspace/`);
    await page.getByRole("button", { name: "Create a workspace" }).click();
    await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
    const input = page.locator('input[type="file"]');
    await input.setInputFiles({ name: "gone.txt", mimeType: "text/plain", buffer: Buffer.from("fixture text") });
    const row = page.getByRole("listitem").filter({ hasText: "gone.txt" }).first();
    await expect(row).toContainText(CANNOT_WATCH_TEXT);
    const region = page.getByRole("status").filter({ hasText: `gone.txt: ${CANNOT_WATCH_TEXT}` });
    await expect(region).toHaveCount(1);
    expect(jobGets).toBeGreaterThanOrEqual(1); // the recorded 404 is what the watcher met
    await expect(page.getByText(refusal.detail, { exact: true })).toHaveCount(0); // the page's own words, not the server's
    expect(await nextToItsControl(region, input)).toBe(true);
    expect(await nextToItsControl(row, input)).toBe(true);
  });
}

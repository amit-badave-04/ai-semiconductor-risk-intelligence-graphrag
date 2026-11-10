import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW, SITE_KEY } from "./servers.ts";
import { CLOUDFLARE, problems, stubTurnstile, watch, type TurnstileStubState } from "./support.ts";

// R22 / R31 / R33(1) / S12 in a real browser, over the fixture server (the real routers and the real built app, with fakes for the
// model; no LLM, no Neo4j): a cached example, a live answer with its checks, an escalation, errors, the agent's steps, a refusal, a
// route change in the middle of a stream, and the bot-check gate. `fixture_replay.QUESTIONS` selects the recorded scenario by the
// question text. The preview server runs with --agent; the site-key server answers a dummy key and a stubbed bot check.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const QUESTIONS = {
  live: "Fixture question: a live hybrid answer",
  escalated: "Fixture question: an escalated answer",
  errorBefore: "Fixture question: an error before any text",
  errorAfter: "Fixture question: an error after some text",
};

const box = (page: Page) => page.getByRole("textbox", { name: "Your question" });
const askButton = (page: Page) => page.getByRole("button", { name: "Ask", exact: true });
const status = (page: Page) => page.getByRole("status");

/** Count the POSTs to /api/ask a page makes. */
function countAsks(page: Page): { count: () => number } {
  let n = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/ask") n += 1;
  });
  return { count: () => n };
}

async function ask(page: Page, question: string) {
  await box(page).fill(question);
  await askButton(page).click();
}

/** The console error Chromium logs for a refusal on purpose (one failed response). Nothing else is filtered. */
const withoutRefusals = (list: string[]) => list.filter((p) => !p.startsWith("failed response: 403") && !(p.startsWith("console error: Failed to load resource") && p.includes("403")));

test.describe("on the preview server", () => {
  test("a cached example sends exactly one POST, shows the answer and its chips, with zero violations and one origin", async ({ page }) => {
    const w = await watch(page, PREVIEW);
    const asks = countAsks(page);
    await page.goto(`${PREVIEW}/v2/`);
    await expect(page.getByRole("heading", { level: 1, name: "Ask" })).toBeVisible();
    await page.locator("#examples button").first().click();
    await expect(status(page)).toHaveText("served from cache \u2014 no model call");
    expect(asks.count()).toBe(1);
    await expect(page.locator("#answer [data-chip-key]").first()).toBeVisible();
    await expect(askButton(page)).toBeEnabled();
    expect(await problems(w)).toEqual([]);
  });

  test("loading the page sends no POST at all, whatever the shell does", async ({ page }) => {
    const posts: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(request.url());
    });
    await page.goto(`${PREVIEW}/v2/`);
    await expect(page.locator("#examples button").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(posts).toEqual([]);
  });

  test("a live answer shows the badges and the checks, and the status says done", async ({ page }) => {
    const w = await watch(page, PREVIEW);
    await page.goto(`${PREVIEW}/v2/`);
    await ask(page, QUESTIONS.live);
    await expect(status(page)).toHaveText("done");
    const meta = page.locator("#meta");
    await expect(meta).toContainText(/\d+ citations?/);
    await expect(meta).toContainText("do not prove");
    await expect(page.locator("#answer").locator("[data-chip-key]").first()).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").flatMap((v) => v.nodes.map((n) => `${v.id}: ${n.target.join(" ")} ${n.any.map((a) => a.message).join(" ")}`))).toEqual([]);
    expect(await problems(w)).toEqual([]);
  });

  test.describe("dark scheme", () => {
    test.use({ colorScheme: "dark" });
    test("a live answer with its badges and warnings has no serious axe violation", async ({ page }) => {
      await page.goto(`${PREVIEW}/v2/`);
      await page.getByRole("combobox", { name: "Retrieval strategy" }).selectOption("vector");
      await ask(page, QUESTIONS.escalated); // a key of its own: the server caches a live answer after its first ask
      await expect(status(page)).toHaveText("done");
      await expect(page.locator("#meta")).toContainText("escalated after a failed check");
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").flatMap((v) => v.nodes.map((n) => `${v.id}: ${n.target.join(" ")} ${n.any.map((a) => a.message).join(" ")}`))).toEqual([]);
    });
  });

  test("an escalated answer names the failed checks", async ({ page }) => {
    await page.goto(`${PREVIEW}/v2/`);
    await ask(page, QUESTIONS.escalated);
    await expect(status(page)).toHaveText("done");
    await expect(page.locator("#meta")).toContainText("escalated after a failed check");
  });

  test("an error before any text is the status, in the error colour, and Ask is enabled again", async ({ page }) => {
    await page.goto(`${PREVIEW}/v2/`);
    await ask(page, QUESTIONS.errorBefore);
    await expect(askButton(page)).toBeEnabled();
    await expect(status(page)).not.toHaveText("");
    await expect(status(page)).not.toHaveText("done");
    await expect(status(page)).toHaveClass(/destructive/);
    await expect(page.locator("#meta")).toHaveCount(0);
  });

  test("an error after some text keeps the text and shows the error", async ({ page }) => {
    await page.goto(`${PREVIEW}/v2/`);
    await ask(page, QUESTIONS.errorAfter);
    await expect(status(page)).toHaveClass(/destructive/);
    await expect(page.locator("#answer")).not.toHaveText("");
  });

  test("the agent strategy is offered, and its answer shows the steps timeline", async ({ page }) => {
    await page.goto(`${PREVIEW}/v2/`);
    await page.getByRole("combobox", { name: "Retrieval strategy" }).selectOption("agent");
    await expect(page.getByText(/planning model/)).toBeVisible();
    await ask(page, QUESTIONS.live);
    await expect(status(page)).toHaveText("done");
    await expect(page.getByRole("list", { name: "What the research agent did" }).getByRole("listitem").first()).toBeVisible();
  });

  test("a route change in the middle of a stream does not abort it and loses nothing", async ({ page }) => {
    // (the server caches a live answer after its first ask: this spec asks the question on the vector strategy, a key of its own)
    const asks = countAsks(page);
    const failed: string[] = [];
    page.on("requestfailed", (request) => failed.push(`${request.url()} ${request.failure()?.errorText ?? ""}`));
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/ask", async (route) => {
      await gate;
      await route.continue();
    });
    await page.goto(`${PREVIEW}/v2/`);
    await page.getByRole("combobox", { name: "Retrieval strategy" }).selectOption("vector");
    await ask(page, QUESTIONS.live);
    await expect(askButton(page)).toBeDisabled();
    await page.getByRole("link", { name: "How it works" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
    release();
    await page.getByRole("link", { name: "Ask", exact: true }).click();
    await expect(status(page)).toHaveText("done");
    await expect(page.locator("#answer [data-chip-key]").first()).toBeVisible();
    expect(asks.count()).toBe(1);
    // After the terminal event the client lets the connection go (sse.ts releases the reader), which Chromium reports as ERR_ABORTED
    // for a body that was still open. Nothing else may fail, and the status above was done, never "connection lost".
    expect(failed.filter((f) => !f.startsWith(`${PREVIEW}/api/ask `) || !f.endsWith("net::ERR_ABORTED"))).toEqual([]);
    expect(failed.length).toBeLessThanOrEqual(1);
  });
});

test.describe("on the site-key server (R33(1): an ask is never held back for a bot-check token)", () => {
  const stub = (page: Page) =>
    page.evaluate(() => {
      const s = (window as unknown as { __turnstileStub: TurnstileStubState }).__turnstileStub;
      return { renders: s.renders, resets: s.resets, live: s.live };
    });

  test("a cached example with no token is sent at once and answered", async ({ page }) => {
    await stubTurnstile(page);
    const w = await watch(page, SITE_KEY, [CLOUDFLARE]);
    const asks = countAsks(page);
    await page.goto(`${SITE_KEY}/v2/`);
    await expect(page.locator("[data-widget]")).toHaveCount(1);
    await page.locator("#examples button").first().click();
    await expect(status(page)).toHaveText("served from cache \u2014 no model call");
    expect(asks.count()).toBe(1);
    expect(await problems(w)).toEqual([]);
  });

  test("a live question with no token reaches the server, and its 403 shows the clause", async ({ page }) => {
    // (vector: the "with a token" spec below caches the same question on hybrid, and a cached answer needs no token)
    await stubTurnstile(page);
    const w = await watch(page, SITE_KEY, [CLOUDFLARE]);
    const asks = countAsks(page);
    await page.goto(`${SITE_KEY}/v2/`);
    await expect(page.locator("[data-widget]")).toHaveCount(1);
    await page.getByRole("combobox", { name: "Retrieval strategy" }).selectOption("vector");
    await ask(page, QUESTIONS.live);
    await expect(status(page)).toContainText("(the bot check below has not completed yet");
    await expect(status(page)).toHaveClass(/destructive/);
    expect(asks.count()).toBe(1);
    expect(withoutRefusals(await problems(w))).toEqual([]);
  });

  test("with a token the live question is answered, and the widget is reset once the token is used", async ({ page }) => {
    await stubTurnstile(page);
    await page.goto(`${SITE_KEY}/v2/`);
    await expect(page.locator("[data-widget]")).toHaveCount(1);
    await page.evaluate(() => (window as unknown as { __turnstileStub: TurnstileStubState }).__turnstileStub.solve());
    const before = await stub(page);
    await ask(page, QUESTIONS.live);
    await expect(status(page)).toHaveText("done");
    const after = await stub(page);
    expect(after.resets).toBe(before.resets + 1);
    expect(after.live).toBe(1);
    expect(after.renders).toBe(before.renders);
  });
});

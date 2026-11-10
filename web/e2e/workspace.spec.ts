import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW, SITE_KEY } from "./servers.ts";
import { CLOUDFLARE, problems, stubTurnstile, watch, type TurnstileStubState } from "./support.ts";

// R28, R33(2), R34, A3, A4, A6, S8, S12 in a real browser: the workspace page over the fixture server (the real routers over an
// in-memory workspace, a job stream that replays the recorded job events). The fixture server has ONE upload slot, so the specs
// of this file run one after another (they share a server with no other upload spec).
test.describe.configure({ mode: "serial" });

const STORAGE_KEY = "semigraph:workspace";
const DROP_TEXT = "Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one (up to about 20 pages).";
const WORKSPACE = `${PREVIEW}/v2/workspace/`;
const SITE_KEY_WORKSPACE = `${SITE_KEY}/v2/workspace/`;
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

interface Tracked {
  documentPosts: string[];
  workspacePosts: string[];
  jobGets: string[];
  uploadTokens: (string | undefined)[];
  console: string[];
}

/** Count what the page sends: every upload POST, every job watcher GET, every workspace create, every console line. */
function track(page: Page): Tracked {
  const seen: Tracked = { documentPosts: [], workspacePosts: [], jobGets: [], uploadTokens: [], console: [] };
  page.on("console", (message) => seen.console.push(message.text()));
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (request.method() === "POST" && /^\/api\/workspace\/[0-9a-f]{32}\/documents$/.test(pathname)) {
      seen.documentPosts.push(pathname);
      seen.uploadTokens.push(request.headers()["x-turnstile-token"]);
    }
    if (request.method() === "POST" && pathname === "/api/workspace") seen.workspacePosts.push(pathname);
    if (request.method() === "GET" && /^\/api\/workspace\/[0-9a-f]{32}\/jobs\/[0-9a-f]{16}$/.test(pathname)) seen.jobGets.push(pathname);
  });
  return seen;
}

const file = (name: string, text = "fixture text") => ({ name, mimeType: "text/plain", buffer: Buffer.from(text) });
const chooser = (page: Page) => page.locator('input[type="file"]');
const dropZone = (page: Page) => page.getByRole("button", { name: DROP_TEXT });

async function createWorkspace(page: Page) {
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
}

async function storedToken(page: Page): Promise<string> {
  return page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "{}").token as string, STORAGE_KEY);
}

test("create, upload, a second version, what changed, a chip's drawer and delete, with zero violations", async ({ page }) => {
  const w = await watch(page, PREVIEW);
  await page.goto(WORKSPACE);
  await expect(page.getByRole("heading", { level: 2, name: "Your documents" })).toBeVisible();
  await createWorkspace(page);
  await expect(page.getByText("No documents uploaded yet.")).toBeVisible();

  await chooser(page).setInputFiles(file("memo.txt"));
  const row = page.getByRole("listitem").filter({ hasText: "memo.txt" }).first();
  await expect(row).toContainText("ready");
  await expect(page.getByText("memo.txt (latest v1)")).toBeVisible();

  await page.getByLabel("upload as:").selectOption({ label: "New version of memo.txt" });
  await chooser(page).setInputFiles(file("memo.txt", "second version"));
  await expect(page.getByText("memo.txt (latest v2)")).toBeVisible();
  // the fixture reports every older version's status as "ready"
  await expect(page.getByText(/^v1 \(\w+\) \u2192 v2 \(current\)$/)).toBeVisible();

  await page.getByRole("button", { name: "memo.txt: v2" }).click();
  await expect(page.getByText("v1 \u2192 v2, 1 section(s) unchanged:")).toBeVisible();
  const chip = page.locator('[data-chip-key^="changes:"]').first();
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  await page.keyboard.press("Escape"); // the drawer is non-modal but covers the page's right edge
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeHidden();
  await expect(chip).toBeFocused(); // focus went back to the chip (A2)

  await page.getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeVisible();
  expect(await page.evaluate((key) => window.sessionStorage.getItem(key), STORAGE_KEY)).toBeNull();
  expect(await problems(w)).toEqual([]);
});

test("the token is in the header and the session storage only: never in the page, the URL, the title or the console", async ({ page }) => {
  const seen = track(page);
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  const token = await storedToken(page);
  expect(token.length).toBeGreaterThan(8);
  const stored = await page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "{}"), STORAGE_KEY);
  expect(Object.keys(stored)).toEqual(["id", "token", "expires_at"]);
  expect(await page.evaluate(() => window.localStorage.length)).toBe(0);
  expect(await page.evaluate(() => document.cookie)).toBe("");
  await chooser(page).setInputFiles(file("note.txt"));
  await expect(page.getByRole("listitem").filter({ hasText: "note.txt" }).first()).toContainText("ready");
  const page_ = await page.evaluate(() => ({ html: document.documentElement.outerHTML, text: document.body.innerText, title: document.title, url: location.href }));
  for (const [where, value] of Object.entries(page_)) expect(value, where).not.toContain(token);
  expect(seen.console.join("\n")).not.toContain(token);
  expect(seen.jobGets.join("\n")).not.toContain(token);
});

test("a reload restores the workspace with a GET only: no create, no upload, no watcher", async ({ page }) => {
  const seen = track(page);
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  await chooser(page).setInputFiles(file("kept.txt"));
  await expect(page.getByText("kept.txt (latest v1)")).toBeVisible();
  const before = { creates: seen.workspacePosts.length, uploads: seen.documentPosts.length, watchers: seen.jobGets.length };
  await page.reload();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
  await expect(page.getByText("kept.txt (latest v1)")).toBeVisible();
  expect({ creates: seen.workspacePosts.length, uploads: seen.documentPosts.length, watchers: seen.jobGets.length }).toEqual(before);
});

test("a multi-file drop with a route change in the middle: one POST per file, one watcher per job, the queue goes on", async ({ page }) => {
  const seen = track(page);
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  const dataTransfer = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["alpha"], "first.txt", { type: "text/plain" }));
    transfer.items.add(new File(["beta"], "second.txt", { type: "text/plain" }));
    return transfer;
  });
  await dropZone(page).dispatchEvent("drop", { dataTransfer });
  await expect.poll(() => seen.documentPosts.length).toBeGreaterThanOrEqual(1);
  await page.getByRole("link", { name: "How it works" }).click(); // the route changes while the first job runs
  await expect(page.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeFocused();
  await page.getByRole("link", { name: "Workspace" }).click();
  const rows = page.locator("[data-job-row]");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("ready");
  await expect(rows.nth(1)).toContainText("ready");
  expect(seen.documentPosts).toHaveLength(2);
  expect(seen.jobGets).toHaveLength(2);
  expect(new Set(seen.jobGets).size).toBe(2);
  await expect(page.getByText("first.txt (latest v1)", { exact: false })).toBeVisible();
});

test("a failed job shows the server's own message and the next file still uploads", async ({ page }) => {
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  await chooser(page).setInputFiles(file("fail.txt", "fail"));
  const failed = page.getByRole("listitem").filter({ hasText: "fail.txt" }).first();
  await expect(failed).toContainText("failed: ");
  await chooser(page).setInputFiles(file("fine.txt"));
  await expect(page.getByRole("listitem").filter({ hasText: "fine.txt" }).first()).toContainText("ready");
});

test("restore accepts the id and token of a workspace made in this tab, and refuses a wrong token", async ({ page }) => {
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  const stored = await page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "{}") as { id: string; token: string }, STORAGE_KEY);
  await page.getByRole("button", { name: "Delete workspace" }).click();
  // the workspace is gone on the server now: a restore of it says so
  await page.getByRole("button", { name: "I have a workspace id and key" }).click();
  await page.getByLabel("Workspace id").fill(stored.id);
  await page.getByLabel("Workspace key").fill(stored.token);
  await page.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText("workspace not found")).toBeVisible();
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
});

test("the controls are labelled, the drop zone works from the keyboard, and axe finds nothing serious", async ({ page }) => {
  await page.goto(WORKSPACE);
  await page.getByRole("button", { name: "I have a workspace id and key" }).click();
  await expect(page.getByLabel("Workspace id")).toBeVisible();
  await expect(page.getByLabel("Workspace key")).toHaveAttribute("type", "password");
  await createWorkspace(page);
  for (const name of ["Ask as of this date instead of the current version", "upload as:"]) await expect(page.getByLabel(name)).toBeVisible();
  // the drop zone's own text names the hidden file input too
  const zoneId = await dropZone(page).getAttribute("id");
  await expect(chooser(page)).toHaveAttribute("aria-labelledby", zoneId ?? "missing");
  for (const key of ["Enter", "Space"]) {
    await dropZone(page).focus();
    const [picker] = await Promise.all([page.waitForEvent("filechooser"), page.keyboard.press(key)]);
    expect(picker.isMultiple()).toBe(false);
  }
  await chooser(page).setInputFiles(file("a11y.txt"));
  await expect(page.getByText("a11y.txt (latest v1)")).toBeVisible();
  await expect(page.getByRole("combobox", { name: /^a11y\.txt:/ })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  expect(results.violations.filter((v) => v.id === "label")).toEqual([]);
});

// docs/specs/m5b-redesign.md R4 (the open workspace): names come from the uploader. A 300-character name, markup and a right-to-left
// override stay text, each in its own <bdi>, and nothing pushes the page sideways on a phone; the drag cue survives forced colours.
const LONG_NAME = `${"quarterly-risk-report-".repeat(13)}final.txt`;
const MARKUP_NAME = '<img src=x onerror="window.__pwned=1">.txt';
const RLO_NAME = "report‮pdf.txt";

for (const width of [390, 320]) {
  test(`at ${width} px a long, a markup and a right-to-left-override name stay text and nothing scrolls sideways`, async ({ page }) => {
    expect(LONG_NAME.length).toBeGreaterThanOrEqual(290);
    await page.setViewportSize({ width, height: 900 });
    await page.goto(WORKSPACE);
    await createWorkspace(page);
    for (const name of [LONG_NAME, MARKUP_NAME, RLO_NAME]) {
      await chooser(page).setInputFiles(file(name));
      await expect(page.locator("[data-job-row]").filter({ hasText: name }).first()).toContainText("ready");
      // the document's title is the name cut to the server's 120-character cap (lib/jobs.ts MAX_UPLOAD_TITLE_CHARS)
      const title = name.slice(0, 120);
      await expect(page.locator("strong bdi").filter({ hasText: title })).toHaveText(title);
    }
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
    await expect(page.locator("main img")).toHaveCount(0);
    for (const name of [LONG_NAME, MARKUP_NAME, RLO_NAME]) {
      await expect(page.locator("[data-job-row] bdi").filter({ hasText: name }).first()).toHaveText(name);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    for (const select of await page.locator("main select").all()) {
      const box = await select.boundingBox();
      expect(box === null ? 0 : box.x + box.width).toBeLessThanOrEqual(width);
    }
  });
}

test("in forced colours the drag cue is a solid border, not only a colour", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  const style = () => dropZone(page).evaluate((zone) => getComputedStyle(zone).borderTopStyle);
  expect(await style()).toBe("dashed");
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  await dropZone(page).dispatchEvent("dragenter", { dataTransfer });
  await expect(dropZone(page)).toHaveAttribute("data-dragging", "");
  expect(await style()).toBe("solid");
});

// docs/specs/m5b-redesign.md RD26 and docs/decisions/002-r4-workspace-copy.md in a real browser: the first-time view makes no
// workspace request by itself, Restore opens from its disclosure, focus goes to the h2 after create or restore and to Create after
// delete, the key is never in the page after a restore that worked or failed, and the view holds at 320 px and in forced colours.
const TRIGGER = "I have a workspace id and key";
const H2 = "Your documents";

test("the first-time view: no workspace request on load, the steps and types, and focus after create, delete and restore", async ({ page }) => {
  const seen: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/workspace")) seen.push(`${request.method()} ${pathname}`);
  });
  const console_: string[] = [];
  page.on("console", (message) => console_.push(message.text()));
  await page.goto(WORKSPACE);
  await expect(page.getByRole("heading", { level: 2, name: H2 })).toBeVisible();
  await expect(page.locator("main ol > li")).toHaveText(["Create a workspace.", "Upload a document.", "Ask a question about it on the Ask page."]);
  expect(seen).toEqual([]);

  await createWorkspace(page);
  await expect(page.getByRole("heading", { level: 2, name: H2 })).toBeFocused();
  const stored = await page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "{}") as { id: string; token: string }, STORAGE_KEY);
  // a restore that works: the session is still stored, so put it back after a delete
  await page.getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeFocused();

  const trigger = page.getByRole("button", { name: TRIGGER });
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByLabel("Workspace key")).toHaveCount(0);
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await page.getByLabel("Workspace id").fill(stored.id);
  await page.getByLabel("Workspace key").fill(stored.token);
  await page.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText("workspace not found")).toBeVisible(); // deleted on the server, so the restore is refused
  await expect(page.getByRole("button", { name: "Restore" })).toBeFocused(); // focus stays in the panel
  await trigger.click(); // collapsing keeps the message, which lives outside the disclosure
  await expect(page.getByRole("status").filter({ hasText: "workspace not found" })).toBeVisible();
  const failed = await page.evaluate(() => ({ html: document.documentElement.outerHTML, text: document.body.innerText, url: location.href }));
  for (const [where, value] of Object.entries(failed)) expect(value, where).not.toContain(stored.token);
  expect(console_.join("\n")).not.toContain(stored.token);
});

test("a restore that works lands focus on the h2 and leaves the key out of the page", async ({ page }) => {
  await page.goto(WORKSPACE);
  await createWorkspace(page);
  const stored = await page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "{}") as { id: string; token: string }, STORAGE_KEY);
  await page.evaluate((key) => window.sessionStorage.removeItem(key), STORAGE_KEY);
  await page.reload(); // the workspace lives on the server; this tab no longer holds it
  await page.getByRole("button", { name: TRIGGER }).click();
  await page.getByLabel("Workspace id").fill(stored.id);
  await page.getByLabel("Workspace key").fill(stored.token);
  await page.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: H2 })).toBeFocused();
  const after = await page.evaluate(() => ({ html: document.documentElement.outerHTML, text: document.body.innerText, title: document.title, url: location.href }));
  for (const [where, value] of Object.entries(after)) expect(value, where).not.toContain(stored.token);
  await page.getByRole("button", { name: "Delete workspace" }).click();
});

test("at 320 px and in forced colours the first-time view fits, its targets are at least 24 px and the chevron shows", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto(WORKSPACE);
  await page.getByRole("button", { name: TRIGGER }).click();
  await expect(page.getByLabel("Workspace key")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  for (const control of [page.getByRole("button", { name: TRIGGER }), page.getByRole("button", { name: "Create a workspace" }), page.getByRole("button", { name: "Restore" })]) {
    const box = await control.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(24);
  }
  const chevron = page.getByRole("button", { name: TRIGGER }).locator("svg");
  await expect(chevron).toBeVisible();
  expect(await chevron.evaluate((svg) => getComputedStyle(svg).color)).not.toBe("rgba(0, 0, 0, 0)");
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});

test.describe("with a site key (R33(2)): an upload waits for the bot-check token", () => {
  const stub = (page: Page) =>
    page.evaluate(() => {
      const s = (window as unknown as { __turnstileStub: TurnstileStubState }).__turnstileStub;
      return { resets: s.resets, live: s.live };
    });
  const solve = (page: Page) => page.evaluate(() => (window as unknown as { __turnstileStub: TurnstileStubState }).__turnstileStub.solve());

  test("it sends the token the widget gives, uses it up and resets the widget; a second file waits for a fresh one", async ({ page }) => {
    await stubTurnstile(page);
    const seen = track(page);
    const w = await watch(page, SITE_KEY, [CLOUDFLARE]);
    await page.goto(SITE_KEY_WORKSPACE);
    await expect(page.locator("[data-widget]")).toHaveCount(1);
    await createWorkspace(page); // sent at once, with no token (the fixture's workspace routes need none)
    expect(seen.workspacePosts).toHaveLength(1);
    const resets = (await stub(page)).resets;
    await chooser(page).setInputFiles(file("keyed.txt"));
    await expect(page.getByRole("listitem").filter({ hasText: "keyed.txt" }).first()).toContainText("uploading\u2026");
    expect(seen.documentPosts).toHaveLength(0); // waiting: nothing is sent without a token
    const token = await solve(page);
    await expect(page.getByRole("listitem").filter({ hasText: "keyed.txt" }).first()).toContainText("ready");
    expect(seen.uploadTokens).toEqual([token]);
    expect((await stub(page)).resets).toBeGreaterThan(resets); // used up, so the widget was reset
    expect((await stub(page)).live).toBe(1);
    expect(await problems(w)).toEqual([]);
  });

  test("with no token it waits at most 15 seconds, says so, sends nothing, and the next file gets its turn", async ({ page }) => {
    test.setTimeout(60_000);
    await stubTurnstile(page);
    const seen = track(page);
    await page.goto(SITE_KEY_WORKSPACE);
    await expect(page.locator("[data-widget]")).toHaveCount(1);
    await createWorkspace(page);
    const started = Date.now();
    await chooser(page).setInputFiles(file("never.txt"));
    const row = page.getByRole("listitem").filter({ hasText: "never.txt" }).first();
    await expect(row).toContainText("bot check did not complete in time \u2014 try again", { timeout: 20_000 });
    const waited = Date.now() - started;
    expect(waited).toBeGreaterThanOrEqual(14_000);
    expect(waited).toBeLessThanOrEqual(17_500);
    expect(seen.documentPosts).toHaveLength(0);
  });
});

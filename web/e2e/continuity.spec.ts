import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { problems, watch } from "./support.ts";

// R32 in a real browser: the session is {id, token, expires_at} under "semigraph:workspace" in sessionStorage, so a workspace made
// on /legacy is restored on /v2/ in the same tab, and the reverse. A restore on load is a GET only. No upload happens here (the
// fixture server has one upload slot, and workspace.spec.ts owns it).

const KEY = "semigraph:workspace";

interface Stored {
  id: string;
  token: string;
  expires_at: string | null;
}

const stored = (page: Page) => page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) ?? "null") as Stored | null, KEY);

function writes(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (request.method() !== "GET" && pathname.startsWith("/api/workspace")) seen.push(`${request.method()} ${pathname}`);
  });
  return seen;
}

test("a workspace made on the legacy page is restored on /v2/ in the same tab, with a GET only", async ({ page }) => {
  const sent = writes(page);
  await page.goto(`${PREVIEW}/legacy`);
  await expect(page.locator("#workspacePanel")).toBeVisible();
  await page.locator("#wsCreateBtn").click();
  await expect(page.locator("#wsActive")).toBeVisible();
  const made = await stored(page);
  expect(made).not.toBeNull();
  expect(Object.keys(made as Stored)).toEqual(["id", "token", "expires_at"]);
  expect(sent).toEqual(["POST /api/workspace"]);

  const w = await watch(page, PREVIEW);
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
  await expect(page.getByText((made as Stored).id)).toBeVisible();
  expect(sent).toEqual(["POST /api/workspace"]); // nothing but the create on the legacy page: the restore here was a GET
  const after = (await stored(page)) as Stored;
  expect([after.id, after.token]).toEqual([(made as Stored).id, (made as Stored).token]);
  expect(Object.keys(after)).toEqual(["id", "token", "expires_at"]);
  expect(await problems(w)).toEqual([]);
});

test("a workspace made on /v2/ is restored on the legacy page in the same tab", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await expect(page.getByRole("button", { name: "Delete workspace" })).toBeVisible();
  const made = (await stored(page)) as Stored;
  await page.goto(`${PREVIEW}/legacy`);
  await expect(page.locator("#wsActive")).toBeVisible();
  await expect(page.locator("#wsIdLabel")).toHaveText(made.id);
  await page.locator("#wsDeleteBtn").click();
  await expect(page.locator("#wsCreate")).toBeVisible();
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeVisible(); // deleted on the legacy page: gone here too
  expect(await stored(page)).toBeNull();
});

test("a stored session the server no longer knows is dropped on load", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await page.evaluate((key) => window.sessionStorage.setItem(key, JSON.stringify({ id: "0".repeat(32), token: "gone-token", expires_at: null })), KEY);
  await page.reload();
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeVisible();
  expect(await stored(page)).toBeNull();
});

test("a stored value that is not a session is ignored and makes no workspace request", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/workspace")) requests.push(pathname);
  });
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await page.evaluate((key) => window.sessionStorage.setItem(key, JSON.stringify({ id: "../../api/admin", token: "x", expires_at: null })), KEY);
  await page.reload();
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeVisible();
  expect(requests).toEqual([]);
});

import { expect, test, type CDPSession, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";
import { firstStarter } from "./support.ts";

// The owner's NVDA pass on release v21 (docs/v2/M5B_A11Y_MANUAL_PASS.md, findings N1, N3, N4). What NVDA reads comes from Chrome's
// accessibility tree, which jsdom cannot build: these checks read Chrome's own names and text over the DevTools protocol. Chrome's
// computed names already had the spaces; NVDA's browse mode reads the text runs (StaticText) instead, so the runs are what is checked.
//   N1  a chip was announced "expanded" when focus came back to it after the panel closed (3 of 3): its aria-expanded must be false
//       at least one frame before focus lands on it, and focus must not fall to the page body meanwhile.
//   N3  a label ran into its value ("ChecksWorking", "Next checkin", "RevenueMoney from sales").
//   N4  two names lost a space ("More optionsDepth: Standard", "Freshness:Data as of ...").

interface AXNode {
  nodeId: string;
  backendDOMNodeId?: number;
  role?: { value: string };
  name?: { value: string };
  childIds?: string[];
}

async function cdp(page: Page): Promise<CDPSession> {
  const client = await page.context().newCDPSession(page);
  await client.send("Accessibility.enable");
  return client;
}

async function backendId(client: CDPSession, selector: string): Promise<number> {
  const { result } = await client.send("Runtime.evaluate", { expression: `document.querySelector(${JSON.stringify(selector)})` });
  if (result.objectId === undefined) throw new Error(`no element for ${selector}`);
  const { node } = await client.send("DOM.describeNode", { objectId: result.objectId });
  return node.backendNodeId;
}

/** Chrome's accessible name of the element the selector finds. */
async function axName(page: Page, selector: string): Promise<string> {
  const client = await cdp(page);
  const { nodes } = (await client.send("Accessibility.getPartialAXTree", { backendNodeId: await backendId(client, selector), fetchRelatives: false })) as { nodes: AXNode[] };
  return nodes[0]?.name?.value ?? "";
}

/** The text runs Chrome exposes under the element, joined as they are (this is the text a screen reader's browse mode reads). */
async function axText(page: Page, selector: string): Promise<string> {
  const client = await cdp(page);
  const target = await backendId(client, selector);
  const { nodes } = (await client.send("Accessibility.getFullAXTree")) as { nodes: AXNode[] };
  const byId = new Map(nodes.map((n) => [n.nodeId, n]));
  const root = nodes.find((n) => n.backendDOMNodeId === target);
  const runs: string[] = [];
  const walk = (n: AXNode): void => {
    if (n.role?.value === "StaticText") {
      runs.push(n.name?.value ?? "");
      return;
    }
    for (const id of n.childIds ?? []) {
      const child = byId.get(id);
      if (child !== undefined) walk(child);
    }
  };
  if (root !== undefined) walk(root);
  return runs.join("");
}

interface N1Record {
  frame: number;
  collapsedAt: number;
  focusedAt: number;
  bodyFrames: number;
}

test("N1: a chip is collapsed a frame before focus returns to it, and focus never falls to the page body", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  const first = firstStarter(page);
  await expect(first).toBeVisible();
  await first.click();
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  const chip = page.locator("#answer [data-chip-key]").first();
  await chip.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Close evidence" })).toBeFocused();
  await expect(chip).toHaveAttribute("aria-expanded", "true");
  await page.evaluate(() => {
    const record: N1Record = { frame: 0, collapsedAt: -1, focusedAt: -1, bodyFrames: 0 };
    (window as unknown as { n1: N1Record }).n1 = record;
    const el = document.querySelector("#answer [data-chip-key]") as HTMLElement;
    const tick = (): void => {
      record.frame += 1;
      if (document.activeElement === document.body) record.bodyFrames += 1;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    new MutationObserver(() => {
      if (el.getAttribute("aria-expanded") === "false" && record.collapsedAt < 0) record.collapsedAt = record.frame;
    }).observe(el, { attributes: true, attributeFilter: ["aria-expanded"] });
    el.addEventListener("focus", () => {
      if (record.focusedAt < 0) record.focusedAt = record.frame;
    });
  });
  await page.keyboard.press("Escape");
  await expect(chip).toBeFocused();
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  const n1 = await page.evaluate(() => (window as unknown as { n1: N1Record }).n1);
  expect(n1.collapsedAt).toBeGreaterThanOrEqual(0);
  expect(n1.focusedAt).toBeGreaterThan(n1.collapsedAt);
  expect(n1.bodyFrames).toBe(0);
});

test("N1: Close evidence does the same as Escape", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await firstStarter(page).click();
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  const chip = page.locator("#answer [data-chip-key]").first();
  await chip.click();
  await page.getByRole("button", { name: "Close evidence" }).click();
  await expect(chip).toBeFocused();
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("aside[aria-label='Evidence']")).toHaveAttribute("inert", "");
});

test("N3: each Freshness row reads its label, a space, then its value", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/freshness/`);
  const rows = page.locator('ul[aria-label="Freshness details"] > li');
  await expect(rows.first()).toBeVisible();
  const count = await rows.count();
  for (let i = 1; i <= count; i++) {
    const label = (await page.locator(`ul[aria-label="Freshness details"] > li:nth-child(${i}) > span:first-child`).innerText()).trim();
    const text = await axText(page, `ul[aria-label="Freshness details"] > li:nth-child(${i})`);
    expect(text.replace(/\u00a0/g, " ").startsWith(`${label} `), `row ${i}: "${text}"`).toBe(true);
  }
});

test("N3: the evidence panel's facts read each name, a space, then its value", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await firstStarter(page).click();
  await expect(page.getByRole("status")).toHaveText("Saved answer, shown instantly.");
  await page.locator("#answer [data-chip-key]").first().click();
  const terms = page.locator("#evidence-panel dl dt");
  await expect(terms.first()).toBeVisible();
  // the rows' wrappers are display: contents (no accessibility node), so the whole list's text runs are read at once
  const text = (await axText(page, "#evidence-panel dl")).replace(/\u00a0/g, " ");
  for (const term of await terms.allInnerTexts()) {
    const name = term.trim();
    const at = text.indexOf(name);
    expect(at, `"${name}" in "${text}"`).toBeGreaterThanOrEqual(0);
    expect(text.slice(at + name.length, at + name.length + 1), `after "${name}" in "${text}"`).toBe(" ");
    if (at > 0) expect(text.slice(at - 1, at), `before "${name}" in "${text}"`).toBe(" ");
  }
});

test("N3: a Key numbers row header reads the metric, a space, then its plain meaning", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/company/NVDA/`);
  await expect(page.getByRole("rowheader").first()).toBeVisible();
  const text = await axText(page, "table[aria-labelledby='key-numbers'] tbody tr:first-child th");
  expect(text.replace(/\u00a0/g, " ")).toBe("Revenue Money from sales");
});

test("N4: More options and the freshness link keep the space between their two parts", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByRole("button", { name: /More options/ })).toBeVisible();
  const trigger = await page.getByRole("button", { name: /More options/ }).evaluate((el) => {
    el.setAttribute("data-probe", "more");
    return true;
  });
  expect(trigger).toBe(true);
  expect(await axText(page, "[data-probe='more']")).toBe("More options Depth: Standard");
  const link = page.locator("#shell-freshness a");
  await expect(link).toBeVisible();
  // the fixture server's freshness is "Data status unavailable"; the live site's is "Data as of ..."
  expect(await axText(page, "#shell-freshness a")).toMatch(/^Freshness:\s+\S/);
  expect(await axName(page, "#shell-freshness a")).toMatch(/^Freshness:\s+\S/);
  // the date line is said once: no tooltip repeats it as a description
  await expect(link).not.toHaveAttribute("title", /.*/);
});

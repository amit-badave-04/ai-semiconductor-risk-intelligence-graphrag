import AxeBuilder from "@axe-core/playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import nodePath from "node:path";
import { expect, test } from "@playwright/test";
import { PAGES, PREVIEW } from "./servers.ts";

// B3 / P3: axe on the five pages, in both colour schemes, WCAG 2.2 AA tags (docs/specs/m5b-frontend.md Acceptance); zero
// serious or critical violations. Automated checks cannot detect every WCAG failure, so the manual pass (A11) stays.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// Opt-in record: with AXE_OUT=<directory> each run also writes one JSON file (page, scheme, every violation found) for
// artifacts/m5b_axe.json. Unset (the default, and CI) nothing is written.
type AxeViolation = { id: string; impact?: string | null; help: string; nodes: unknown[] };
function recordAxe(page: string, scheme: string, violations: AxeViolation[]): void {
  const dir = process.env.AXE_OUT;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  const blocking = violations.filter((v) => v.impact === "serious" || v.impact === "critical").length;
  const rows = violations.map((v) => ({ id: v.id, impact: v.impact ?? null, help: v.help, nodes: v.nodes.length }));
  const name = `${scheme}.${page.replace(/[^a-z0-9]+/gi, "_")}.json`;
  writeFileSync(nodePath.join(dir, name), `${JSON.stringify({ page, scheme, blocking, violations: rows }, null, 2)}\n`);
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`axe, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });
    for (const { path, heading } of PAGES) {
      test(`/v2/${path} has no serious or critical violation`, async ({ page }) => {
        await page.goto(`${PREVIEW}/v2/${path}`);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
        recordAxe(`/v2/${path}`, scheme, results.violations);
        const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
        expect(blocking.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)).toEqual([]);
      });
    }
  });
}

test("the not-found view has no serious or critical violation either", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/nope/`);
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
});

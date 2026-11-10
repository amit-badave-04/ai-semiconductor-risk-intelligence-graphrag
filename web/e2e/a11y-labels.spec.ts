import AxeBuilder from "@axe-core/playwright";
import { chooseDepth } from "./support.ts";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW } from "./servers.ts";

// A1 / A4 / A6 and the axe "error status" state (docs/specs/m5b-frontend.md :643-645), in one thin spec: every control of the Ask
// page and the start of the Workspace page is found by its label (the controls of an open workspace are checked in workspace.spec.ts:168,
// which shares the fixture's one workspace and upload slot, so this spec does not create one), and
// the Ask page showing an error in its status region has no serious or critical violation, in both colour schemes.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const ERROR_QUESTION = "Fixture question: an error before any text";

const blocking = (violations: { id: string; help: string; impact?: string | null; nodes: unknown[] }[]) =>
  violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.help} (${v.nodes.length})`);

async function askForError(page: Page) {
  await page.goto(`${PREVIEW}/v2/`);
  await page.getByLabel("Your question").fill(ERROR_QUESTION);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("status")).toHaveClass(/destructive/);
}

test("the Ask page's controls are found by label, and axe's label rules are clean (A4)", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/`);
  await expect(page.getByLabel("Your question")).toBeVisible();
  await expect(page.getByRole("button", { name: /More options/ })).toBeVisible();
  await chooseDepth(page, "Standard (recommended)"); // RD7: Depth is a labelled radio group, checked by axe below
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeVisible();
  const results = await new AxeBuilder({ page }).withRules(["label", "select-name"]).analyze();
  expect(results.violations.map((v) => v.id)).toEqual([]);
});

test("the Workspace page's start controls are found by label, and the restore key is a password field (A4, A6, S8)", async ({ page }) => {
  await page.goto(`${PREVIEW}/v2/workspace/`);
  await page.getByRole("button", { name: "I have a workspace id and key" }).click(); // RD26: Restore sits behind a disclosure
  await expect(page.getByLabel("Workspace id")).toBeVisible();
  await expect(page.getByLabel("Workspace key")).toHaveAttribute("type", "password");
  await expect(page.getByRole("button", { name: "Create a workspace" })).toBeVisible();
  const results = await new AxeBuilder({ page }).withRules(["label", "select-name"]).analyze();
  expect(results.violations.map((v) => v.id)).toEqual([]);
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`axe on the error status, ${scheme} scheme`, () => {
    test.use({ colorScheme: scheme });
    test("an error in the status region has no serious or critical violation", async ({ page }) => {
      await askForError(page);
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      expect(blocking(results.violations)).toEqual([]);
    });
  });
}

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// docs/specs/m5b-redesign.md RD1 and the accessibility review of baba14d: the one focus indicator is the global 3 px outline of
// index.css (forced-colours mode keeps an outline; it drops a box-shadow ring). No component may draw a focus ring of its own
// or remove the outline, except the question box's textarea, whose bordered box draws the outline for it (Ask.tsx).

const SRC = path.resolve(import.meta.dirname, "../..");
const ALLOWED_OUTLINE_NONE = new Set(["pages/Ask.tsx"]);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return name === "__tests__" || name === "test" ? [] : sources(full);
    return /\.(tsx?|css)$/.test(name) ? [full] : [];
  });
}

describe("focus styles", () => {
  const files = sources(SRC).map((file) => ({ rel: path.relative(SRC, file).split(path.sep).join("/"), text: readFileSync(file, "utf-8") }));

  it("reads the source tree", () => {
    expect(files.length).toBeGreaterThan(40);
  });

  it("no component draws a focus ring of its own", () => {
    const rings = files.flatMap(({ rel, text }) => (text.match(/focus(?:-visible|-within)?:ring\S*/g) ?? []).map((m) => `${rel}: ${m}`));
    expect(rings).toEqual([]);
  });

  it("no component removes the outline, but the question box's textarea", () => {
    const removed = files.flatMap(({ rel, text }) =>
      (text.match(/(?:^|[\s"'`])(?:focus(?:-visible)?:)?outline-none/g) ?? []).map(() => rel).filter((r) => !ALLOWED_OUTLINE_NONE.has(r)),
    );
    expect(removed).toEqual([]);
  });
});

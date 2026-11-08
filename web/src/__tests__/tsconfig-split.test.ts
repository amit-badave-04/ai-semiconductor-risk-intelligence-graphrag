import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// I3 main-first item 1: the app's own project must not see Node's types (a `process` or `Buffer` in shipped code would type-check
// and then fail in the browser), while the tests, which do use node:fs and friends, still type-check under `tsc -b`.
const WEB = path.resolve(import.meta.dirname, "..", "..");

function readJson(name: string): Record<string, unknown> {
  // tsconfig files may carry /* block */ comments (tsconfig.app.json does) and strings holding "/*" ("./src/*"): match whole
  // strings first so only real comments are dropped
  const text = readFileSync(path.join(WEB, name), "utf8").replace(/("(?:[^"\\]|\\.)*")|\/\*[\s\S]*?\*\//g, (_all, str?: string) => str ?? "");
  return JSON.parse(text) as Record<string, unknown>;
}

describe("tsconfig split: app code without Node types, tests with them", () => {
  it("the app project excludes every test file and the test setup", () => {
    const app = readJson("tsconfig.app.json");
    expect(app.exclude).toEqual(expect.arrayContaining(["src/**/__tests__/**", "src/**/*.test.*", "src/test/**"]));
  });

  it("the app project does not list node among its types", () => {
    const options = readJson("tsconfig.app.json").compilerOptions as { types: string[] };
    expect(options.types).not.toContain("node");
  });

  it("the test project adds node types and covers the tests", () => {
    const test = readJson("tsconfig.test.json");
    const options = test.compilerOptions as { types: string[] };
    expect(options.types).toEqual(expect.arrayContaining(["node", "vite/client"]));
    expect(test.include).toEqual(expect.arrayContaining(["src"]));
  });

  it("tsconfig.json references the test project", () => {
    const root = readJson("tsconfig.json");
    expect((root.references as { path: string }[]).map((r) => r.path)).toContain("./tsconfig.test.json");
  });
});

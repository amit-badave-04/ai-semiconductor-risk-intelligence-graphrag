import { describe, expect, it } from "vitest";
import { basenameFor } from "../basename";

describe("basenameFor (R12: chosen at runtime from the path)", () => {
  it.each([
    ["/v2/", "/v2"],
    ["/v2", "/v2"],
    ["/v2/company/NVDA/", "/v2"],
    ["/v2/workspace/", "/v2"],
    ["/v2/assets/index-abc.js", "/v2"],
  ])("%s is served under /v2", (pathname, expected) => {
    expect(basenameFor(pathname)).toBe(expected);
  });

  it.each(["/", "", "/company/NVDA/", "/workspace/", "/method/", "/v22/", "/v2x", "/api/v2/", "/V2/", "/legacy"])(
    "%s is served at the root (the flipped app)",
    (pathname) => {
      expect(basenameFor(pathname)).toBe("");
    },
  );
});

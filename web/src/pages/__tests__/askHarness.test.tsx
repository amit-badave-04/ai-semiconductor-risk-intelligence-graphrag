import { describe, expect, it, vi } from "vitest";
import { workspaceValue } from "./askHarness";

// The harness's own defaults: the callbacks a test does not script do nothing and the headers are empty.
describe("workspaceValue defaults", () => {
  it("has inert callbacks and no headers, and lets a test override any field", () => {
    const value = workspaceValue();
    expect(value.headers()).toEqual({});
    expect(() => {
      value.setAsOf("2026-01-01");
      value.setWorkspace(null);
      value.invalidateDocEvidence();
    }).not.toThrow();
    const setAsOf = vi.fn();
    workspaceValue({ setAsOf }).setAsOf("x");
    expect(setAsOf).toHaveBeenCalledWith("x");
  });
});

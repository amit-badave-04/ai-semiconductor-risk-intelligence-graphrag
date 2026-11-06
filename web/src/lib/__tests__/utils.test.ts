import { describe, expect, it } from "vitest";
import { cn } from "../utils";

describe("cn (shadcn's class merger, re-exported)", () => {
  it("joins classes and lets the later Tailwind class win", () => {
    expect(cn("p-2", "text-sm")).toBe("p-2 text-sm");
    expect(cn("p-2", "p-4")).toBe("p-4");
  });
  it("drops falsy values", () => {
    const enabled = Math.random() > 2; // always false, but not a compile-time constant
    expect(cn("a", enabled && "b", undefined, null, "c")).toBe("a c");
  });
});

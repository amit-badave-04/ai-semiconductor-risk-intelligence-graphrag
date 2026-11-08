import { afterEach, describe, expect, it, vi } from "vitest";

// Two Playwright runs at once (different workers) must not collide, and Lighthouse owns 8767 (web/scripts/lighthouse.mjs): every
// e2e fixture-server port derives from one variable, E2E_PORT_BASE (default 8765, today's ports).

async function ports(base?: string) {
  vi.resetModules();
  if (base === undefined) vi.stubEnv("E2E_PORT_BASE", "");
  else vi.stubEnv("E2E_PORT_BASE", base);
  return import("../../e2e/servers");
}

afterEach(() => vi.unstubAllEnvs());

describe("e2e fixture-server ports", () => {
  it("keep today's values by default", async () => {
    const s = await ports();
    expect([s.PREVIEW_PORT, s.FLIPPED_PORT, s.SITE_KEY_PORT]).toEqual([8765, 8766, 8768]);
    expect(s.PREVIEW).toBe("http://127.0.0.1:8765");
    expect(s.SITE_KEY).toBe("http://127.0.0.1:8768");
  });

  it("never use 8767 (the Lighthouse runner's port) with the default base", async () => {
    const s = await ports();
    expect([s.PREVIEW_PORT, s.FLIPPED_PORT, s.SITE_KEY_PORT]).not.toContain(8767);
  });

  it("all move with the base", async () => {
    const s = await ports("8865");
    expect([s.PREVIEW_PORT, s.FLIPPED_PORT, s.SITE_KEY_PORT]).toEqual([8865, 8866, 8868]);
  });

  it("two bases a hundred apart share no port", async () => {
    const a = await ports("8765");
    const first = new Set([a.PREVIEW_PORT, a.FLIPPED_PORT, a.SITE_KEY_PORT]);
    const b = await ports("8865");
    expect([b.PREVIEW_PORT, b.FLIPPED_PORT, b.SITE_KEY_PORT].filter((p) => first.has(p))).toEqual([]);
  });

  it.each(["abc", "80", "70000", "8765.5", "-1"])("refuses the base %j", async (base) => {
    await expect(ports(base)).rejects.toThrow("E2E_PORT_BASE");
  });
});

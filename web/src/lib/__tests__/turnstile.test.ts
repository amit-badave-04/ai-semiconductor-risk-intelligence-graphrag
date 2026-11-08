import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { currentTurnstile, loadTurnstileScript, resetTurnstileScriptCache, TURNSTILE_ONLOAD, TURNSTILE_SCRIPT_SRC } from "../turnstile";
import { installFakeTurnstile, uninstallFakeTurnstile } from "@/test/fakeTurnstile";

// R33: the script is loaded from challenges.cloudflare.com only, once, and only when a site key exists (the caller decides that).

const scripts = () => [...document.querySelectorAll("script")].filter((s) => s.src.includes("challenges.cloudflare.com"));

beforeEach(() => {
  resetTurnstileScriptCache();
  uninstallFakeTurnstile();
});

afterEach(() => {
  resetTurnstileScriptCache();
  uninstallFakeTurnstile();
  document.head.querySelectorAll("script").forEach((s) => s.remove());
  Reflect.deleteProperty(window, TURNSTILE_ONLOAD);
});

describe("the script address", () => {
  it("is the literal Cloudflare address with the explicit-render and onload query", () => {
    expect(TURNSTILE_SCRIPT_SRC).toBe(`https://challenges.cloudflare.com/turnstile/v0/api.js?onload=${TURNSTILE_ONLOAD}&render=explicit`);
    expect(new URL(TURNSTILE_SCRIPT_SRC).origin).toBe("https://challenges.cloudflare.com");
  });
});

describe("currentTurnstile", () => {
  it("is null with no script loaded and the api once one is", () => {
    expect(currentTurnstile()).toBeNull();
    const fake = installFakeTurnstile();
    expect(currentTurnstile()).toBe(fake);
  });

  it("is null when window.turnstile is not an object with render, remove and reset", () => {
    (window as unknown as { turnstile?: unknown }).turnstile = { render: 1 };
    expect(currentTurnstile()).toBeNull();
  });
});

describe("loadTurnstileScript", () => {
  it("adds no script when the api is already there", async () => {
    const fake = installFakeTurnstile();
    expect(await loadTurnstileScript()).toBe(fake);
    expect(scripts()).toHaveLength(0);
  });

  it("appends one async script with the literal address and resolves when Cloudflare calls the onload hook", async () => {
    const pending = loadTurnstileScript();
    const [script] = scripts();
    expect(scripts()).toHaveLength(1);
    expect(script.src).toBe(TURNSTILE_SCRIPT_SRC);
    expect(script.async).toBe(true);
    const fake = installFakeTurnstile();
    (window as unknown as Record<string, () => void>)[TURNSTILE_ONLOAD]();
    expect(await pending).toBe(fake);
  });

  it("shares one script between concurrent callers (StrictMode, a remount)", async () => {
    const first = loadTurnstileScript();
    const second = loadTurnstileScript();
    expect(scripts()).toHaveLength(1);
    const fake = installFakeTurnstile();
    scripts()[0].dispatchEvent(new Event("load"));
    expect(await first).toBe(fake);
    expect(await second).toBe(fake);
  });

  it("resolves null when the script fails to load, and a later call tries again", async () => {
    const failed = loadTurnstileScript();
    scripts()[0].dispatchEvent(new Event("error"));
    expect(await failed).toBeNull();
    const retry = loadTurnstileScript();
    expect(scripts().length).toBeGreaterThanOrEqual(1);
    const fake = installFakeTurnstile();
    scripts().at(-1)?.dispatchEvent(new Event("load"));
    expect(await retry).toBe(fake);
  });

  it("resolves null when the script loads but leaves no usable api", async () => {
    const pending = loadTurnstileScript();
    scripts()[0].dispatchEvent(new Event("load"));
    expect(await pending).toBeNull();
  });
});

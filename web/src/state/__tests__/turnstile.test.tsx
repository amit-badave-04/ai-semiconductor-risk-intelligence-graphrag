import { act, render, screen } from "@testing-library/react";
import { StrictMode, useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TurnstileWidget } from "@/components/TurnstileWidget";
import { resetConfigCache } from "@/lib/config";
import { resetTurnstileScriptCache } from "@/lib/turnstile";
import { installFakeTurnstile, uninstallFakeTurnstile, type FakeTurnstile } from "@/test/fakeTurnstile";
import { TurnstileProvider } from "../TurnstileProvider";
import { useTurnstile, type TurnstileContextValue } from "../useTurnstile";

// R33 (docs/specs/m5b-frontend.md :436-438): one widget in the app shell, reset after each use, one token consumed by whichever
// request uses it first, an expired token cleared, a StrictMode double mount leaving one live widget, a route change leaving it
// alone. The Cloudflare script is a fake window.turnstile: nothing here ever solves a real challenge.

let fake: FakeTurnstile;
let ctx: TurnstileContextValue;

function Probe() {
  const value = useTurnstile();
  useEffect(() => {
    ctx = value; // the tests read the latest context value
  });
  return null;
}

function Shell({ page }: { page: ReactNode }) {
  return (
    <>
      <TurnstileWidget />
      <main data-testid="page">{page}</main>
      <Probe />
    </>
  );
}

function answerConfig(body: unknown, status = 200) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
}

const withKey = () => answerConfig({ turnstile_site_key: "dummy-site-key" });

async function mount(page: ReactNode = "ask") {
  const tree = (p: ReactNode) => (
    <StrictMode>
      <TurnstileProvider>
        <Shell page={p} />
      </TurnstileProvider>
    </StrictMode>
  );
  const view = render(tree(page));
  await act(async () => {});
  return { view, rerenderPage: (p: ReactNode) => view.rerender(tree(p)) };
}

beforeEach(() => {
  resetConfigCache();
  resetTurnstileScriptCache();
  fake = installFakeTurnstile();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  uninstallFakeTurnstile();
  document.head.querySelectorAll("script").forEach((s) => s.remove());
});

describe("no site key", () => {
  it("a known none mounts nothing: no script, no widget, no token", async () => {
    answerConfig({ turnstile_site_key: "" });
    await mount();
    expect(ctx.siteKeyState).toEqual({ kind: "none" });
    expect(ctx.siteKeySet).toBe(false);
    expect(fake.renders).toBe(0);
    expect(document.querySelectorAll("script")).toHaveLength(0);
    expect(ctx.peek()).toBeNull();
    expect(ctx.consume()).toBeNull();
    expect(await ctx.waitForToken(10_000)).toBeNull(); // nothing to wait for: it answers at once
  });

  it("an unknown config (two failures) mounts no widget and is not treated as none", async () => {
    vi.useFakeTimers();
    answerConfig({}, 500);
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(ctx.siteKeyState).toEqual({ kind: "unknown", failed: true });
    expect(ctx.siteKeySet).toBe(false);
    expect(ctx.siteKeyState.kind).not.toBe("none");
    expect(fake.renders).toBe(0);
  });

  it("while the config is still loading the state is unknown, not failed", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    render(
      <TurnstileProvider>
        <Probe />
      </TurnstileProvider>,
    );
    expect(ctx.siteKeyState).toEqual({ kind: "unknown", failed: false });
  });
});

describe("with a site key", () => {
  it("under StrictMode exactly one widget is live (every render but one was removed)", async () => {
    withKey();
    await mount();
    expect(ctx.siteKeySet).toBe(true);
    expect(fake.renders).toBeGreaterThanOrEqual(1);
    expect(fake.renders - fake.removes).toBe(1);
    expect(fake.live.size).toBe(1);
    expect(fake.frames()).toBe(1);
    expect([...fake.live.values()][0].options.sitekey).toBe("dummy-site-key");
  });

  it("asks /api/config once even though StrictMode ran the effect twice", async () => {
    withKey();
    await mount();
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("the widget lives in the host the shell renders", async () => {
    withKey();
    await mount();
    const host = document.querySelector("[data-widget]")?.parentElement ?? null;
    expect(host).not.toBeNull();
    expect(screen.getByTestId("page").contains(host)).toBe(false);
  });

  it("a route change (the page below the shell changes) neither renders nor removes the widget", async () => {
    withKey();
    const { rerenderPage } = await mount("ask");
    const before = { renders: fake.renders, removes: fake.removes, resets: fake.resets };
    rerenderPage("company");
    await act(async () => {});
    expect({ renders: fake.renders, removes: fake.removes, resets: fake.resets }).toEqual(before);
    expect(fake.live.size).toBe(1);
  });

  it("an unmount removes the widget and a remount leaves one", async () => {
    withKey();
    const first = await mount();
    first.view.unmount();
    expect(fake.live.size).toBe(0);
    expect(fake.frames()).toBe(0);
    resetConfigCache();
    await mount();
    expect(fake.live.size).toBe(1);
    expect(fake.renders - fake.removes).toBe(1);
  });

  it("hands out a fresh token per use: consume clears it and resets the widget, and no token is handed out twice", async () => {
    withKey();
    await mount();
    expect(ctx.peek()).toBeNull();
    const first = fake.solve();
    expect(ctx.peek()).toBe(first);
    expect(ctx.consume()).toBe(first);
    expect(fake.resets).toBe(1);
    expect(ctx.peek()).toBeNull();
    expect(ctx.consume()).toBeNull();
    expect(fake.resets).toBe(1); // nothing was used, nothing is reset again
    const second = fake.solve();
    expect(second).not.toBe(first);
    expect(ctx.consume()).toBe(second);
  });

  it("clear drops the token and resets the widget without handing it to anyone", async () => {
    withKey();
    await mount();
    fake.solve();
    ctx.clear();
    expect(ctx.peek()).toBeNull();
    expect(fake.resets).toBe(1);
  });

  it("an expired token is cleared", async () => {
    withKey();
    await mount();
    fake.solve();
    act(() => fake.expire());
    expect(ctx.peek()).toBeNull();
    expect(ctx.consume()).toBeNull();
  });

  it("a widget error clears the token too", async () => {
    withKey();
    await mount();
    fake.solve();
    act(() => fake.fail());
    expect(ctx.peek()).toBeNull();
  });

  async function discardedThenLive() {
    withKey();
    const first = await mount();
    const discarded = fake.registered.at(-1);
    if (!discarded) throw new Error("no widget was rendered");
    first.view.unmount(); // the widget is removed
    resetConfigCache();
    await mount(); // a new one is rendered
    const live = fake.registered.at(-1);
    if (!live || live === discarded) throw new Error("the second mount rendered no new widget");
    return { discarded, live };
  }

  it("a late callback of a widget that was removed never becomes the current token", async () => {
    const { discarded, live } = await discardedThenLive();
    discarded.callback("stale-token-from-the-removed-widget");
    expect(ctx.peek()).toBeNull();
    live.callback("token-from-the-live-widget");
    expect(ctx.peek()).toBe("token-from-the-live-widget");
  });

  it("a late expiry of a removed widget does not clear the live token", async () => {
    const { discarded, live } = await discardedThenLive();
    live.callback("live");
    discarded["expired-callback"]();
    expect(ctx.peek()).toBe("live");
  });
});

describe("waitForToken", () => {
  it("resolves at once with a token that is already there, consuming it", async () => {
    withKey();
    await mount();
    const token = fake.solve();
    expect(await ctx.waitForToken(15_000)).toBe(token);
    expect(ctx.peek()).toBeNull();
    expect(fake.resets).toBe(1);
  });

  it("waits for the visitor and resolves with the token, which is consumed", async () => {
    withKey();
    await mount();
    const pending = ctx.waitForToken(15_000);
    let settled = false;
    void pending.then(() => {
      settled = true;
    });
    await act(async () => {});
    expect(settled).toBe(false);
    let token = "";
    await act(async () => {
      token = fake.solve();
    });
    expect(await pending).toBe(token);
    expect(ctx.peek()).toBeNull();
    expect(fake.resets).toBe(1);
  });

  it("gives up with null after the time asked for", async () => {
    withKey();
    await mount();
    vi.useFakeTimers();
    const pending = ctx.waitForToken(15_000);
    await vi.advanceTimersByTimeAsync(14_999);
    let settled = false;
    void pending.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await pending).toBeNull();
  });

  it("gives up with null when the caller aborts, and leaves the later token for someone else", async () => {
    withKey();
    await mount();
    const controller = new AbortController();
    const pending = ctx.waitForToken(15_000, controller.signal);
    controller.abort();
    expect(await pending).toBeNull();
    const token = fake.solve();
    expect(ctx.peek()).toBe(token);
  });

  it("answers null at once for a signal that is already aborted", async () => {
    withKey();
    await mount();
    const controller = new AbortController();
    controller.abort();
    expect(await ctx.waitForToken(15_000, controller.signal)).toBeNull();
  });

  it("two waiters are served one token each, in order (a token is never given twice)", async () => {
    withKey();
    await mount();
    const first = ctx.waitForToken(15_000);
    const second = ctx.waitForToken(15_000);
    let a = "";
    let b = "";
    await act(async () => {
      a = fake.solve();
    });
    expect(await first).toBe(a);
    await act(async () => {
      b = fake.solve();
    });
    expect(await second).toBe(b);
    expect(a).not.toBe(b);
  });

  it("resolves a pending waiter with null when the shell unmounts", async () => {
    withKey();
    const { view } = await mount();
    const pending = ctx.waitForToken(15_000);
    view.unmount();
    expect(await pending).toBeNull();
  });
});

describe("the Cloudflare script", () => {
  it("is added once, from the literal address, only when a key exists and no api is present", async () => {
    uninstallFakeTurnstile();
    withKey();
    await mount();
    const scripts = [...document.querySelectorAll("script")].filter((s) => s.src.startsWith("https://challenges.cloudflare.com/"));
    expect(scripts).toHaveLength(1);
    expect(scripts[0].src).toBe("https://challenges.cloudflare.com/turnstile/v0/api.js?onload=semigraphTurnstileReady&render=explicit");
  });

  it("is never added without a key", async () => {
    uninstallFakeTurnstile();
    answerConfig({ turnstile_site_key: "" });
    await mount();
    expect(document.querySelectorAll("script")).toHaveLength(0);
  });

  it("renders the widget once the script has loaded", async () => {
    uninstallFakeTurnstile();
    withKey();
    await mount();
    const late = installFakeTurnstile();
    await act(async () => {
      document.querySelector("script")?.dispatchEvent(new Event("load"));
    });
    expect(late.live.size).toBe(1);
  });
});

describe("outside a provider", () => {
  it("useTurnstile answers an inert, honest default: unknown, no key, no token, no waiting", async () => {
    render(<Probe />);
    expect(ctx.siteKeyState).toEqual({ kind: "unknown", failed: false });
    expect(ctx.siteKeySet).toBe(false);
    expect(ctx.peek()).toBeNull();
    expect(ctx.consume()).toBeNull();
    expect(await ctx.waitForToken(10_000)).toBeNull();
    expect(() => ctx.clear()).not.toThrow();
  });
});

describe("a widget whose callbacks run synchronously inside render()", () => {
  it("does not hit the temporal dead zone: the early token becomes the current one once the widget is registered", async () => {
    withKey();
    const original = fake.render;
    fake.render = (host, options) => {
      options.callback("early-token"); // before render() has returned the id
      return original(host, options);
    };
    await mount();
    expect(ctx.peek()).toBe("early-token");
  });

  it("an expiry that arrives synchronously drops the early token", async () => {
    withKey();
    const original = fake.render;
    fake.render = (host, options) => {
      options.callback("early-token");
      options["expired-callback"]();
      return original(host, options);
    };
    await mount();
    expect(ctx.peek()).toBeNull();
  });
});

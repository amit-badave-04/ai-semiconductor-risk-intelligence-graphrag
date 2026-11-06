import { act, render } from "@testing-library/react";
import { StrictMode, useCallback, useEffect, useRef, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Smoke check 7 of docs/specs/m5b-frontend.md R2: "Turnstile double-mount under React StrictMode yields one live widget and a
// fresh token per use". This is a PROBE of the stack (React 19.3.1, Testing Library 16.3.3, jsdom 30.1.3), not the shipped
// widget: the real hook and the app-shell widget are I3 (R33(3), main-owned). The hook below is the smallest one that
// follows R33(3): one `render` in an effect with a cleanup that `remove`s it, a token held in a ref, `reset` and a cleared
// token on every use, and an `expired-callback` that clears the token. A fake `window.turnstile` stands in for Cloudflare's
// script (typed here, not with a global declaration, so it cannot leak into the app's types).

interface WidgetOptions {
  sitekey: string;
  callback: (token: string) => void;
  "expired-callback": () => void;
}

class FakeTurnstile {
  readonly live = new Map<string, { host: HTMLElement; options: WidgetOptions }>();
  renders = 0;
  removes = 0;
  resets = 0;
  private issued = 0;

  render = (host: HTMLElement, options: WidgetOptions): string => {
    this.renders += 1;
    const id = `widget-${this.renders}`;
    const frame = document.createElement("div");
    frame.dataset.widget = id;
    host.append(frame);
    this.live.set(id, { host, options });
    return id;
  };

  remove = (id: string): void => {
    this.removes += 1;
    this.live.get(id)?.host.querySelector(`[data-widget="${id}"]`)?.remove();
    this.live.delete(id);
  };

  reset = (id: string): void => {
    if (this.live.has(id)) this.resets += 1;
  };

  /** What Cloudflare does when the visitor passes the challenge: the callback gets a token nobody has seen. */
  solve(id: string): string {
    this.issued += 1;
    const token = `token-${this.issued}`;
    this.live.get(id)?.options.callback(token);
    return token;
  }

  expire(id: string): void {
    this.live.get(id)?.options["expired-callback"]();
  }

  frames(): number {
    return document.querySelectorAll("[data-widget]").length;
  }
}

interface Turnstile {
  consume: () => string | null;
  peek: () => string | null;
}

function useTurnstileProbe(siteKey: string, withCleanup: boolean): Turnstile & { host: React.RefObject<HTMLDivElement | null> } {
  const host = useRef<HTMLDivElement | null>(null);
  const widgetId = useRef<string | null>(null);
  const token = useRef<string | null>(null);

  useEffect(() => {
    const api = (window as unknown as { turnstile?: FakeTurnstile }).turnstile;
    if (!api || !host.current) return undefined;
    let id = "";
    id = api.render(host.current, {
      sitekey: siteKey,
      // a late callback of a widget this effect already removed must not become the current token
      callback: (value) => {
        if (widgetId.current === id) token.current = value;
      },
      "expired-callback": () => {
        if (widgetId.current === id) token.current = null;
      },
    });
    widgetId.current = id;
    if (!withCleanup) return undefined;
    return () => {
      api.remove(id);
      widgetId.current = null;
      token.current = null;
    };
  }, [siteKey, withCleanup]);

  const peek = useCallback(() => token.current, []);
  const consume = useCallback(() => {
    const used = token.current;
    token.current = null;
    const api = (window as unknown as { turnstile?: FakeTurnstile }).turnstile;
    if (api && widgetId.current) api.reset(widgetId.current);
    return used;
  }, []);
  return { host, consume, peek };
}

interface ShellProps {
  page: ReactNode;
  onReady: (turnstile: Turnstile) => void;
  withCleanup?: boolean;
}

function Shell({ page, onReady, withCleanup = true }: ShellProps) {
  const { host, consume, peek } = useTurnstileProbe("dummy-site-key", withCleanup);
  useEffect(() => {
    onReady({ consume, peek });
  }, [onReady, consume, peek]);
  return (
    <div>
      <div ref={host} data-testid="widget-host" />
      <main>{page}</main>
    </div>
  );
}

let fake: FakeTurnstile;

beforeEach(() => {
  fake = new FakeTurnstile();
  (window as unknown as { turnstile?: FakeTurnstile }).turnstile = fake;
});

afterEach(() => {
  Reflect.deleteProperty(window, "turnstile");
});

function mount(page: ReactNode = "ask", withCleanup = true) {
  const probe: { current: Turnstile | null } = { current: null };
  const onReady = (turnstile: Turnstile) => {
    probe.current = turnstile;
  };
  const view = render(
    <StrictMode>
      <Shell page={page} onReady={onReady} withCleanup={withCleanup} />
    </StrictMode>,
  );
  return { probe, onReady, view };
}

describe("smoke check 7: one Turnstile widget in the app shell under React StrictMode (R33(3))", () => {
  it("renders the widget twice (StrictMode runs the effect, its cleanup, then the effect again) and leaves exactly one live", () => {
    mount();
    expect(fake.renders).toBe(2); // not vacuous: StrictMode really double-ran the effect
    expect(fake.removes).toBe(1);
    expect(fake.live.size).toBe(1);
    expect(fake.frames()).toBe(1);
  });

  it("the probe is not vacuous: the same hook with no cleanup leaves two live widgets", () => {
    mount("ask", false);
    expect(fake.renders).toBe(2);
    expect(fake.live.size).toBe(2);
    expect(fake.frames()).toBe(2);
  });

  it("a route change (the page below the shell changes) neither renders nor removes the widget", () => {
    const { onReady, view } = mount("ask");
    const before = { renders: fake.renders, removes: fake.removes };
    view.rerender(
      <StrictMode>
        <Shell page="company" onReady={onReady} />
      </StrictMode>,
    );
    expect({ renders: fake.renders, removes: fake.removes }).toEqual(before);
    expect(fake.live.size).toBe(1);
  });

  it("a remount leaves none while unmounted and one after mounting again", () => {
    const first = mount();
    first.view.unmount();
    expect(fake.live.size).toBe(0);
    expect(fake.frames()).toBe(0);
    mount();
    expect(fake.live.size).toBe(1);
    expect(fake.frames()).toBe(1);
    expect(fake.renders - fake.removes).toBe(1);
  });

  it("hands out a fresh token per use: consuming clears it and resets the widget, and a token is never handed out twice", () => {
    const { probe } = mount();
    const [id] = [...fake.live.keys()];
    const first = fake.solve(id);
    expect(probe.current?.peek()).toBe(first);
    expect(probe.current?.consume()).toBe(first);
    expect(fake.resets).toBe(1);
    expect(probe.current?.peek()).toBeNull();
    expect(probe.current?.consume()).toBeNull(); // nothing to reuse: the same token cannot be sent twice
    const second = fake.solve(id);
    expect(second).not.toBe(first);
    expect(probe.current?.consume()).toBe(second);
  });

  it("an expired token is cleared", () => {
    const { probe } = mount();
    const [id] = [...fake.live.keys()];
    fake.solve(id);
    act(() => fake.expire(id));
    expect(probe.current?.peek()).toBeNull();
    expect(probe.current?.consume()).toBeNull();
  });

  it("a late callback of the widget StrictMode discarded never becomes the current token", () => {
    // Capture what the first render registered, before its cleanup ran: mount once, remember every options object.
    const seen: WidgetOptions[] = [];
    const render = fake.render;
    fake.render = (host, options) => {
      seen.push(options);
      return render(host, options);
    };
    const { probe } = mount();
    expect(seen).toHaveLength(2);
    seen[0].callback("stale-token-from-the-discarded-widget");
    expect(probe.current?.peek()).toBeNull();
    seen[1].callback("token-from-the-live-widget");
    expect(probe.current?.peek()).toBe("token-from-the-live-widget");
  });
});

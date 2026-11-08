// A fake Cloudflare `window.turnstile` for unit tests (never a real challenge: nothing here solves or bypasses one). It counts
// renders, removes and resets, draws a marker element per live widget, and lets a test play the visitor (`solve`) or the
// service (`expire`, `fail`). Shared by the Turnstile tests and by any test that needs a token to exist.
import type { TurnstileApi, TurnstileRenderOptions } from "@/lib/turnstile";

export class FakeTurnstile implements TurnstileApi {
  readonly live = new Map<string, { host: HTMLElement; options: TurnstileRenderOptions }>();
  /** Every options object ever registered, in order (a discarded widget's late callback can be replayed from here). */
  readonly registered: TurnstileRenderOptions[] = [];
  renders = 0;
  removes = 0;
  resets = 0;
  private issued = 0;

  render = (host: HTMLElement, options: TurnstileRenderOptions): string => {
    this.renders += 1;
    const id = `widget-${this.renders}`;
    const frame = document.createElement("div");
    frame.dataset.widget = id;
    host.append(frame);
    this.live.set(id, { host, options });
    this.registered.push(options);
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

  /** The id of the one live widget (throws when there is not exactly one). */
  onlyId(): string {
    const ids = [...this.live.keys()];
    if (ids.length !== 1) throw new Error(`expected one live widget, found ${ids.length}`);
    return ids[0];
  }

  /** What Cloudflare does when the visitor passes: the callback gets a token nobody has seen. Returns it. */
  solve(id: string = this.onlyId()): string {
    this.issued += 1;
    const token = `token-${this.issued}`;
    this.live.get(id)?.options.callback(token);
    return token;
  }

  expire(id: string = this.onlyId()): void {
    this.live.get(id)?.options["expired-callback"]();
  }

  fail(id: string = this.onlyId()): void {
    this.live.get(id)?.options["error-callback"]();
  }

  frames(): number {
    return document.querySelectorAll("[data-widget]").length;
  }
}

/** Put a fresh fake on `window.turnstile` and return it. Pair with {@link uninstallFakeTurnstile} in afterEach. */
export function installFakeTurnstile(): FakeTurnstile {
  const fake = new FakeTurnstile();
  (window as unknown as { turnstile?: TurnstileApi }).turnstile = fake;
  return fake;
}

export function uninstallFakeTurnstile(): void {
  Reflect.deleteProperty(window, "turnstile");
}

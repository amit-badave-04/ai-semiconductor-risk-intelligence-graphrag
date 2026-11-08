// R33: the Cloudflare Turnstile script and the part of its API the shell uses. There is no wrapper package (the page needs a few
// lines, and a wrapper would inject a third-party script from a dependency). The script is loaded from
// https://challenges.cloudflare.com only, once, and only when a site key exists (the caller decides that). `window.turnstile` is
// typed here by a cast, not by a global declaration, so it cannot leak into the app's types.

/** The name of the global function Cloudflare calls when the script has loaded (the `onload` query parameter). */
export const TURNSTILE_ONLOAD = "semigraphTurnstileReady";

/** The one script address, a literal. `render=explicit`: the shell renders the single widget itself. */
export const TURNSTILE_SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=semigraphTurnstileReady&render=explicit";

export interface TurnstileRenderOptions {
  sitekey: string;
  /** The visitor passed: a single-use token. */
  callback: (token: string) => void;
  /** The token went stale before it was used. */
  "expired-callback": () => void;
  /** The widget failed (network, a bad key, an unsupported browser). */
  "error-callback": () => void;
}

/** The three calls of Cloudflare's API that the shell uses. */
export interface TurnstileApi {
  /** Draw a widget inside `host`; returns its id. */
  render: (host: HTMLElement, options: TurnstileRenderOptions) => string;
  /** Remove a widget and its frame. */
  remove: (id: string) => void;
  /** Ask for a fresh challenge and token (a token is single-use). */
  reset: (id: string) => void;
}

/** The API when the script has defined a usable `window.turnstile`, else null. */
export function currentTurnstile(): TurnstileApi | null {
  const api = (window as unknown as { turnstile?: Partial<TurnstileApi> }).turnstile;
  if (api && typeof api.render === "function" && typeof api.remove === "function" && typeof api.reset === "function") {
    return api as TurnstileApi;
  }
  return null;
}

let pending: Promise<TurnstileApi | null> | null = null;

/** Forget the shared load (tests, and nothing else). */
export function resetTurnstileScriptCache(): void {
  pending = null;
}

function appendScript(resolve: (api: TurnstileApi | null) => void, forget: () => void): void {
  const script = document.createElement("script");
  const finish = (api: TurnstileApi | null) => {
    Reflect.deleteProperty(window, TURNSTILE_ONLOAD);
    if (api === null) {
      script.remove(); // a failed load leaves nothing behind, so a later call can try again
      forget();
    }
    resolve(api);
  };
  (window as unknown as Record<string, () => void>)[TURNSTILE_ONLOAD] = () => finish(currentTurnstile());
  script.addEventListener("load", () => finish(currentTurnstile()));
  script.addEventListener("error", () => finish(null));
  script.src = TURNSTILE_SCRIPT_SRC;
  script.async = true;
  script.defer = true;
  document.head.append(script);
}

/**
 * The Turnstile API, loading the Cloudflare script first when it is not there. One script however many callers (React
 * StrictMode runs an effect twice): they share one promise. Resolves null, never rejects, when the script fails to load or
 * defines no usable API; that failure is not cached.
 */
export function loadTurnstileScript(): Promise<TurnstileApi | null> {
  const ready = currentTurnstile();
  if (ready !== null) return Promise.resolve(ready);
  if (pending !== null) return pending;
  const run: Promise<TurnstileApi | null> = new Promise((resolve) => {
    appendScript(resolve, () => {
      if (pending === run) pending = null;
    });
  });
  pending = run;
  return run;
}

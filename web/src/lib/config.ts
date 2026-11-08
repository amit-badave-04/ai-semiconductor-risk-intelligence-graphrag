// R8 client: GET /api/config once (and once more after a failure). The answer is the site key state of the Turnstile gate:
//   * key      - a site key is set: the shell mounts the one widget;
//   * none     - a 200 with an empty key: no widget, asks are sent without a token (the legacy page does the same);
//   * unknown  - not answered (still loading) or answered badly: `failed` says which. "Unknown" is NEVER treated as "none":
//                no widget mounts, and the page does not claim there is no bot check. Asks are sent either way (R33): the
//                server is the gate.
// The site key is public by design. Nothing here is a secret.

export const CONFIG_URL = "/api/config";

export type SiteKeyState =
  | { kind: "unknown"; failed: boolean }
  | { kind: "none" }
  | { kind: "key"; siteKey: string };

export interface LoadConfigOptions {
  /** Pause before the single retry. Default 500 ms; tests pass 0. */
  retryDelayMs?: number;
}

const DEFAULT_RETRY_DELAY_MS = 500;
// A Cloudflare site key is short and made of these characters; anything else is a malformed answer, not a key to inject.
const SITE_KEY_FORM = /^[0-9A-Za-z_-]{1,128}$/;

/** The state a 200 body means, or null when the body is not `{turnstile_site_key: string}` with an empty or well-formed key. */
export function parseConfig(body: unknown): SiteKeyState | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const key = (body as Record<string, unknown>).turnstile_site_key;
  if (typeof key !== "string") return null;
  if (key === "") return { kind: "none" };
  return SITE_KEY_FORM.test(key) ? { kind: "key", siteKey: key } : null;
}

async function attempt(): Promise<SiteKeyState | null> {
  try {
    const response = await fetch(CONFIG_URL);
    if (!response.ok) return null;
    return parseConfig(await response.json());
  } catch {
    return null; // a network error or a body that is not JSON
  }
}

async function request(retryDelayMs: number): Promise<SiteKeyState> {
  const first = await attempt();
  if (first !== null) return first;
  if (retryDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
  return (await attempt()) ?? { kind: "unknown", failed: true };
}

let pending: Promise<SiteKeyState> | null = null;

/** Forget the cached answer (tests, and nothing else). */
export function resetConfigCache(): void {
  pending = null;
}

/**
 * The site key state. Never rejects. One request per page load: a settled answer (key or none) is cached and shared with every
 * caller, including the second effect run of React StrictMode; a failure (two attempts) is not cached, so a later call asks
 * again. It takes no abort signal because the in-flight request is shared; a caller that unmounts ignores the result.
 */
export function loadConfig(options: LoadConfigOptions = {}): Promise<SiteKeyState> {
  if (pending !== null) return pending;
  const run = request(options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS).then((state) => {
    if (state.kind === "unknown" && pending === run) pending = null;
    return state;
  });
  pending = run;
  return run;
}

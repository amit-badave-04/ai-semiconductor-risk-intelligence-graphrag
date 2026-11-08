// The Turnstile gate's context, its value type and the hook (R33). The provider is state/TurnstileProvider.tsx; this file holds
// everything else so the provider file exports only a component (fast refresh).
import { createContext, useContext } from "react";
import type { SiteKeyState } from "@/lib/config";

export interface TurnstileContextValue {
  /** What /api/config said: `unknown` (loading, or failed: see `failed`), `none` (no widget, asks go without a token) or `key`.
   *  "Unknown" is never "none". */
  siteKeyState: SiteKeyState;
  /** True exactly when a site key is set (state `key`). Uploads wait for a token only then (R33(2)). */
  siteKeySet: boolean;
  /** The current token without using it, or null. */
  peek: () => string | null;
  /** Take the current token and use it up: the token is cleared and the widget reset (a fresh challenge). Null when there is
   *  none. A token is never returned twice. */
  consume: () => string | null;
  /**
   * Resolve with a token, ALREADY CONSUMED (the widget is reset), as soon as one exists; the caller must not call `consume`
   * for it. Answers at once with an existing token. Resolves null, never rejects, after `ms` milliseconds, when `signal`
   * aborts (also if it was already aborted), when no site key is set, or when the shell unmounts. Waiters are served one token
   * each, first come first served. Callers: uploads (15 s, only when `siteKeySet` and `peek()` is null).
   */
  waitForToken: (ms: number, signal?: AbortSignal) => Promise<string | null>;
  /** Drop the token and reset the widget without handing the token to anyone. */
  clear: () => void;
  /** Used by TurnstileWidget only: the element the widget is drawn in. */
  registerHost: (element: HTMLElement | null) => void;
}

export const INERT_TURNSTILE: TurnstileContextValue = {
  siteKeyState: { kind: "unknown", failed: false },
  siteKeySet: false,
  peek: () => null,
  consume: () => null,
  waitForToken: () => Promise.resolve(null),
  clear: () => {},
  registerHost: () => {},
};

export const TurnstileContext = createContext<TurnstileContextValue>(INERT_TURNSTILE);

/** The Turnstile gate. Outside a provider it is inert (unknown, no key, no token), which keeps component tests simple. */
export function useTurnstile(): TurnstileContextValue {
  return useContext(TurnstileContext);
}

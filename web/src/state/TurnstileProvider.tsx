// R33(3), R8: the one Turnstile widget of the app shell and the single-use token it yields. The provider sits above the router,
// so a route change neither re-renders nor re-challenges the widget. It reads GET /api/config once (R8), loads the Cloudflare
// script only when a key exists, renders ONE widget inside the host the shell mounts (TurnstileWidget), and keeps the token in a
// ref. The widget is rendered by an effect with a cleanup that removes it, so a StrictMode double mount, a remount and a key
// change each leave ONE live widget (the render is the one effect the page needs; R31 forbids effects only for requests).
// There is one token, consumed by whichever request uses it first. The work is the hooks in ./turnstileHooks.ts (site key, token
// store, widget, waiter queue); this file is the wiring and the context value. The context's type and hook are in ./useTurnstile.
import { useMemo, useState, type ReactNode } from "react";
import { useSiteKey, useTokenStore, useTokenWaiters, useTurnstileWidget } from "./turnstileHooks";
import { TurnstileContext, type TurnstileContextValue } from "./useTurnstile";

export function TurnstileProvider({ children }: { children: ReactNode }) {
  const siteKeyState = useSiteKey();
  const [host, setHost] = useState<HTMLElement | null>(null);
  const store = useTokenStore();
  const siteKey = siteKeyState.kind === "key" ? siteKeyState.siteKey : null;
  const { offer, waitForToken } = useTokenWaiters(store, siteKey !== null);
  useTurnstileWidget(host, siteKey, store, offer);

  const { peek, consume, clear } = store;
  const value = useMemo<TurnstileContextValue>(
    () => ({ siteKeyState, siteKeySet: siteKey !== null, peek, consume, waitForToken, clear, registerHost: setHost }),
    [siteKeyState, siteKey, peek, consume, waitForToken, clear],
  );
  return <TurnstileContext.Provider value={value}>{children}</TurnstileContext.Provider>;
}

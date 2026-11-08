// The hooks behind TurnstileProvider (R33(3), R8): the site key, the token store, the widget and the waiter queue. Split out so the
// provider is only wiring and the context value.
import { useCallback, useEffect, useRef, useState } from "react";
import { loadConfig, type SiteKeyState } from "@/lib/config";
import { currentTurnstile, loadTurnstileScript, type TurnstileApi } from "@/lib/turnstile";

interface Waiter {
  resolve: (token: string | null) => void;
}

export interface Widget {
  api: TurnstileApi;
  id: string;
}

export interface TokenStore {
  peek: () => string | null;
  /** Take the token and use it up: it is cleared and the widget reset. Null when there is none. */
  consume: () => string | null;
  /** Drop the token and reset the widget without handing the token to anyone. */
  clear: () => void;
  /** The widget `id` solved: its token becomes the current one. False (and nothing changes) when `id` is not the live widget. */
  accept: (id: string, value: string) => boolean;
  /** The widget `id` expired or errored: drop the token, only if `id` is the live widget. */
  expire: (id: string) => void;
  /** Make `widget` the live one. */
  adopt: (widget: Widget) => void;
  /** The effect that owned `widget` ends: it stops being the live one and the token is dropped. */
  release: (widget: Widget | null) => void;
}

/** What /api/config said (read once, R8): `unknown` until it answers, then `none` or `key`. */
export function useSiteKey(): SiteKeyState {
  const [state, setState] = useState<SiteKeyState>({ kind: "unknown", failed: false });
  useEffect(() => {
    let live = true;
    void loadConfig().then((loaded) => {
      if (live) setState(loaded);
    });
    return () => {
      live = false;
    };
  }, []);
  return state;
}

/** The one token (a ref) and the widget handle it is reset through. */
export function useTokenStore(): TokenStore {
  const token = useRef<string | null>(null);
  const widget = useRef<Widget | null>(null);
  const reset = useCallback(() => {
    if (widget.current) widget.current.api.reset(widget.current.id);
  }, []);
  const consume = useCallback((): string | null => {
    const used = token.current;
    if (used === null) return null;
    token.current = null;
    reset();
    return used;
  }, [reset]);
  const clear = useCallback(() => {
    token.current = null;
    reset();
  }, [reset]);
  const peek = useCallback(() => token.current, []);
  const accept = useCallback((id: string, value: string): boolean => {
    if (widget.current?.id !== id) return false;
    token.current = value;
    return true;
  }, []);
  const expire = useCallback((id: string) => {
    if (widget.current?.id === id) token.current = null;
  }, []);
  const adopt = useCallback((next: Widget) => {
    widget.current = next;
  }, []);
  const release = useCallback((ended: Widget | null) => {
    if (ended !== null && widget.current === ended) widget.current = null;
    token.current = null;
  }, []);
  return { peek, consume, clear, accept, expire, adopt, release };
}

/**
 * ONE widget in `host`, rendered by an effect whose cleanup removes it, so a StrictMode double mount, a remount and a key change each
 * leave one live widget. A late callback of a widget this effect already removed never becomes the current token.
 */
export function useTurnstileWidget(host: HTMLElement | null, siteKey: string | null, store: TokenStore, offer: () => void): void {
  const { accept, expire, adopt, release } = store;
  useEffect(() => {
    if (siteKey === null || host === null) return undefined;
    const lifetime = new AbortController();
    let mine: Widget | null = null;
    const start = (api: TurnstileApi) => {
      if (lifetime.signal.aborted) return;
      // A callback may run synchronously inside render(), before the id exists: it is kept and applied once the widget is adopted.
      let id: string | undefined;
      const early: { value: string | null } = { value: null };
      const solved = (value: string) => {
        if (id === undefined) early.value = value;
        else if (accept(id, value)) offer();
      };
      const drop = () => {
        if (id === undefined) early.value = null;
        else expire(id);
      };
      id = api.render(host, { sitekey: siteKey, callback: solved, "expired-callback": drop, "error-callback": drop });
      mine = { api, id };
      adopt(mine);
      if (early.value !== null) solved(early.value);
    };
    const ready = currentTurnstile();
    if (ready !== null) start(ready); // already loaded: render in this very effect run
    else {
      void loadTurnstileScript().then((api) => {
        if (api !== null) start(api);
      });
    }
    return () => {
      lifetime.abort();
      if (mine !== null) mine.api.remove(mine.id);
      release(mine);
    };
  }, [siteKey, host, offer, accept, expire, adopt, release]);
}

export interface TokenWaiters {
  offer: () => void;
  waitForToken: (ms: number, signal?: AbortSignal) => Promise<string | null>;
}

/** The queue of callers waiting for a token: one token each, first come first served; all resolve null when the shell unmounts. */
export function useTokenWaiters(store: TokenStore, siteKeySet: boolean): TokenWaiters {
  const { peek, consume } = store;
  const waiters = useRef<Waiter[]>([]);
  const siteKeySetRef = useRef(false);
  useEffect(() => {
    siteKeySetRef.current = siteKeySet;
  }, [siteKeySet]);

  const offer = useCallback(() => {
    const next = waiters.current[0];
    if (next === undefined || peek() === null) return;
    waiters.current.shift();
    next.resolve(consume());
  }, [consume, peek]);

  useEffect(
    () => () => {
      const pending = waiters.current;
      waiters.current = [];
      for (const waiter of pending) waiter.resolve(null);
    },
    [],
  );

  const waitForToken = useCallback(
    (ms: number, signal?: AbortSignal): Promise<string | null> => {
      if (signal?.aborted || !siteKeySetRef.current) return Promise.resolve(null);
      const ready = consume();
      if (ready !== null) return Promise.resolve(ready);
      return new Promise((resolve) => {
        const waiter: Waiter = { resolve: (value) => settle(value) };
        const timer = setTimeout(() => settle(null), ms);
        const onAbort = () => settle(null);
        const settle = (value: string | null) => {
          clearTimeout(timer);
          signal?.removeEventListener("abort", onAbort);
          waiters.current = waiters.current.filter((w) => w !== waiter);
          resolve(value);
        };
        signal?.addEventListener("abort", onAbort, { once: true });
        waiters.current.push(waiter);
      });
    },
    [consume],
  );
  return { offer, waitForToken };
}

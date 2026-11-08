// R31 / R33(1) / S12: the ask and its stream live here, above the router, so a route change neither aborts the stream nor loses the
// answer. The contract is state/useAsk.ts. The rules this file keeps:
//   * an ask starts only from `ask()`, which a handler calls; this file never starts one from an effect (StrictMode re-runs effects,
//     which would POST twice and spend twice). The "one at a time" guard is a REF, so two quick calls cannot both read an idle state;
//   * the POST is made once and never retried, and it is never held back for a bot-check token (the server answers a cached example
//     before the bot check): it carries the token the shell has, or none, and the token is used up at send time (consume resets the
//     widget, so a token solved during the stream is not thrown away by this file);
//   * the stream is aborted only by `abort()` or by the page unloading, and the state says so (`aborted`), never "connection lost";
//   * every way out leaves a true state (askState.finishState), and /api/stats is reloaded after every ask (the legacy loadStats).
// How the contract's loose ends are read: a refusal (403, 429, 503...) is `ended` with `refusal` set (the server's word is final); a
// cut stream, a request that never connected, a frame over the buffer limit and a page failure are `failed`, each with its own
// statusText; `aborted` has no status text (the page has no sentence for it). A page failure is also reported with `reportError`,
// as the legacy page's rethrow was, so it is never hidden.
// The run, the one-at-a-time guard and the unload stop are the hooks in ./useAskRun.ts; this file is the wiring and the context value.
import { useMemo, type ReactNode } from "react";
import { AskContext, type AskContextValue } from "./useAsk";
import { useAskLifecycle, useAskRun } from "./useAskRun";

export function AskProvider({ children }: { children: ReactNode }) {
  const { state, ask, abort, reset, stop } = useAskRun();
  useAskLifecycle(stop);
  const value = useMemo<AskContextValue>(() => ({ ...state, ask, abort, reset }), [state, ask, abort, reset]);
  return <AskContext.Provider value={value}>{children}</AskContext.Provider>;
}

// The hooks behind AskProvider (R31): the run of one ask (`useAskRun`) and the page-unload stop (`useAskLifecycle`). The rules they
// keep are written in AskProvider.tsx. Split out so the provider is only wiring and the context value.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from "react";
import { askStream, type AskBody } from "@/lib/sse";
import { refreshStats } from "@/lib/statsStore";
import { applyEvent, finishState, startState } from "./askState";
import { IDLE_ASK_STATE, type AskRequest, type AskState } from "./useAsk";
import { useTurnstile } from "./useTurnstile";
import { useWorkspace } from "./useWorkspace";

const WORKSPACE_TOKEN_HEADER = "X-Workspace-Token";

type Workspace = ReturnType<typeof useWorkspace>;
type Turnstile = ReturnType<typeof useTurnstile>;
interface Live {
  workspace: Workspace;
  turnstile: Turnstile;
}

function askBody(request: AskRequest, token: string | null, workspace: { id: string; asOf: string } | null): AskBody {
  const body: AskBody = { question: request.question.trim() };
  if (request.strategy) body.strategy = request.strategy;
  if (token !== null) body.turnstile_token = token;
  if (workspace !== null) {
    body.workspace_id = workspace.id;
    if (workspace.asOf) body.as_of = workspace.asOf;
  }
  return body;
}

/** One ask, start to finish: the token is used up, the state is published on every event and on the way out. */
function useStreamRun(publish: (next: AskState) => void, accumulated: MutableRefObject<AskState>, live: MutableRefObject<Live>) {
  return useCallback(
    async (request: AskRequest, signal: AbortSignal) => {
      const { workspace: ws, turnstile: gate } = live.current;
      const token = gate.consume(); // used up at send time: the widget is reset, a fresh challenge starts
      if (token === null && gate.siteKeySet) gate.clear(); // no token to use up: still reset, as R33(3) says, index.html:706, 675, 1019, 1050
      const session = ws.workspace === null ? null : { id: ws.workspace.id, asOf: ws.asOf };
      const connection = { reached: false };
      const fetchImpl: typeof fetch = async (input, init) => {
        const response = await fetch(input, init);
        connection.reached = true;
        return response;
      };
      publish(startState(accumulated.current, request.question.trim(), request.strategy ?? ""));
      const outcome = await askStream(askBody(request, token, session), {
        onEvent: (event) => publish(applyEvent(accumulated.current, event)),
        signal,
        headers: session === null ? undefined : { [WORKSPACE_TOKEN_HEADER]: ws.headers()[WORKSPACE_TOKEN_HEADER] ?? "" },
        fetchImpl,
      });
      publish(finishState(accumulated.current, { outcome, networkError: !connection.reached && !outcome.aborted, siteKeySet: gate.siteKeySet, tokenHeld: token !== null }));
      if (outcome.thrown !== undefined) reportError(outcome.thrown);
    },
    [publish, accumulated, live],
  );
}

/** The ask state and its actions: `ask` (one at a time, a REF guard), `abort`, `reset`, and `stop` (abort whatever is open). */
export function useAskRun() {
  const workspace = useWorkspace();
  const turnstile = useTurnstile();
  const [state, setState] = useState<AskState>(IDLE_ASK_STATE);
  const accumulated = useRef<AskState>(IDLE_ASK_STATE); // what the stream handler builds; `state` is what is shown
  const streaming = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const live = useRef<Live>({ workspace, turnstile });
  useLayoutEffect(() => {
    live.current = { workspace, turnstile }; // read by handlers and the stream, which run after this commit
  });
  const publish = useCallback((next: AskState) => {
    accumulated.current = next;
    setState(next);
  }, []);
  const run = useStreamRun(publish, accumulated, live);

  const ask = useCallback(
    (request: AskRequest) => {
      if (!request.question.trim() || streaming.current) return;
      streaming.current = true;
      const own = new AbortController();
      controller.current = own;
      void run(request, own.signal).finally(() => {
        streaming.current = false;
        controller.current = null;
        void refreshStats();
      });
    },
    [run],
  );
  const abort = useCallback(() => {
    if (streaming.current) controller.current?.abort();
  }, []);
  const stop = useCallback(() => controller.current?.abort(), []);
  const reset = useCallback(() => publish({ ...IDLE_ASK_STATE, seq: accumulated.current.seq }), [publish]);
  return { state, ask, abort, reset, stop };
}

/** The page unloading stops the open stream (R31); so does the provider going away for good. */
export function useAskLifecycle(stop: () => void): void {
  useEffect(() => {
    window.addEventListener("pagehide", stop);
    return () => {
      window.removeEventListener("pagehide", stop);
      stop();
    };
  }, [stop]);
}

// The logic of the evidence drawer (R24 / A5 / A2 / S4), in hooks, so that the provider (EvidenceDrawer.tsx) is only wiring and markup.
// It ports openDrawer / closeDrawer / openSeq / chipPlace of the legacy page: `open` takes the opener, fetches (or reads the cache),
// and lets only the LAST open fill the drawer; `close` decides where focus goes, after the commit that hides the drawer; a new workspace
// identity closes the drawer and clears the cache, and a new doc-evidence epoch drops the cached `doc:` evidence.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type MutableRefObject, type RefObject, type SetStateAction } from "react";
import { rememberChunkEvidence } from "@/lib/chipEvidence";
import { EvidenceCache } from "@/lib/evidenceCache";
import { fallbackFocusTarget } from "@/lib/focusTargets";
import { useWorkspace, type WorkspaceContextValue } from "@/state/useWorkspace";
import { fetchEvidence, refusalFor } from "./evidenceFetch";
import type { EvidenceDrawerApi } from "./useEvidenceDrawer";

export type Content = { phase: "loading" } | { phase: "error"; message: string } | { phase: "loaded"; payload: unknown };
type Restore = { target: HTMLElement | null; tick: number };
type Opener = { element: HTMLElement | null; key: string | null };

const OPENER_KEY_ATTR = "data-chip-key";

/** The chip that now has `key`, wherever the answer re-rendered, or null (a new answer replaced it). */
function chipWithKey(key: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[${OPENER_KEY_ATTR}="${key.replace(/["\\]/g, "\\$&")}"]`);
}

/** Where focus goes on close (A2): the opener; if it is gone, the same chip in the re-rendered text; if that is gone, the heading. */
function focusTargetAfterClose(opener: HTMLElement | null, key: string | null): HTMLElement | null {
  // React may keep the same button for a different chip when a new answer re-renders in place: the key says whether it is still ours.
  const same = opener !== null && opener.isConnected && (key === null || opener.getAttribute(OPENER_KEY_ATTR) === key);
  if (same) return opener;
  return (key === null ? null : chipWithKey(key)) ?? fallbackFocusTarget();
}

interface Shell {
  cache: MutableRefObject<EvidenceCache<unknown>>;
  isOpen: MutableRefObject<boolean>;
  openSeq: MutableRefObject<number>;
  opener: MutableRefObject<Opener>;
  /** The evidence GET in flight, if any: aborted by the next open, by close and by the provider going away. */
  fetching: MutableRefObject<AbortController | null>;
  live: MutableRefObject<Pick<WorkspaceContextValue, "workspace" | "headers">>;
}

function useShell(workspace: WorkspaceContextValue["workspace"], headers: WorkspaceContextValue["headers"]): Shell {
  // One stable object of mutable boxes for the life of the provider, so the callbacks built from it keep their identity.
  const [shell] = useState<Shell>(() => ({
    cache: { current: new EvidenceCache<unknown>() },
    isOpen: { current: false },
    openSeq: { current: 0 },
    opener: { current: { element: null, key: null } },
    fetching: { current: null },
    live: { current: { workspace, headers } },
  }));
  useLayoutEffect(() => {
    put(shell.live, { workspace, headers }); // read by `open`, which runs after this commit
  });
  return shell;
}

/** Write a box. (The boxes are plain mutable cells owned by one provider; these two helpers are the only writers.) */
function put<T>(box: { current: T }, value: T): void {
  box.current = value;
}
function cancelFetch(shell: Shell): void {
  shell.fetching.current?.abort();
  put(shell.fetching, null);
}
function bump(box: { current: number }): number {
  box.current += 1;
  return box.current;
}

type SetOpen = Dispatch<SetStateAction<string | null>>;
type SetContent = Dispatch<SetStateAction<Content>>;

function useOpen(shell: Shell, setOpenId: SetOpen, setContent: SetContent, setFocusClose: Dispatch<SetStateAction<number>>) {
  const show = useCallback(
    (id: string, seq: number, next: Content) => {
      if (seq !== shell.openSeq.current) return; // a stale response never fills the drawer (the old openSeq rule)
      if (next.phase === "loaded") rememberChunkEvidence(id, next.payload);
      setContent(next);
    },
    [shell, setContent],
  );
  return useCallback<EvidenceDrawerApi["open"]>(
    (id, element, _place) => {
      const seq = bump(shell.openSeq);
      cancelFetch(shell);
      const previous = shell.opener.current;
      const from = element ?? (shell.isOpen.current ? previous.element : (document.activeElement as HTMLElement | null));
      put(shell.opener, { element: from, key: element ? element.getAttribute(OPENER_KEY_ATTR) : shell.isOpen.current ? previous.key : null });
      put(shell.isOpen, true);
      setOpenId(id);
      setFocusClose((n) => n + 1);
      const cached = shell.cache.current.get(id);
      if (cached !== undefined) return show(id, seq, { phase: "loaded", payload: cached });
      const ws = shell.live.current.workspace;
      const session = ws === null ? null : { id: ws.id, headers: shell.live.current.headers };
      const refusal = refusalFor(id, session);
      if (refusal !== null) return show(id, seq, { phase: "error", message: refusal });
      setContent({ phase: "loading" });
      const ticket = shell.cache.current.begin();
      const own = new AbortController();
      put(shell.fetching, own);
      void fetchEvidence(id, session, fetch, own.signal).then((outcome) => {
        if (own.signal.aborted) return; // closed, replaced or torn down meanwhile: nothing is stored or shown
        if (outcome.ok) shell.cache.current.put(id, outcome.payload, ticket);
        show(id, seq, outcome.ok ? { phase: "loaded", payload: outcome.payload } : { phase: "error", message: outcome.message });
      });
    },
    [shell, show, setOpenId, setContent, setFocusClose],
  );
}

function useClose(shell: Shell, aside: RefObject<HTMLElement | null>, setOpenId: SetOpen, setRestore: Dispatch<SetStateAction<Restore>>) {
  return useCallback<EvidenceDrawerApi["close"]>(
    ({ restoreFocus = true } = {}) => {
      if (!shell.isOpen.current) return;
      put(shell.isOpen, false);
      cancelFetch(shell);
      const active = document.activeElement;
      const focusWasHere = !active || active === document.body || (aside.current?.contains(active) ?? false);
      const target = restoreFocus && focusWasHere ? focusTargetAfterClose(shell.opener.current.element, shell.opener.current.key) : null;
      put(shell.opener, { element: null, key: null });
      setOpenId(null);
      setRestore((r) => ({ target, tick: r.tick + 1 }));
    },
    [shell, aside, setOpenId, setRestore],
  );
}

export interface DrawerController {
  api: EvidenceDrawerApi;
  content: Content;
  aside: RefObject<HTMLElement | null>;
  closeButton: RefObject<HTMLButtonElement | null>;
}

/** Everything the drawer provider needs: the API it publishes, what the drawer shows, and the two elements it must reach. */
export function useDrawerController(): DrawerController {
  const { workspace, headers, generation, docEvidenceEpoch } = useWorkspace();
  const shell = useShell(workspace, headers);
  const aside = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [content, setContent] = useState<Content>({ phase: "loading" });
  const [focusClose, setFocusClose] = useState(0);
  const [restore, setRestore] = useState<Restore>({ target: null, tick: 0 });
  const open = useOpen(shell, setOpenId, setContent, setFocusClose);
  const close = useClose(shell, aside, setOpenId, setRestore);
  useEffect(() => () => cancelFetch(shell), [shell]); // the provider going away cancels the request in flight
  const dropDocEvidence = useCallback(() => shell.cache.current.dropDoc(), [shell]);
  const clearAll = useCallback(() => {
    shell.cache.current.clear();
    bump(shell.openSeq);
    close({ restoreFocus: false });
  }, [shell, close]);

  // Layout effects: the cache is already dropped when a fetch that completes after this commit asks to store (no window).
  useLayoutEffect(() => shell.cache.current.sync(docEvidenceEpoch), [shell, docEvidenceEpoch]);
  const seenGeneration = useRef(generation);
  useEffect(() => {
    if (seenGeneration.current === generation) return;
    seenGeneration.current = generation;
    clearAll();
  }, [generation, clearAll]);
  // Focus changes happen after the commit that un-inerts (or hides) the drawer: an inert element cannot take focus.
  useLayoutEffect(() => {
    if (focusClose > 0) closeButton.current?.focus();
  }, [focusClose]);
  useLayoutEffect(() => {
    if (restore.tick > 0) restore.target?.focus();
  }, [restore]);
  useEscapeToClose(openId !== null, close);

  const api = useMemo<EvidenceDrawerApi>(() => ({ openId, open, close, dropDocEvidence, clearAll }), [openId, open, close, dropDocEvidence, clearAll]);
  return { api, content, aside, closeButton };
}

function useEscapeToClose(isOpen: boolean, close: EvidenceDrawerApi["close"]) {
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (event: KeyboardEvent) => {
      // docs/specs/m5b-redesign.md SR9: an Escape something above the drawer already handled (an open popover) is not ours
      if (event.key === "Escape" && !event.defaultPrevented) close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, close]);
}

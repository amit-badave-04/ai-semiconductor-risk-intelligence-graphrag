// The legacy page upgraded the label of EVERY chip with an id once that chunk's evidence had loaded (`10-K FY2026 - Item 1A`, from
// the form and period). This is the small store that does it without a second context: the drawer writes the payload of a public chunk
// after a fetch (or a cache hit), and a chip reads its own id. A public chunk never changes, so nothing here is ever cleared by the
// workspace; `doc:` evidence is never stored (the drawer shows its title, the chip keeps its bare label, as before).
import { useSyncExternalStore } from "react";
import { classifyId } from "./citations";

const payloads = new Map<string, unknown>();
const listeners = new Set<() => void>();

export function rememberChunkEvidence(id: string, payload: unknown): void {
  if (classifyId(id) !== "chunk" || payloads.get(id) === payload) return;
  payloads.set(id, payload);
  for (const listener of Array.from(listeners)) listener();
}

/** Tests only. */
export function resetChipEvidence(): void {
  payloads.clear();
  for (const listener of Array.from(listeners)) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** The payload of the chunk `id` once the drawer has loaded it, else undefined. */
export function useChipEvidence(id: string): unknown {
  return useSyncExternalStore(subscribe, () => payloads.get(id), () => undefined); // no evidence on a server render
}

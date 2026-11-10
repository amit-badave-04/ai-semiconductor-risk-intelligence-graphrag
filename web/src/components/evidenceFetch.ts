// R24 / S4: the one place an evidence request is made. An id comes from untrusted model text, so it is fetched only after an anchored
// match of the generated grammar (classifyId) and the URL is built with encodeURIComponent; a `doc:` id with no workspace open and
// an id outside the grammar make NO request and answer with the server's own words. The response is read through detailOf, which
// bounds what is read and never shows an upstream page.
import { deck } from "@/copy/deck";
import { classifyId } from "@/lib/citations";
import { detailOf } from "@/lib/refusal";
import { MSG_MALFORMED_EVIDENCE, MSG_NOT_PUBLIC_EVIDENCE } from "@/pages/Ask.copy";

export type EvidenceOutcome = { ok: true; payload: unknown } | { ok: false; message: string };

export interface EvidenceWorkspace {
  id: string;
  headers: () => Record<string, string>;
}

/** Why an id must not be fetched, or null when it may be. */
export function refusalFor(id: string, workspace: EvidenceWorkspace | null): string | null {
  const kind = classifyId(id);
  if (kind === null) return MSG_MALFORMED_EVIDENCE;
  if (kind === "doc" && workspace === null) return MSG_NOT_PUBLIC_EVIDENCE;
  return null;
}

/** The request path of an id that passed refusalFor. */
export function evidencePath(id: string, workspace: EvidenceWorkspace | null): string {
  const encoded = encodeURIComponent(id);
  return classifyId(id) === "doc" && workspace !== null
    ? `/api/workspace/${encodeURIComponent(workspace.id)}/evidence/${encoded}`
    : `/api/evidence/${encoded}`;
}

/** Fetch the evidence of `id`. Resolves, never rejects: a refusal, a failed request and a bad body all come back as a message. */
export async function fetchEvidence(id: string, workspace: EvidenceWorkspace | null, fetchImpl: typeof fetch = fetch, signal?: AbortSignal): Promise<EvidenceOutcome> {
  const refusal = refusalFor(id, workspace);
  if (refusal !== null) return { ok: false, message: refusal };
  const headers = classifyId(id) === "doc" && workspace !== null ? workspace.headers() : undefined;
  try {
    const response = await fetchImpl(evidencePath(id, workspace), { ...(headers ? { headers } : {}), ...(signal ? { signal } : {}) });
    if (!response.ok) return { ok: false, message: await detailOf(response) };
    return { ok: true, payload: (await response.json()) as unknown };
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : deck.errors.network };
  }
}

// docs/specs/m5b-redesign.md RD4, SR3: each specimen's evidence is fetched once per page load, through the one evidence path
// (components/evidenceFetch.ts: classifyId first, encodeURIComponent, no request for an id outside the grammar).
import { fetchEvidence, type EvidenceOutcome } from "@/components/evidenceFetch";

const outcomes = new Map<string, Promise<EvidenceOutcome>>();

/** The evidence of a public specimen id; a failure is not kept, so the next opening tries again. */
export function specimenEvidence(id: string): Promise<EvidenceOutcome> {
  const known = outcomes.get(id);
  if (known !== undefined) return known;
  const pending = fetchEvidence(id, null).then((outcome) => {
    if (!outcome.ok) outcomes.delete(id);
    return outcome;
  });
  outcomes.set(id, pending);
  return pending;
}

/** Tests only: forget every outcome. */
export function resetSpecimenCache(): void {
  outcomes.clear();
}

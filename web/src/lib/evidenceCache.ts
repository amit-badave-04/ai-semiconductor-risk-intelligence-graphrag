import { classifyId } from "./citations";

/**
 * The evidence drawer's cache (R24), the legacy `evidenceCache` plus `docEvidenceGen` (index.html:736-743, 799-806).
 *
 * Only `doc:` evidence can go stale: a new upload version changes the status of an older version's chunks. Dropping it must also
 * defeat a `doc:` fetch still in flight, so a fetch takes a ticket from {@link begin} and hands it back to {@link put}, which
 * refuses a `doc:` payload whose ticket is older than the last drop. Evidence of the other kinds is never stale this way and is
 * stored whatever the ticket says.
 */
export class EvidenceCache<T> {
  private readonly entries = new Map<string, T>();
  private version = 0;
  private epoch: number | null = null;

  /** The ticket a fetch carries from its start to its {@link put}. */
  begin(): number {
    return this.version;
  }

  get(id: string): T | undefined {
    return this.entries.get(id);
  }

  /** Store a payload; false (and nothing stored) for a `doc:` payload fetched before the last drop. */
  put(id: string, payload: T, ticket: number): boolean {
    if (classifyId(id) === "doc" && ticket !== this.version) return false;
    this.entries.set(id, payload);
    return true;
  }

  /** Forget the `doc:` entries and outdate every `doc:` fetch in flight. */
  dropDoc(): void {
    this.version += 1;
    for (const id of Array.from(this.entries.keys())) if (classifyId(id) === "doc") this.entries.delete(id);
  }

  /** Forget everything and outdate every `doc:` fetch in flight (the workspace identity changed). */
  clear(): void {
    this.version += 1;
    this.entries.clear();
  }

  /** Follow the workspace's doc-evidence epoch: a value other than the last seen drops the `doc:` entries once. */
  sync(epoch: number): void {
    if (this.epoch === epoch) return;
    const first = this.epoch === null;
    this.epoch = epoch;
    if (!first) this.dropDoc();
  }
}

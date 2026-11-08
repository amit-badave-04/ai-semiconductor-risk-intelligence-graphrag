import { describe, expect, it } from "vitest";
import { EvidenceCache } from "../evidenceCache";

const CHUNK = "0001045810-26-000021:I.1A:0361";
const DOC = "doc:0123456789ab:v2:0007";

// The drawer's cache (R24, the legacy evidenceCache + docEvidenceGen of index.html:736-743, 799-806): only `doc:` evidence can go
// stale (a new upload version changes the status of an older version's chunks), and a `doc:` fetch still in flight when it is
// dropped must not put its now-outdated payload back.
describe("EvidenceCache", () => {
  it("returns what was put, for any kind of id", () => {
    const cache = new EvidenceCache<string>();
    expect(cache.put(CHUNK, "a", cache.begin())).toBe(true);
    expect(cache.put(DOC, "b", cache.begin())).toBe(true);
    expect(cache.get(CHUNK)).toBe("a");
    expect(cache.get(DOC)).toBe("b");
    expect(cache.get("nothing")).toBeUndefined();
  });

  it("dropDoc forgets doc: entries and keeps every other kind", () => {
    const cache = new EvidenceCache<string>();
    cache.put(CHUNK, "a", cache.begin());
    cache.put(DOC, "b", cache.begin());
    cache.dropDoc();
    expect(cache.get(DOC)).toBeUndefined();
    expect(cache.get(CHUNK)).toBe("a");
  });

  it("refuses a doc: result whose fetch began before a drop, and still takes a non-doc one", () => {
    const cache = new EvidenceCache<string>();
    const started = cache.begin();
    cache.dropDoc();
    expect(cache.put(DOC, "old", started)).toBe(false);
    expect(cache.get(DOC)).toBeUndefined();
    expect(cache.put(CHUNK, "fine", started)).toBe(true);
    expect(cache.put(DOC, "new", cache.begin())).toBe(true);
  });

  it("clear forgets everything and also refuses the in-flight doc: result", () => {
    const cache = new EvidenceCache<string>();
    cache.put(CHUNK, "a", cache.begin());
    const started = cache.begin();
    cache.clear();
    expect(cache.get(CHUNK)).toBeUndefined();
    expect(cache.put(DOC, "old", started)).toBe(false);
  });

  it("sync drops doc: entries once per new epoch and not again for the same epoch", () => {
    const cache = new EvidenceCache<string>();
    cache.sync(0);
    cache.put(DOC, "b", cache.begin());
    cache.sync(0);
    expect(cache.get(DOC)).toBe("b");
    const started = cache.begin();
    cache.sync(1);
    expect(cache.get(DOC)).toBeUndefined();
    expect(cache.put(DOC, "old", started)).toBe(false);
    cache.put(DOC, "c", cache.begin());
    cache.sync(1);
    expect(cache.get(DOC)).toBe("c");
  });
});

Reading this cold, S1 adds a new production machine to solve a problem the council has already removed. Valkey exists to share state between several live machines; Council 1 decided live stays on ONE machine. The brief itself says process-local state "is CORRECT on one machine." So the plan's justification is a fleet that will not exist.

What a newcomer sees S1 buying: a single-node store with no HA, downtime on every deploy (per Fly's doc), a reported DNS resolver bug, a recent security release fixing an unauthenticated Lua use-after-free (so version pinning is mandatory), four hand-written Lua scripts, three backends needing parity tests, and one worker owning all of it. Its own failure modes are new outages. Fail-closed on a store that is down at every deploy means paid answers vanish on every release. For a demo site, that is a worse product than today.

The load claim is also unmeasured: the brief says "Neo4j cannot carry it" is an assumption, and about 12 reads/s plus 9 writes/s of small service-state operations is not obviously beyond it. Spike S7 would settle it cheaply. Building the cure before measuring the disease is the obvious red flag.

Jargon test: lease TTL, reconcile, sweep, reserve. If the owner cannot explain why a one-process spend cap needs distributed atomics, it has not earned its place. Also the "S2" label means both a spike and an option here; fix that before anyone signs off.

Policy: accept the $10/day cap, three-level kill switch, and the HMAC pepper (a privacy gain independent of Valkey). Defer the shared Langfuse salt until a second machine exists. With Neo4j as the only store, fail closed costs nothing extra, because a Neo4j outage already stops retrieval.

Evidence that would change my mind: S7 showing Neo4j failing at 5-20 asks/s, or S2 showing one performance machine cannot pass the gate.

Recommendation: S2, a StateBackend protocol with in-process and neo4j backends now, async rewrite first, S7 measured on staging, and Valkey only if a measurement or the owner's explicit approval demands it.

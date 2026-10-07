# First Principles Thinker

You're asking the wrong question. The issue isn't how to charge an abandoned ask. It's what enforces the invariant the owner approved, which is that no single address can consume more than 1/8 of the shared day. Today that invariant is enforced in asks (20 × 8 > 150), but the shared resource that gets exhausted is dollars. Admission estimates are hundreds of times real cost ($0.58-$0.83 against $0.002). So one address can turn 20 asks into nearly the whole budget. That is a unit mismatch, and only B fixes it by construction.

**Build C, with these parameters:**

1. **A (already being built).** Charge an abandoned ask the reported cost of completed calls plus the bound of started calls. This is also a prerequisite for B. Without A, a real buyer who closes the tab on two hybrid asks books $1.16 and is nearly locked out by B. One condition: settle only after the ask's task has fully stopped, or a call started after settlement goes uncounted. I assume the finalize is shielded, and a test should confirm it.

2. **B at $1.25 per address per UTC day** ($10/8, which keeps "at least 8 addresses to pause"). The admission check is: this address's settled spend, plus its own in-flight reservations (at most 2 asks in flight globally), plus this ask's estimate, must stay at or below $1.25. Check only at admission, never mid-ask. A 20-ask buyer demo settles at about $0.04 if typical, or about $0.90 if every ask escalates.

**Why A alone fails.** An attacker with worst-case prompts who disconnects right after the Sonnet call starts is charged about $0.3-0.6 per ask. Twenty asks reach $6-12, so one address can still pause the site. Only B caps that.

**Why not D or E.** D contradicts the approved reasoning during a buyer preview. Charging an address but not the global cap (E) under-counts real spend and breaks the $10 ceiling.

**Tests first:**
- 17 abandoned hybrid asks from one address are cut off by B at $1.25, and the global day is never paused.
- 20 typical asks from one address are all admitted.
- An abandoned ask with zero started calls is charged $0.
- The global ceiling on real spend is unchanged.

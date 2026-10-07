# Council 4 verdict

## Where the Council Agrees

All five advisors say build C. Option A on its own fails. An attacker who disconnects after escalation starts is still charged about $0.29 per worst-case ask (assumption: one Sonnet attempt has started). If both Sonnet attempts start before the disconnect, the charge comes close to $0.57, so one address can still pause the day with 17 abandons. Option B on its own fails the buyer. With full-estimate charging, one closed tab on an agent ask ($0.83) locks that office out of hybrid and agent asks.

The share is $1.25 ($10 divided by 8). That matches the count cap, where 150 asks at 20 per address also needs 8 addresses. The full in-flight reservation and the global $10 ceiling stay as they are. The council rejects D, and it rejects charging the address but not the global cap. A visitor refused by B gets its own message, not the "paused" wording.

## Where the Council Clashes

**Admission formula.** There are three candidates:
- **Settled spend only** (the Contrarian; all five reviewers backed it). An address at $1.24 can start two agent asks and end near $2.90. That drops the floor to 4 addresses, not the 6 the Contrarian claimed.
- **Settled + this ask's estimate** (Expansionist, Outsider, Executor). Two concurrent asks from one address can still both pass, so the address can overshoot.
- **Settled + this address's in-flight reservations + this ask's estimate** (First Principles). This holds every address at or below $1.25, so pausing needs at least 8 addresses. That holds only if no ask's final charge ever exceeds its estimate.

**Ruling: First Principles.** Its cost is that an agent ask is refused once that address's settled spend passes $0.42. The evidence, with its limit: BAKEOFF.md measured escalation at 0.00–0.15 after the verifier fix, on a curated benchmark rather than real visitor traffic. At a few cents per escalation, a buyer would need about 8–10 escalations in 20 asks (40–50%) before losing agent asks. Watch the escalation rate during the preview.

**Per-buyer overrides** (the Expansionist): rejected for now. They reopen the 8-address arithmetic, and a separate buyer lane is the owner's decision for the preview.

## Blind Spots the Council Caught

1. **Settle only after the ask's task has fully stopped.** A started call's bound already covers that call running to completion at the provider. The real hole is a call that *starts* after settlement.
2. **Fail closed.** Charge min(estimate, bound), and charge the full estimate if any record is missing.
3. **Check and reserve atomically.** Store per-address spend in the same backend record as the 20/day counter. (Assumption: that path is atomic today. Test it.)
4. **Check the 2.5 characters per token ratio.** Confirm it for each model on the most table-heavy prompts, because Luna and Sonnet tokenize differently. A bound that is too low silently under-counts against the money cap.
5. **Abandons with no paid call still count.** They use up the 20/day, the paid window and the 150.
6. **Per-address limits only slow an attacker down.** Rotating IPv6 /64 addresses is cheap (assumption: Fly serves IPv6). The $10 cap is what actually protects the money.

Rejected:
- **Hiding the "escalated" event:** it no longer matters once each address is capped.
- **Letting abandoned asks finish and settling at the reported cost:** it pays for answers nobody reads and holds one of the two in-flight slots.
- **Reserving per call instead of per ask:** it weakens the up-front ceiling.

## The Recommendation

Build C as one test-first change.

- **Abandoned charge:** min(estimate, reported cost of completed calls + bounds of calls started but not completed).
  - $0 if no paid call started.
  - The full estimate if any record is missing.
  - Computed only after the shielded task has stopped.
- **Per-address share:** $1.25 per UTC day.
  - Admit only if settled + this address's in-flight reservations + this ask's estimate ≤ $1.25.
  - Check and reserve atomically, at admission only.
- **Refusal messages:**
  - If settled + estimate alone exceeds the share, say "this network's live allowance for today is used; cached examples still work".
  - If only the in-flight reservations push it over, use the existing busy/retry message.
  - Known cost: two people in one office asking at once (hybrid + agent ≈ $1.41) get "busy" even when a global slot is free.
- **Tests:**
  - (a) One address abandoning worst-case escalated asks is refused at or below $1.25 recorded, and the day stays open. Pausing takes 8 such addresses.
  - (b) 20 asks from one address, including 3 escalations and 2 agent asks, are all admitted.
  - (c) An abandon with no paid call is charged $0 and still counts against the limits.
  - (d) The charge is at least the provider-reported cost and at most the estimate.
  - (e) Two concurrent asks from one address cannot both pass on the same remaining share.
  - (f) A call that starts after a disconnect is counted.
  - (g) Reported cost ≤ estimate for every ask type at the worst-case prompt. Without (g), the "at least 8 addresses" guarantee is weaker than stated.
- **Operations:**
  - Log a warning at 50% of the day's spend or count, and again at pause.
  - Write the pause runbook (kill switch, then wait for UTC midnight) before the buyer preview.
  - No new services.

## The One Thing to Do First

Write failing test (a): 17 abandoned hybrid asks from one address must be refused at or below $1.25 recorded, with the day still open.

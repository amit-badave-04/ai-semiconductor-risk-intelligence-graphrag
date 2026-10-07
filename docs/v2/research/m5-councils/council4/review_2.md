# Peer review 2

**1. Strongest: Response B.** It is the only one that does all of these:
- It caps the abandoned charge at min(estimate, bound) and falls back to the full estimate if the bound can't be computed.
- It still counts zero-call abandons against the 20/day, the paid window and the 150 count.
- It checks B on settled spend only, so the $0.83 agent estimate can't refuse a buyer mid-demo.
- It flags that the 2.5 chars/token bound is unvalidated.
- It admits that the real guarantee is about 6 addresses, not 8.
- It adds an owner alert and a runbook for the unrecoverable pause.

**2. Biggest blind spot: Response D.** Its admission check adds the address's in-flight reservations to the new ask's estimate. Two concurrent agent asks ($0.83 × 2 = $1.66) are then refused with $0 settled, which breaks the buyer-demo constraint. Its "you're asking the wrong question" framing also dismisses A as just a prerequisite, when A is the half that stops honest buyers being locked out. Response E has a lesser flaw: it calls the bound "an upper bound by construction" without checking the chars/token assumption.

**3. What all five missed:**
- **Disconnect does not stop the provider.** If the charge drops to the bound at disconnect but the upstream call keeps running, the provider can bill more than the recorded charge, which under-counts the $10 cap. Either cancel upstream, or keep the reservation until the task fully ends and settle at reported usage. Response D only touches this.
- **The "escalated" event is a free timing signal.** Don't emit it mid-stream, or buffer it until the answer completes. Then the attacker can't time a disconnect right after the Sonnet call starts.
- **Nobody weighed "finish and settle."** For disconnected asks, let the call complete and settle at the provider-reported cost, so no estimate is needed.

# Peer review 1

1. **Strongest: B.** It is the only response that shows A and B depend on each other.
   - B alone locks out a buyer who abandons two agent asks.
   - A alone still lets two addresses pause the day.
   - It also gets the details right. Zero-call abandons still consume the 20/day and the paid window. The charge is min(estimate, reported + started bounds), with a full-estimate fallback if the bound can't be computed.
   - Its admission check uses settled spend only, so a $0.83 agent estimate doesn't refuse a buyer after about $0.42 settled.
   - It admits the cost: the guarantee drops to about 6 addresses.
   - It asks for the 2.5 chars/token assumption to be validated, and it wants a pause alert and a runbook.

2. **Biggest blind spot: E.**
   - It calls the bound "an upper bound by construction", but 2.5 chars/token is untested on table-heavy text.
   - It uses the estimate-inclusive check that B shows refuses late agent asks mid-demo, and it dismisses that as "only the last agent asks". A, C and D share this flaw.
   - Its per-buyer share override raises the cap for named addresses. That erodes the 8-address invariant the cap exists to protect.

3. **All five missed:**
   - Reserve per call at call start, from the actual prompt, using the mechanism already being built. This replaces the $0.58 worst-case reservation up front. Reservations and B's admission check then become tight, and the buyer conflict mostly disappears.
   - Remove the attack trigger. Don't emit the "escalated" event before completion, or cap Sonnet escalations per address.
   - Make check-and-reserve atomic. Two concurrent asks from one address can both pass a per-address check. Also add a per-address in-flight limit of 1, since one address can currently hold both global slots.

# Peer review 3

1. **Strongest: B.**
   - It is the only response that makes the abandoned charge fail closed: min(estimate, bound), falling back to the full estimate if the bound can't be computed.
   - It checks the share on settled spend only, which protects the 20-ask buyer demo.
   - It states the honest cost of that choice, which is overshoot of about two in-flight asks.
   - It asks for the 2.5 chars/token assumption to be validated, since an under-bound silently under-counts the money cap.
   - It keeps zero-call abandons counting against the 20/day and window limits.
   - It adds an owner alert and a runbook, because a paused day can't be reset.
   - Its "about 6 addresses" floor is optimistic. With agent asks, in-flight overshoot could be up to 2 × $0.83.

2. **Biggest blind spot: D.**
   - Its admission check is settled + in-flight + this ask's estimate ≤ $1.25.
   - With a $0.83 agent estimate, that refuses a buyer once settled spend passes about $0.42. Two concurrent agent asks ($1.66) are refused outright.
   - D still says the 20-ask demo is safe. C and E make the same error with a milder check. Only A and B notice the problem.
   - E also proposes per-address overrides for buyers. That works against the "production refuses raised caps" rule and weakens the 8-address guarantee.

3. **Missed by all five.**
   - Nobody considered not cancelling on disconnect. The server could drain the provider call in the shielded task and settle at the reported cost.
   - That removes abandoned-ask estimation entirely. The exposure is then the real spend of at most 2 in-flight asks, and the in-flight limit already caps it.

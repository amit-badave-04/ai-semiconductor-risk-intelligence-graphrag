# Peer review 5

1. **Strongest: Response B.** It is the only one that treats the bound as fallible. The abandoned charge is min(estimate, completed cost + started-call bounds), with a fallback to the full estimate if the bound can't be computed. It makes zero-call abandons still consume the daily count, window and quota. It says to validate the 2.5 chars/token figure, because an under-bound silently under-counts the money cap. It states honestly that the overshoot guarantee drops to about 6 addresses, and it asks for an owner alert and a pause runbook. Its settled-only check is debatable, but it is argued.

2. **Biggest blind spot: Response E.** It calls the bound "an upper bound by construction", but 2.5 chars/token is an unverified assumption. It also assumes a low escalation rate and only asks that someone check it. It then adds per-buyer allowances such as $3, which reopen the 8-address premise. It never mentions shared-address collateral damage or concurrent in-flight asks from the same address.

3. **Missed by all five:**
   - Let an abandoned ask finish in a shielded task and drop only the stream. The provider-reported cost is then exact, and the guesswork disappears. The attacker would spend real, tiny, bounded dollars.
   - Degrade instead of refusing. Once an address passes a lower threshold, disable Sonnet escalation for it. That caps its per-ask cost near $0.014 and removes the main lever. Delaying or hiding the "escalated" event also removes the attacker's cue.
   - Make the per-address check and reserve atomic. Make the ledger survive restarts and deploys, and reset it at UTC rollover.

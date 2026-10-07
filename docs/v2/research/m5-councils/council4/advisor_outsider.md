# Outsider

**Build C (A and B together), with the per-address share at $1.25 (= $10/8), and treat that number as the real design parameter.**

The core problem is that the ledger says $9.86 was spent when about $0 was. Options D and E leave the ledger wrong or hide the problem. Fixing the ledger is option A, because charging "completed calls plus the bound of started calls" is honest accounting. A does not remove the denial risk. Reading the facts plainly, an attacker who disconnects right after the Sonnet call starts is charged about $0.30 per ask. Twenty such asks cost one address about $6, so two addresses could pause the site. That breaks the "8 addresses" promise, and the mechanism document doesn't mention it. A alone is therefore not enough.

B restores the promise in an easy-to-state form. If no address can be charged more than $1.25, then at least 8 addresses are needed to reach $10. The 150-ask count cap divided by 20 asks per address is also about 8, so both pause routes need the same number of addresses. That consistency is the strongest argument for B.

Your own numbers show a buyer is safe. Twenty typical asks settle at about $0.04. Even if every one escalated at roughly $0.05, that is about $1.00, still under $1.25.

Three things to check, which I'm flagging because insiders may not see them:
1. **Agent asks.** Their estimate is $0.83, so a buyer with more than $0.42 already settled gets refused on an agent question. Test this case explicitly.
2. **Shared addresses (assumption).** An office network or mobile carrier may put a buyer and a stranger behind one address, so the stranger's abuse blocks the buyer.
3. **The refusal message.** A visitor refused by B should not see the global "paused" wording. Use a distinct message saying their address has reached its share.

Ship C, and test the buyer-at-20-asks scenario and the 8-address scenario as acceptance tests.

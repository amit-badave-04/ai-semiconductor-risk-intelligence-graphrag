# Contrarian

**Build C (A+B). Neither half survives an adversary alone.**

**A alone fails.** The attacker picks 4-company questions (largest prompts), waits for the "escalated" event, and disconnects. The charge is about $0.014 + $0.28 = $0.29 per ask, so 20 asks cost $5.8 per address. Two addresses pause the day, not eight. I assume escalation is inducible, because citation-check failures can be provoked.

**B alone punishes honest buyers.** Under full-estimate charging, a buyer who closes the tab on two agent asks ($1.66) is locked out of live for the day. B only makes sense once abandonment is charged realistically.

**Parameters**
- **Abandoned charge** = min(full estimate, completed reported cost + started-call bounds). Assert in a test that it never exceeds the estimate. If the bound cannot be computed (missing record, tokenizer error), charge the full estimate.
- **Zero-call abandons** charge $0, but they still consume the 20/day, the paid window and the 150 count. Otherwise they are free probing.
- **B = $1.25, checked on settled spend only.** Do not add this ask's estimate. The $0.83 agent estimate would refuse a buyer once settled spend passes about $0.42, roughly 8 escalated answers in, and that breaks the 20-ask demo.
- **Cost of that choice:** overshoot of up to 2 in-flight asks × about $0.29, so the honest floor is about 6 addresses, not 8. Tell the owner the guarantee changed.
- **Validate 2.5 chars/token** against SEC-table-heavy and numeric text before trusting it. An under-bound silently under-counts the money cap.

**What nobody is saying.** The "8 addresses" premise was already weak. A free IPv6 /48 from a tunnel broker gives 65,536 distinct /64 keys, and Turnstile solvers are cheap. Per-address limits are a speed bump, not a fence. I am assuming Fly serves IPv6, which it does by default.

The real hole is that there is no recovery path. Add an owner alert the moment the day pauses. Before the buyer preview, write down the runbook (the kill switch plus waiting for UTC midnight), because the pause cannot be reset. Do not build forgiveness or exemption features. They weaken the one invariant that works.

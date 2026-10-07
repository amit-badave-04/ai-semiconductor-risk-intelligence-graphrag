# Expansionist

**Build C (A plus B), and treat B's per-address ledger as the real prize.**

**Why A alone fails.** At the worst-case prompt, an attacker who disconnects right after Sonnet starts is charged about $0.29 per ask. 20 asks come to about $5.9, so two addresses pause the day. That breaks the approved rule that pausing needs at least 8 addresses. A shrinks the damage from about $9.86 to about $5.9 per address, but it doesn't restore that rule.

**Why B restores it.** A cap of $10/8 = **$1.25 per address per day**, settled spend only, is the invariant the owner already approved. With A feeding it honest numbers, an abusive address is blocked after about 4 worst-case abandoned asks. The count cap of 150 against 20 per address independently needs at least 8 addresses.

**Parameters:**
- **Share:** $1.25 per address, held in config as a per-address map with a default. This is a per-address share, not a raised global cap, so the "production refuses raised caps" rule is untouched.
- **Admission check:** settled spend + this ask's estimate ≤ share.
- **Buyer demo:** 20 asks at about $0.002 settle at about $0.04, so a normal demo is never blocked. Even if every ask escalated at about $0.05, spend reaches about $1.00, and only the last agent asks would be refused. I assume the escalation rate is low, so please check it.
- **Global cap:** unchanged. The in-flight reservation still holds the full estimate, and the bound is an upper bound by construction, so the $10 ceiling holds.

**Upside others will miss:**
1. A per-address settled-spend ledger turns defence into product. A named-buyer preview becomes one config line, for example $3 for a buyer's hashed address inside the same $10 cap. That is a sales feature, not just abuse control.
2. It records real cost per prospect, so you can price the commercial tier from measured data.
3. Honest accounting shows typical traffic uses about 3% of the dollar cap (150 × $0.002 = $0.30). The binding constraints are count and abuse, not dollars.

Defer E (the forgive action). With A, abandoned charges track real spend closely, so there is little to forgive.

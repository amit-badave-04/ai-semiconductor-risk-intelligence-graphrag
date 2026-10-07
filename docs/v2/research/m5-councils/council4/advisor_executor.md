# Executor

**Build C (A + B) now, in one change. Neither half works alone, and together they are small.**

**Why not A alone.** An attacker who sees the "escalated" event and disconnects is charged about $0.29 per ask at a worst-case 4-company prompt. Twenty asks is about $5.9, so two addresses pause the day. The "at least 8 addresses" assumption still fails. Escalation is easy to trigger because questions that fail the citation check escalate.

**Why not B alone.** B with full-estimate charging breaks the buyer demo. A buyer who refreshes twice mid-answer has $1.16 settled. The third hybrid ask, at $0.58, then fails the admission check ($1.74 > $1.25), and the whole office is locked out at ask 3. A fixes this, because an abandon before any paid call starts is charged $0.

**Why not D or E.** D leaves the outage in place for the buyer preview. The admin "forgive" action in E needs a person watching, and the admin can't reset an exhausted day anyway. Lowering estimates, or not charging abandoned asks to the global cap, risks under-counting real spend, which is forbidden.

**Parameters:**
- **Abandoned-ask charge:** reported cost of completed calls plus the bound of calls started but not completed. If no call started, the charge is 0.
- **In-flight reservation:** still the full estimate until the ask settles, so the $10 ceiling is unchanged.
- **Per-address share:** $1.25, stored in the same per-address state as the 20/day count. Admit only if that address's settled spend plus this ask's estimate is at most $1.25.
- **Overshoot:** the check ignores that address's in-flight reservations. With 2 asks in flight globally, the overshoot is at most about 2 charges, so I would not complicate the check.
- **Refusal message:** use a distinct "this network's daily live allowance is used" message, not "paused for everyone".

**Result.** An attacker is stopped after about 4 Sonnet-escalated abandons, costing at most about $1.25 of recorded spend. Pausing the day needs 8 or more addresses again. A buyer's 20 asks at about $0.002 to $0.05 each stay well under $1.25.

**Monday tests, written first:**
1. One address abandons after Sonnet starts at worst-case prompts. It is blocked by about the 5th ask, and the global charge is at most $1.25.
2. A buyer's 20 typical asks, including 3 escalations, are all admitted.
3. An abandon before any paid call is charged exactly 0.
4. Reported plus started-bound is never less than provider-reported spend for the same ask.

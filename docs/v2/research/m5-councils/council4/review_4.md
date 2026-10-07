# Peer review 4

**1. Strongest: Response B.** It is the only one that treats the guarantee as changed. Its points are:
- Cap the abandoned charge at the full estimate, and fall back to the full estimate if the bound can't be computed.
- Zero-call abandons still use up the 20/day, the paid window and the 150 count.
- Check B on settled spend only, which keeps agent asks ($0.83) admissible in a 20-ask demo.
- Admit the honest floor is about 6 addresses, not 8, because of in-flight overshoot.
- Validate the 2.5 chars/token assumption before trusting the bound.
- Add an owner alert and a runbook.

Response D's "unit mismatch" framing is also sharp.

**2. Biggest blind spot: Response E.**
- It says the demo is "never blocked", then admits the last agent asks would be refused under settled + estimate. That breaks the 20-ask buyer constraint, and E doesn't treat it as a defect.
- It calls the bound correct "by construction" and never tests the chars-per-token assumption.
- It leaves overshoot unquantified.

Response C shares the agent-estimate problem and dismisses overshoot.

**3. What all five missed:**
- None proposes protecting the buyer preview directly. A carved-out lane would do it, such as an invite token or allowlisted address with its own budget and count slice. A and B only raise attack cost. Any 8 addresses, or IPv6 /64 rotation (only Response B raises it), still pause the demo for the buyer.
- None proposes a cheap early-warning alert before the pause (Response B only alerts at the pause). A threshold alert at about 50% of the day's spend or count would help.
- None picks one admission formula with numbers. The three variants are settled only, settled + estimate, and settled + in-flight + estimate. They behave differently for agent asks.

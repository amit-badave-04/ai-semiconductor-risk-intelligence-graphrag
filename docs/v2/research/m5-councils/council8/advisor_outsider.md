# Outsider

**Read plainly: W1b has not passed.** One boot was valid and passed. Two attempts at boot 2 were invalid. Boot 3 never ran. I'd write that as the result today, before anyone interprets it.

**Option (c) is out.** In the invalid boots the upload never finished, so its last passages were never embedded. That is the very stretch the memory number is supposed to cover. Counting those boots would be waving a gate through.

**Option (a) is pointless.** Two identical failures with the same cause is a pattern, not bad luck. A third identical run just spends 20 minutes to learn the same thing.

**Option (b) is fine only if it is done openly.** The recorded text says "at the live concurrency caps". A cap is a ceiling, not a rate. Keeping 2 asks saturated is one reading, and it is a defensible one. A paced workload is another reading. You only discovered the difference after seeing results, so call it a new, named test, "W1b-paced". Write down its rule and the reason before running it. Then run three valid boots of it. Boot 1 can't just carry over, because it was run under a different workload. Keep the saturated runs as a separate, labelled stress result.

**Nobody measured the claim that q8 is worse.** "It would be worse with q8" is reasoning. Running the saturated workload on q8 costs cents, and it would show whether the timeout comes from the fp32 image or from the machine.

**The upload timeout is a finding the owner must hear.** The site has two asks in flight at most, and any visitor can fill that cap. Doing so makes every concurrent upload fail after 20 minutes. "A few asks per hour" describes normal traffic, not what a bored or hostile visitor can do. Tell the owner now, before any deploy.

**Do not change the product inside this gate (option d).** That is a separate decision with its own test.

Two things also need reporting: the memory margin is only 1.5-2%, and that margin was never measured on a run where the upload completed under saturating load.

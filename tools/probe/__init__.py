"""The fresh-machine throttle probe (M5a I5, docs/v2/M5_DECISIONS.md section 3).

``burn.py`` runs one full-core loop on a newly created machine and records iterations per second every second, with the steal time of
``/proc/stat``; ``report.py`` turns the raw lines into ``probe.json``: when the throughput fell for good, what share of it the machine
sustains, and how fast an idle stretch refills it. Stdlib only; run from the repository root (or inside the tools image):

    python -m tools.probe.burn --burn-s 3600 --idle-s 1800 --out /out/probe
    python -m tools.probe.report /out/probe --out /out/probe/probe.json
"""

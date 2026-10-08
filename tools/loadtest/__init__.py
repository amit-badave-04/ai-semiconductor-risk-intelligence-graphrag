"""The S2 load generator and its offline checks (M5a I5; docs/v2/M5_PLAN.md section 6, council 5 verdict).

Nothing here is imported by the service. The modules split in two groups:

* stdlib (+ ``requests``, ``psutil``) only, so they run inside the generator image and import without ``semigraph``,
  ``locust`` or ``gevent``: :mod:`model`, :mod:`sse`, :mod:`salt`, :mod:`records`, :mod:`client`, :mod:`cpu_watch`, and
  the loading half of :mod:`pool`;
* developer-side (import ``semigraph`` lazily, inside the function that needs it): the building half of :mod:`pool` and
  :mod:`salt_check`. They run on the desktop before staging, never in the image.

Only :mod:`locustfile` imports ``locust`` (which monkey-patches ``gevent`` into the process). Run from the repository
root with ``PYTHONPATH=.``; see ``tools/loadtest/README.md``.
"""

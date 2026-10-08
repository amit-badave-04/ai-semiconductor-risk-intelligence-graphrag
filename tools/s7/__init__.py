"""Spike S7: Neo4j service state under sustained load (M5a I5; docs/v2/M5_DECISIONS.md section 3).

``replay`` drives the service's own state, store and retrieval functions against a staging Neo4j and writes raw files;
``report`` turns them into ``s7.json`` with a verdict per level. ``limits`` holds the pre-registered limits and ``mix`` the
operation mixes and the W3 schedule. Nothing here is imported by the service.
"""

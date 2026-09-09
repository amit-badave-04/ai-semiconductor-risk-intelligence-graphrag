"""semigraph.serve — the web service (FastAPI) over the semigraph SDK.

    uvicorn semigraph.serve.main:app --port 8080

Endpoints: ``/`` (single-page UI), ``POST /api/ask`` (SSE stream),
``GET /api/examples``, ``GET /api/evidence/{chunk_id}``, ``GET /api/stats``,
``GET /healthz``, ``GET|POST /api/admin/policy`` (kill switch, admin token).
Cost controls live in :mod:`semigraph.serve.guard`; the answer cache and
query ledger in :mod:`semigraph.serve.store` — both persisted in Neo4j so a
restart (the machine auto-stops when idle) never resets a daily ceiling.
"""

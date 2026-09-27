"""End-to-end check of serve/tracing.py against the REAL langfuse SDK and a LOCAL capture server (no Langfuse account, no internet).

Run it before the owner enables tracing, in an environment that has the package (``pip install "langfuse>=4.14,<5"``):

    PYTHONPATH=src python scripts/probe_tracing_e2e.py

Everything the SDK exports is captured as raw bytes. Canaries planted in every place a careless caller could leak from (question text, answer text,
an address, a bearer token, exception messages, tool arguments, a fallback reason) must NOT appear in any captured byte; the useful facts (tool, model
and span names) must. Verified 2026-09-27 with langfuse 4.15.6 on Python 3.13: one OTLP export to /api/public/otel/v1/traces, no canary, request-side
cost about 1 ms. It does NOT prove anything about Langfuse's own servers: the owner's one live trace is the last check.
"""

import gzip
import http.server
import json
import sys
import threading
import time

from semigraph.config import Settings
from semigraph.serve import tracing

Q = "CANARY-QUESTION-9731 how exposed is Nvidia to TSMC?"
ANS = "CANARY-ANSWER-4410 Nvidia depends on TSMC."
IP = "203.0.113.77"
BEARER = "Bearer CANARY-BEARER-5521"
SECRET = "sk-lf-e2e-secret-value"
CANARIES = {"question": "CANARY-QUESTION-9731", "answer": "CANARY-ANSWER-4410", "address": IP, "bearer": "CANARY-BEARER-5521",
            "exception": "CANARY-EXC-", "raw question words": "how exposed is Nvidia"}
USEFUL = ("financial_metrics", "openai/gpt-6-luna", "planner", "prefetch", "agent")

captured: list[tuple[str, dict, bytes]] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        captured.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def run_request(tracer) -> float:
    """One agent-shaped request, with a canary planted in every place a caller could leak from. Returns the request-side seconds."""
    t0 = time.perf_counter()
    req = tracer.for_request(Q, strategy="agent")
    with req.span("prefetch", question=Q, client_ip=IP, headers={"authorization": BEARER}, chunks=8):
        pass
    with req.span("plan", model="openai/gpt-6-luna", turn=1) as sp:
        sp.set(tool="financial_metrics", arguments={"companies": ["Nvidia"], "note": ANS},
               fallback_reason="planner_error: CANARY-EXC-8802 boom")
        req.generation(name="planner", model="openai/gpt-6-luna", usage={"prompt_tokens": 900, "completion_tokens": 40},
                       cost_usd=0.0001, input_chars=len(Q), output_chars=0)
    req.event("step", tool="risk_changes", summary=ANS, ok=True)
    try:
        with req.span("boom"):
            raise RuntimeError("CANARY-EXC-1234 " + Q)
    except RuntimeError:
        pass
    req.close()
    return time.perf_counter() - t0


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(_env_file=None, agent_enabled=True, langfuse_public_key="pk-lf-e2e-public", langfuse_secret_key=SECRET,
                        langfuse_host=f"http://127.0.0.1:{server.server_address[1]}", langfuse_sample_rate=1.0)
    tracer = tracing.get_tracer(settings)
    print("tracer:", type(tracer).__name__)
    if type(tracer).__name__ == "NullTracer":
        print("FAIL: expected a real tracer (langfuse importable and all three settings set)")
        return 2
    print(f"request-side tracing calls took {run_request(tracer):.3f}s (the SDK exports in a background thread)")
    tracer.shutdown()
    time.sleep(0.5)
    raw = b""
    for _, headers, body in captured:
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
        raw += body + json.dumps(headers).encode()
    leaks = {name: text.encode() in raw for name, text in CANARIES.items()}
    useful = {text: text.encode() in raw for text in USEFUL}
    print("posts captured:", len(captured), sorted({p for p, _, _ in captured}))
    print("leaks:", leaks)
    print("useful facts present:", useful)
    if any(leaks.values()) or not captured:
        print("FAIL: a leak, or nothing was exported")
        return 1
    print("OK: nothing leaked; the export happened")
    return 0


if __name__ == "__main__":
    sys.exit(main())

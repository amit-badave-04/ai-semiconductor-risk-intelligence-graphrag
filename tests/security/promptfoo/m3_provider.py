"""promptfoo python provider for the M3 writer-rule eval: the rendered answer.txt to the LOCAL model, raw output returned.

The shipped writer is the prompt rules alone (no output rewriting), so the eval scores the model's raw answer. The rendered answer.txt
goes to the local llama-server (gpt-oss-20b, http://127.0.0.1:8080/v1 only; any other host is refused). ``vars`` carries ``question``
and ``context`` next to ``prompt`` (for the assertions), and optionally ``effort`` (gpt-oss reasoning effort, default medium).

Run (from tests/security/promptfoo, repo root venv, PowerShell or bash):
    PROMPTFOO_PYTHON=<repo>/.venv/Scripts/python.exe PYTHONPATH=<repo>/src promptfoo eval -c m3_writer_rules.yaml --no-cache
The repeat count (3) is in the config (``evaluateOptions.repeat``).
"""

import json
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8080/v1/chat/completions"     # the only host this provider talks to
MAX_TOKENS = 4000
RETRIES = 2


def _complete(prompt: str, effort: str = "medium") -> str:
    body = {"model": "gpt-oss-20b", "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": MAX_TOKENS,
            "reasoning_format": "deepseek", "chat_template_kwargs": {"reasoning_effort": effort}}
    request = urllib.request.Request(URL, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:      # nosec B310: fixed http://127.0.0.1 URL
                return json.load(response)["choices"][0]["message"].get("content") or ""
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"local model failed: {type(last).__name__}: {last}")


def call_api(prompt, options, context):
    variables = (context or {}).get("vars", {})
    try:
        raw = _complete(prompt, variables.get("effort") or "medium")
    except RuntimeError as e:
        return {"error": str(e)}
    return {"output": raw}

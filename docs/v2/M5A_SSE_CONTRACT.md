# The /api/ask and job event-stream contract (M5a bytes, read by the M5b client)

Written from the byte fixtures in `tests/data/sse/` (recorded by `tests/test_sse_fixtures.py` over fakes at $0, pings off) and
checked against the client by `web/src/lib/__tests__/contract.test.ts`. The legacy page (`index.html`) and the React client
(`web/src/lib/events.ts`, `sse.ts`, `jobStream.ts`) read the same bytes. Spec: `docs/specs/m5b-frontend.md` R15 to R20.

In this document the JSON escape `\u2014` stands for the em dash, as the server's JSON writes it on the wire.

## 1. Request

`POST /api/ask`, `Content-Type: application/json`, body
`{"question": str, "strategy": "hybrid" | "vector" | "agent", "workspace_id"?: str, "as_of"?: str, "turnstile_token"?: str}`.
A workspace ask also sends the `X-Workspace-Token` header. The client sends one POST per ask and NEVER retries it: a POST is
paid for, and a cut stream is reported, not re-sent.

## 2. Wire grammar

Server-sent events with `sep="\n"`: each frame is `event: <name>\n` then `data: <one line of JSON>\n` then a blank line.
The JSON is `json.dumps` with its default escaping, so every fixture is ASCII. Comment frames (`: ping - <time>`) and `event: ping`
frames may appear between events and carry nothing (`ping_interleaved.sse`, hand-written, proves they are ignored). The server
pings every 15 seconds; the recorder switches them off. Unknown event names, unknown fields, data that is not JSON and payloads
of the wrong shape are ignored by the client.

Response headers (pinned by `tests/test_serve_api.py`): `Content-Type: text/event-stream; charset=utf-8`,
`Cache-Control: no-store`, `X-Accel-Buffering: no`.

## 3. Events of /api/ask

Every `data` object repeats its name in an `event` field. Fixtures: `live_hybrid.sse`, `agent_steps.sse`, `escalated.sse`,
`workspace_done.sse`, `cached_done.sse`, `error_before_delta.sse`, `error_after_delta.sse`.

| Event | Fields the client reads | When |
|---|---|---|
| `retrieval` | `anchors` (name to number), `counts` (name to number); optional `anchor_defaulted` (boolean: no company was detected and the server defaulted to Nvidia) | once, after retrieval |
| `step` | `n`, `tool`, `summary`, `ok` | agent only, before `retrieval` |
| `delta` | `text` | zero or more, in order |
| `escalated` | `reasons` (strings), `from`, `to` | when the draft was rejected and the strong model answers |
| `done` | `answer`, `citations`, `hallucinated`; live: `finish_reason`, `usage`, `cost_usd`, `checks`, `agent`, `workspace`; optional routing fields of a live answer: `answered_by` (string, the model), `routed` (string, `cheap` or `strong`), `escalated` (boolean), `escalation_reasons` (strings) | terminal |
| `error` | `detail` | terminal |

The cached `done` is one frame with `cached: true`, `source`, `answer`, `citations` and `hallucinated` (no usage, no checks;
`cached_done.sse`). A live `done` has no `cached: true`. `done.agent.elapsed_s` is a number. A stream ends with exactly one
terminal event (`done` or `error`); a stream that ends without one was cut.

`error` as the client sees it carries only `detail`, and the server rewrites it: a failure of the model stream becomes
"The answer could not be completed \u2014 please try again." (`error_before_delta.sse`, `error_after_delta.sse`); a failure of
the twin itself becomes "The answer could not be completed (<ClassName>)."

### Client rules (`sse.ts`)

* `ended` is true only after a `done` or `error` event arrived and the page's handler took it in without throwing.
* `failed` is true when the handler threw, a frame went over the buffer limit, or the response had no body.
* Status when the stream did not end itself: a cut (reset, abort, a clean end without a terminal event) shows
  "connection lost before the answer finished: what is shown is incomplete and was not checked. Please ask again."
  and a page failure shows
  "the page could not show this answer completely: what is shown may be incomplete and was not checked. Please ask again."
* Nothing after the terminal event is delivered or read; the connection is released.
* An abort has no status text of its own: a user abort, a page unload and a lost connection all end the call with the cut
  text (`ended` and `failed` both false), but `AskOutcome.aborted` is true exactly when the CALLER's own signal aborted before
  a terminal event arrived (I3). A caller that stopped the call itself reads `aborted` (or its own `signal.aborted`) and words
  it as a stop, never as a lost connection; it never infers an abort from the status text. An answer that had already ended
  is never `aborted`, and a frame over the buffer limit that arrives after the terminal event (in the same read) does not make
  a finished answer `failed`: `failed` means an overflow, a throw or a missing body of an answer that had not ended.
* The page keys every guard on the SSE event NAME (`event: done`, `event: error`), which is what `decodeEvent` receives, and
  never on an `event` field inside `data`: the byte fixtures carry one, the server may stop sending it, and a forged `data`
  must not change which handler runs.
* The time bound of a refused request's body read (`detailOf`) is the caller's signal: `detailOf` has no timer of its own and
  the body belongs to the POST's `fetch`, so aborting the signal passed in `AskOptions.signal` ends the read (the status text
  is then used); the body is capped at 64 KiB regardless.

### Buffer limit

eventsource-parser 4.1.3 has no default limit, so `sse.ts` sets one: `MAX_BUFFER_SIZE = 262144` characters (256 KiB), exported
from `sse.ts`. The parser counts the characters of an unfinished line plus the data of an unfinished event, but a frame that arrives
whole in one read is dispatched before it is counted, so both readers (`sse.ts`, `jobStream.ts`) also check the `data` of every
dispatched frame against the same limit: the limit holds however the bytes are chunked. The largest frame the server can send is a `done` at 2,400 tokens
(`LLM_ANSWER_MAX_TOKENS`, `fly.toml`) with its citation list and checks: the hand-written `done_max_size.sse` is that frame,
17763 bytes (the answer is sized at 6 characters per token, a ceiling, not a recording). The limit is about fifteen times that.
An overflow ends the call as `failed` (never a silent drop), the parser is not fed again and the reader is released.

## 4. Refusals before any stream

A non-OK response is JSON `{"detail": <text>}`; the client shows `detail` unchanged and adds no `Retry-After` countdown. When
the body is not JSON (an upstream HTML page) the status text is shown, never the body. Fixture: `refusals.json`.

| Status | Case | detail |
|---|---|---|
| 400 | unknown strategy | strategy must be one of ('hybrid', 'vector', 'agent') |
| 400 | `as_of` without a workspace | as_of is available only with a workspace |
| 403 | bot check failed | Bot check failed \u2014 reload the page and try again. |
| 404 | unknown workspace or wrong token | workspace not found |
| 404 | unknown job | job not found |
| 429 | per-address window | Too many questions from your address \u2014 please wait a few minutes. |
| 429 | in-flight cap full | The service is busy answering other questions \u2014 try again in a moment. |
| 503 | paused | Live questions are paused right now \u2014 the example questions still work. |
| 503 | `retrieval_only` | Live questions are limited to cached answers right now \u2014 the example questions still work. |
| 503 | draining | The service is restarting \u2014 please try again in a minute. |

The draining 503 carries `Retry-After: 30`; no other recorded refusal has a `Retry-After`.

This table is the RECORDED subset. `/api/ask` has further refusals that `refusals.json` does not record, and their copy is
defined by the server constants of the same names (not repeated here): `MSG_BUDGET`, `MSG_IP_BUDGET` and `MSG_IP_SPEND` (429),
`MSG_READ_RATE` (429: the cache-read budget and the workspace token check), `MSG_STATE_UNAVAILABLE` and `MSG_UPLOADS_OFF`
(503). The client shows their `detail` unchanged like any other. The input checks of `guard.py` answer 400 with their own
`detail` text, shown the same way; FastAPI's own 422 carries a list as `detail`, so the client shows the status text instead
(`detailOf`, `web/src/lib/refusal.ts`).

## 5. Job stream

`GET /api/workspace/{ws}/jobs/{job_id}` with the `X-Workspace-Token` header (the token is never in the URL). Frames are
`event: job` and `data` is the job object (it has no `event` field): `job_id`, `state`, `document_id`, `version` (null before
validation), and per state `progress` (`done`, `total`, `eta_s`), `chunks`, `units`, `items_compared` (a boolean), `not_compared_reason`,
`suspicious`, or `error` (`code`, `message`). States, in order: `received`, `validating`, `parsing`, `chunking`, `embedding`
(repeated with progress), `comparing`, `indexing`, then `ready` or `failed` (terminal). Fixtures: `job_ready.sse`,
`job_failed.sse`; they are route-recorded, with the job dicts constructed exactly as `uploads/jobs.py` emits them.

Client rules (`jobStream.ts`): at most one open GET per watch, because the server allows three live watchers per job and answers
a fourth with 429. A retry starts only after the previous response was released. 429, 5xx, a rejected fetch and an early end
are retried, with growing waits, up to 5 attempts in all; a 404 means the job is gone; any other status is not retried; an
overflow of the buffer is not retried. A reconnect replays the job's log from its start, so an earlier state can arrive again.

## Frozen

These bytes are frozen. A later change to the `/api/ask` bytes needs a spec amendment, a re-record of the fixtures
(`RECORD_SSE=1 uv run pytest tests/test_sse_fixtures.py`) and the owner's approval, because the legacy page reads the same bytes.

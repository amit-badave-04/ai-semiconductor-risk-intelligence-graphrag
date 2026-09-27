"""The thin retrieval-planning agent (docs/v2/M3_AGENT_PLAN.md): OPT-IN, ``strategy=agent``, off by default.

The agent is a retrieval PLANNER, not an answer writer: a cheap model with read-only tools decides which extra lookups to run
over the prefetched hybrid retrieval, the results merge into the retrieval dict, and the ONE shared writer / verifier /
escalation (``retrieval.answerer.stream_answer_for_context``) answers. The planner never writes prose and never sees filing
prose.

Modules: ``trace`` (the tracing seam), ``state`` (state and limits), ``merge`` (pure merges into the retrieval dict),
``sanitize`` (what the planner may see), ``tools`` (the seven read-only tools), ``planner`` (the planner model call),
``graph`` (the LangGraph loop), ``stream`` (the entry points).

Nothing is imported here on purpose: ``semigraph.agent.tools`` and ``semigraph.agent.trace`` never pull in langgraph; only
``graph`` (and so ``stream``) does. No module outside this package may import it at module level: the serving image must
not need langgraph while the flag is off.
"""

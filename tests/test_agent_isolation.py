"""The serving image must not need the agent while the flag is off (docs/v2/M3_AGENT_PLAN.md section 0 and 6).

langgraph is imported only by ``semigraph/agent/graph.py`` (so only through ``semigraph.agent.stream`` and its async
twin ``semigraph.agent.stream_async``); no module outside the package imports the agent at MODULE level
(``serve.stream_runtime.select_twin``, which ``routes._stream_fn`` calls, and ``eval.agent_eval`` import it inside a
function); importing the route module, the async answer runtime and the other two async writers pulls in neither
langgraph nor the agent. The import checks run in a SUBPROCESS: other tests in this session have already put langgraph
into ``sys.modules``.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "semigraph"
ENV = {**os.environ, "PYTHONPATH": str(SRC.parent)}      # semigraph is importable through pytest's ``pythonpath = ["src"]``, not installed
CHECK = "import sys; {imports}; bad = [m for m in ({names}) if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"


def imports_leave_out(imports: str, *names: str) -> subprocess.CompletedProcess:
    code = CHECK.format(imports=imports, names=", ".join(repr(n) for n in names))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=SRC.parents[1], env=ENV)


def test_importing_the_serving_modules_pulls_in_neither_langgraph_nor_the_agent():
    done = imports_leave_out("import semigraph.serve.routes, semigraph.serve.main, semigraph.serve.guard, "
                             "semigraph.retrieval.answerer, semigraph.serve.stream_runtime, semigraph.serve.limiters, "
                             "semigraph.serve.embed, semigraph.retrieval.answerer_async, "
                             "semigraph.retrieval.workspace_async",
                             "langgraph", "semigraph.agent", "semigraph.agent.stream", "semigraph.agent.stream_async",
                             "semigraph.agent.graph")
    assert done.returncode == 0, done.stdout + done.stderr


def test_selecting_a_twin_for_a_non_agent_ask_never_imports_the_agent():
    """The ``AGENT_ENABLED``-off deployment: every non-agent ask (SEC or workspace) picks its twin without the agent
    package ever being imported (the lazy import in ``select_twin`` runs only for ``strategy=agent``)."""
    done = imports_leave_out("from semigraph.serve import routes; "
                             "assert routes._stream_fn('hybrid') is routes.aanswer_stream; "
                             "assert routes._stream_fn('vector') is routes.aanswer_stream; "
                             "assert routes._stream_fn('hybrid', True) is routes.astream_workspace_answer",
                             "langgraph", "semigraph.agent", "semigraph.agent.stream", "semigraph.agent.stream_async",
                             "semigraph.agent.graph")
    assert done.returncode == 0, done.stdout + done.stderr


def test_only_the_entry_point_needs_langgraph_and_it_imports_where_langgraph_is_installed():
    """The tools, planner, merge, sanitize, state and trace modules, and the shared test fakes, never import langgraph; ``stream``
    and ``stream_async`` (through ``graph``) do."""
    code = ("import sys; import semigraph.agent.tools, semigraph.agent.trace, semigraph.agent.merge, semigraph.agent.sanitize, "
            "semigraph.agent.planner, semigraph.agent.state; assert 'langgraph' not in sys.modules, 'a light module imports langgraph'; "
            f"sys.path.insert(0, {str(Path(__file__).parent)!r}); import agent_fakes; "
            "assert 'langgraph' not in sys.modules, 'the shared fakes import langgraph (the serve-shipped CI job has none)'; "
            "from semigraph.agent.stream import agent_answer_stream, agent_answer; assert 'langgraph' in sys.modules; "
            "from semigraph.agent.stream_async import aagent_answer_stream")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=SRC.parents[1], env=ENV)
    assert done.returncode == 0, done.stdout + done.stderr


def _module_level_imports(tree: ast.Module):
    """Import statements executed at import time: the module body, and inside top-level if / try blocks (not inside functions)."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            stack.extend(ast.iter_child_nodes(node))


def _mentions(node, *needles: str) -> bool:
    if isinstance(node, ast.Import):
        return any(any(part in needles for part in alias.name.split(".")) for alias in node.names)
    module_parts = (node.module or "").split(".")
    return any(part in needles for part in module_parts) or (node.level > 0 and any(alias.name in needles for alias in node.names))


def test_no_module_outside_the_agent_package_imports_langgraph_or_the_agent_at_module_level():
    offenders = []
    for path in SRC.rglob("*.py"):
        if "agent" in path.relative_to(SRC).parts[:1] or path.parts[-2] == "agent":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.relative_to(SRC)}:{node.lineno}" for node in _module_level_imports(tree) if _mentions(node, "langgraph", "agent")]
    assert offenders == []


def test_langgraph_is_imported_only_by_the_graph_module():
    imports = [p.name for p in (SRC / "agent").glob("*.py")
               if any(_mentions(n, "langgraph") for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
                      if isinstance(n, (ast.Import, ast.ImportFrom)))]
    assert imports == ["graph.py"]

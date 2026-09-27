"""The serving image must not need the agent while the flag is off (docs/v2/M3_AGENT_PLAN.md section 0 and 6).

langgraph is imported only by ``semigraph/agent/graph.py`` (so only through ``semigraph.agent.stream``); no module outside the package
imports the agent at MODULE level (``routes._stream_fn`` and ``eval.agent_eval`` import it inside a function); importing the route
module pulls in neither langgraph nor the agent. The import checks run in a SUBPROCESS: other tests in this session have already put
langgraph into ``sys.modules``.
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
    done = imports_leave_out("import semigraph.serve.routes, semigraph.serve.main, semigraph.serve.guard, semigraph.retrieval.answerer",
                             "langgraph", "semigraph.agent", "semigraph.agent.stream", "semigraph.agent.graph")
    assert done.returncode == 0, done.stdout + done.stderr


def test_only_the_entry_point_needs_langgraph_and_it_imports_where_langgraph_is_installed():
    """The tools, planner, merge, sanitize, state and trace modules, and the shared test fakes, never import langgraph; ``stream``
    (through ``graph``) does."""
    code = ("import sys; import semigraph.agent.tools, semigraph.agent.trace, semigraph.agent.merge, semigraph.agent.sanitize, "
            "semigraph.agent.planner, semigraph.agent.state; assert 'langgraph' not in sys.modules, 'a light module imports langgraph'; "
            f"sys.path.insert(0, {str(Path(__file__).parent)!r}); import agent_fakes; "
            "assert 'langgraph' not in sys.modules, 'the shared fakes import langgraph (the serve-shipped CI job has none)'; "
            "from semigraph.agent.stream import agent_answer_stream, agent_answer; assert 'langgraph' in sys.modules")
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

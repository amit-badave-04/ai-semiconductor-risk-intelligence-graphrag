"""The fixture server for Playwright, axe and Lighthouse (docs/specs/m5b-frontend.md S13): the real routers and the real
``ui_v2`` over fakes, serving a built ``web/dist``. No Neo4j, no LLM, no embedder, no ``.env``.

    uv run python tests/e2e/serve_fixture_app.py --dist web/dist --port 8765 [--site-key KEY] [--ui-v2-preview] [--ui-v2]
    uv run python tests/e2e/serve_fixture_app.py --check --dist web/dist        (build, report, exit: the CI smoke)

It builds the app from :class:`serve_app_factory.FakeSettings`, never from ``Settings``: that class reads ``.env`` from the
working directory without exporting it, so a check of ``os.environ`` alone would pass with live keys on disk. And a route left
unfaked would still fall back to ``get_settings()`` lazily at request time (``embeddings.py``, ``graph/client.py``,
``agent/stream_async.py``). So the process guards itself for its whole life: before anything is built it calls
:func:`install_guards`, which replaces ``Settings.__init__``, ``semigraph.config.get_settings``, ``litellm.completion`` and
``litellm.acompletion`` with raisers that also append to a hit list, and rewrites every loaded ``semigraph.*`` module attribute that
is one of the originals (the code binds them by name: ``from ..config import get_settings``). Nothing undoes it in the server.

It never imports ``semigraph.serve.main`` (``app = create_app()`` runs there at import and calls ``get_settings()``), refuses to
start under ``ENVIRONMENT=production``, with ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY`` in its environment, or asked to bind
anything but 127.0.0.1; the same refusals run again once the app is built, so a key put into the environment by an import or the
build still stops it. It never reads ``.env``: litellm's import calls ``dotenv.load_dotenv()`` unless ``LITELLM_MODE`` is not
``DEV``, and ``find_dotenv`` would walk up from the installed package to the repo root, so :func:`main` forces
``LITELLM_MODE=PRODUCTION`` as its first statement (a test replaces ``load_dotenv`` with a recorder in a child process and
requires zero calls). It makes no outbound call of its own: litellm is told to use its bundled price list instead of fetching
one when it is imported (``LITELLM_LOCAL_MODEL_COST_MAP``, set in :func:`main` before the first import), and the app is served
from disk.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:               # run as a script, tests/ is not on the path (the factory lives there)
    sys.path.insert(0, str(TESTS_DIR))

LOOPBACK = "127.0.0.1"
PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
DEFAULT_PORT = 8765
EXIT_REFUSED = 2
COST_MAP_VARIABLE = "LITELLM_LOCAL_MODEL_COST_MAP"
# litellm/__init__.py and litellm/proxy/proxy_cli.py both call dotenv.load_dotenv() on import when LITELLM_MODE is "DEV" (the
# default), and find_dotenv walks up from litellm's own file through the .venv to the repo root: so any import of litellm
# would put every key and credential of the repo's .env into this process. Forced, never setdefault: a DEV in the shell loses.
LITELLM_MODE_VARIABLE = "LITELLM_MODE"
LITELLM_MODE_VALUE = "PRODUCTION"


class GuardViolation(RuntimeError):
    """Something in the fixture server reached for the real settings or an LLM provider."""


def refusals(env: Mapping[str, str], host: str) -> list[str]:
    """Why the server must not start, given an environment and a bind host (empty: it may). Pure, so a test passes its own
    environment instead of the developer's. A provider key counts when the variable is present at all, whatever its value."""
    reasons = []
    if env.get("ENVIRONMENT", "").strip().lower() == "production":
        reasons.append("ENVIRONMENT is production")
    reasons += [f"{name} is present in the environment" for name in PROVIDER_KEYS if name in env]
    if host != LOOPBACK:
        reasons.append(f"it binds only {LOOPBACK}, not {host!r}")
    return reasons


class Guards:
    """The installed raisers. ``hits`` is appended to by every call that reached one (a caller that swallows the error still
    shows up here). ``undo()`` (or calling the object) puts the originals back everywhere, including in modules that were
    imported while the guards were on and bound a raiser."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.swaps: list[tuple[object, str, object, Callable]] = []       # (owner, attribute, original, raiser)

    def undo(self) -> None:
        from semigraph import config

        raisers = {id(raiser): original for _, _, original, raiser in self.swaps}
        for owner, attribute, original, _ in self.swaps:
            setattr(owner, attribute, original)
        for module in semigraph_modules():
            for name, value in list(vars(module).items()):
                if id(value) in raisers:
                    setattr(module, name, raisers[id(value)])
        if "__init__" in vars(config.Settings):
            del config.Settings.__init__

    __call__ = undo


def semigraph_modules() -> list:
    return [m for name, m in list(sys.modules.items())
            if m is not None and (name == "semigraph" or name.startswith("semigraph.")) and hasattr(m, "__dict__")]


def install_guards() -> Guards:
    import litellm
    from semigraph import config

    guards = Guards()

    def raiser(label: str) -> Callable:
        def refuse(*args, **kwargs):
            guards.hits.append(label)
            raise GuardViolation(f"{label} was called in the fixture server (it builds nothing from Settings and calls no LLM)")
        return refuse

    get_settings_raiser = raiser("get_settings")
    get_settings_raiser.cache_clear = lambda: None                          # tests call it on the real one
    swaps = [(config, "get_settings", config.get_settings, get_settings_raiser),
             (litellm, "completion", litellm.completion, raiser("litellm.completion")),
             (litellm, "acompletion", litellm.acompletion, raiser("litellm.acompletion"))]
    for owner, attribute, original, refuse in swaps:
        setattr(owner, attribute, refuse)
        guards.swaps.append((owner, attribute, original, refuse))
    config.Settings.__init__ = raiser("Settings()")
    by_original = {id(original): refuse for _, _, original, refuse in swaps}
    for module in semigraph_modules():                                       # the by-name bindings of loaded modules
        for name, value in list(vars(module).items()):
            if id(value) in by_original:
                setattr(module, name, by_original[id(value)])
    return guards


def build_fixture_app(dist: Path, *, site_key: str = "", ui_v2_preview: bool = False, ui_v2: bool = False,
                      agent_enabled: bool = False, mp=None):
    """The app over fakes. ``mp`` is the ``pytest.MonkeyPatch`` the answer and data patches go through (a test passes its
    own and they are undone; the server keeps them)."""
    import pytest
    from serve_app_factory import FakeSettings, build_app, patch_answer_paths, patch_data_paths

    mp = mp if mp is not None else pytest.MonkeyPatch()
    patch_answer_paths(mp)
    patch_data_paths(mp)
    settings = FakeSettings(turnstile_site_key=site_key, ui_v2_preview=ui_v2_preview, ui_v2=ui_v2, ui_dist_dir=dist,
                            agent_enabled=agent_enabled)
    return build_app(settings)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=REPO_ROOT / "web" / "dist", help="the built app (default web/dist)")
    parser.add_argument("--host", default=LOOPBACK, help=f"refused unless {LOOPBACK}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--site-key", default="", help="a dummy Turnstile site key for GET /api/config (default empty: no widget)")
    parser.add_argument("--ui-v2-preview", action="store_true", help="UI_V2_PREVIEW: the app at /v2/")
    parser.add_argument("--ui-v2", action="store_true", help="UI_V2: the app at /")
    parser.add_argument("--agent", action="store_true", help="offer strategy=agent (the stream is a fake)")
    parser.add_argument("--check", action="store_true", help="build the app, report, exit without serving")
    return parser.parse_args(argv)


def check_report(app, guards: Guards) -> dict:
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    config = client.get("/api/config")
    return {"serve_main_imported": "semigraph.serve.main" in sys.modules, "guard_hits": list(guards.hits),
            "config_status": config.status_code, "config": config.json(), "spa_served": hasattr(app.state, "ui_v2_spa"),
            "litellm_local_cost_map": os.environ.get(COST_MAP_VARIABLE),
            "litellm_mode": os.environ.get(LITELLM_MODE_VARIABLE),
            "provider_keys_present": [name for name in PROVIDER_KEYS if name in os.environ]}      # names only, never values


def main(argv: list[str] | None = None) -> int:
    os.environ[LITELLM_MODE_VARIABLE] = LITELLM_MODE_VALUE                 # FIRST: litellm's import loads .env unless this is set
    args = parse_args(argv)
    reasons = refusals(os.environ, args.host)
    if reasons:
        print("the fixture server refuses to start: " + "; ".join(reasons), file=sys.stderr)
        return EXIT_REFUSED
    os.environ.setdefault(COST_MAP_VARIABLE, "True")                       # before litellm is imported: no remote price list
    guards = install_guards()                                              # first thing, before the app is built
    app = build_fixture_app(args.dist, site_key=args.site_key, ui_v2_preview=args.ui_v2_preview, ui_v2=args.ui_v2,
                            agent_enabled=args.agent)
    reasons = refusals(os.environ, args.host)                              # again: whatever the imports and the build loaded
    if reasons:
        print("the fixture server refuses to start (after the build): " + "; ".join(reasons), file=sys.stderr)
        return EXIT_REFUSED
    if args.check:
        print(json.dumps(check_report(app, guards)))
        return 0
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

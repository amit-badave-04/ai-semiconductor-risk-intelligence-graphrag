"""Serving of the new single-page app, beside the old page (docs/specs/m5b-frontend.md R7-R13, D6, D10).

The surface per state of the two flags (``config.Settings.ui_v2_preview`` / ``ui_v2``), with the built app in ``ui_dist_dir``:

    UI_V2_PREVIEW  UI_V2  /          /legacy    /v2/*             root pages
    off            off    old page   404        404               404
    on             off    old page   old page   new app, noindex  404
    any            on     new app    old page   new app, noindex  new app

Everything is decided ONCE, in :func:`install` while ``create_app()`` runs, and recorded on ``app.state``: a flag that is off
registers nothing, and a dist directory without a readable ``index.html`` registers nothing and logs one warning, so the
state falls back to the first row (the old page at ``/``). That keeps the old page up whatever happens to the new one, and
``create_app()`` (which runs at import) never raises over it. ``GET /api/config`` is the one route registered whatever
the flags and the dist say.

Every HTML response is built by :func:`html_response`: it copies ``routes.SECURITY_HEADERS`` into a NEW dict (the shared one is
never touched, so ``X-Robots-Tag`` can never reach ``/``) and puts the pinned CSP on the page. The file branches never
return ``text/html`` (:func:`is_html_path`), so nothing HTML can leave them without the helper. Every file response
carries ``X-Content-Type-Options: nosniff`` and, a script apart, a sandboxing CSP (:data:`FILE_CSP`). There is no root
catch-all: the root pages are enumerated, ``/v2/`` is the only prefix with a fallback, and ``/api/*`` is never answered
here.
"""

import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ..config import UI_DIST_DIR_DEFAULT
from ..universe import FILERS
from . import routes

logger = logging.getLogger("semigraph.serve.ui_v2")

# The pages of the app as the router knows them (``{ticker}`` is a path parameter). ``scripts/export_ids.py`` writes them into
# ``web/src/lib/pages.generated.ts`` so the client and the server cannot disagree about which paths exist.
SPA_PAGES = ("/", "/company/{ticker}/", "/workspace/", "/freshness/", "/method/")
V2_PREFIX = "/v2"
V2_HOME = f"{V2_PREFIX}/"
ASSETS_PREFIX = f"{V2_PREFIX}/assets"
LEGACY_PATH = "/legacy"
DEFAULT_DIST_DIR = UI_DIST_DIR_DEFAULT      # the image path: the one object ``config.Settings.ui_dist_dir`` defaults to as well
STATE_KEY = "ui_v2_spa"                       # app.state attribute: a :class:`SpaState`, present only when the new app is served
LEGACY_PAGE = Path(__file__).parent / "static" / "index.html"
TURNSTILE_PLACEHOLDER = "__TURNSTILE_SITE_KEY__"

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"       # hashed build files under /v2/assets/
REVALIDATE_CACHE = "no-cache"                                 # pages and the unhashed files of the dist root
NO_STORE = "no-store"
NOINDEX = "noindex"
NOSNIFF = "nosniff"
# For every file response but a script's. A document a browser can open (an SVG, an XML or XSL file) gets no scripting and no
# fetching; a stylesheet or a font is unaffected, since the CSP of such a response does not govern the page that loads it.
FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
JAVASCRIPT_EXTENSIONS = frozenset({".js", ".mjs"})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".xht", ".shtml"})
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# The non-slash form of an enumerated page redirects to the value here, never to anything built from the request.
PAGE_REDIRECTS = {"/workspace": "/workspace/", "/freshness": "/freshness/", "/method": "/method/"}

config_router = APIRouter()                   # GET /api/config: always registered
legacy_router = APIRouter()                   # GET /legacy
v2_router = APIRouter()                       # /v2 and /v2/* (route paths stay literals: a test scans for them)
root_router = APIRouter()                     # the enumerated root pages, once UI_V2 flips the root


@dataclass(frozen=True)
class SpaState:
    """What :func:`install` found: the dist's ``index.html`` (read once at boot; a few hundred bytes), the dist root served as
    files, and whether the root pages answer (``UI_V2``)."""

    index_html: str
    files: "DistFiles"
    root_pages: bool


# ---------------------------------------------------------------- headers

def html_response(page: str, *, noindex: bool = False) -> HTMLResponse:
    """The ONE builder of an HTML response. ``noindex`` only for the ``/v2/`` pages; ``/`` and ``/legacy`` never carry it."""
    headers = {**routes.SECURITY_HEADERS, "Cache-Control": REVALIDATE_CACHE}
    if noindex:
        headers["X-Robots-Tag"] = NOINDEX
    return HTMLResponse(page, headers=headers)


def legacy_response(request: Request) -> HTMLResponse:
    """The old single page with the Turnstile site key substituted (what ``GET /`` has always served)."""
    page = LEGACY_PAGE.read_text(encoding="utf-8")
    page = page.replace(TURNSTILE_PLACEHOLDER, request.app.state.settings.turnstile_site_key)
    return html_response(page)


def spa_of(request: Request) -> SpaState | None:
    return getattr(request.app.state, STATE_KEY, None)


def index_response(request: Request) -> HTMLResponse:
    """``GET /``: the new app when ``UI_V2`` is on and the dist loaded, else the old page. ``routes.index`` calls this; an app
    that never ran :func:`install` (a test's hand-built one) has no state and so serves the old page."""
    spa = spa_of(request)
    if spa is not None and spa.root_pages:
        return html_response(spa.index_html)
    return legacy_response(request)


def spa_page(request: Request, *, noindex: bool) -> HTMLResponse:
    spa = spa_of(request)
    if spa is None:
        raise HTTPException(status_code=404)
    return html_response(spa.index_html, noindex=noindex)


# ---------------------------------------------------------------- files

def is_unsafe_dist_path(path: str) -> bool:
    """True for a decoded request path that must never reach the file system: a backslash (a separator on Windows, a file
    name character on Linux: refused on both so they agree), a NUL, or a ``..`` segment. The lookup contains itself as well
    (``StaticFiles.lookup_path`` checks the real path against the directory); this is the first layer and the one a test
    can read."""
    return "\\" in path or "\x00" in path or ".." in path.split("/")


def is_html_path(path: str) -> bool:
    """True when the file name would be served as HTML (by extension, or by the media type the standard library guesses).
    Trailing dots and spaces are dropped first: Windows ignores them when it opens a file. So is everything from the first
    colon of the last segment (``index.html::$DATA`` is an NTFS stream spelling of ``index.html``)."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].split(":", 1)[0].rstrip(". ").lower()
    return os.path.splitext(name)[1] in HTML_EXTENSIONS or mimetypes.guess_type(name)[0] in HTML_MEDIA_TYPES


def is_javascript_path(path: str) -> bool:
    """True for a script by its file name, never by a content type (a 304 has none, and on Windows the registry can retype
    ``.js``). Exact on purpose: a name that is not plainly a script is not exempt from the CSP."""
    return os.path.splitext(path)[1].lower() in JAVASCRIPT_EXTENSIONS


def route_path_of(scope: Scope) -> str:
    root = scope.get("root_path", "")
    path = scope["path"]
    return path[len(root):] if root and path.startswith(root) else path


class DistFiles(StaticFiles):
    """``StaticFiles`` over the built app, constructed with ``check_dir=False`` so a missing directory is a 404 per request and
    never a boot failure (and ``check_config`` is a no-op for the same reason: the stock one raises on the first request).
    It never follows a link out of the directory, never lists one, never serves an HTML file, and stamps the cache,
    ``nosniff`` and CSP headers (also on a 304, which is why they are set on whichever response ``file_response`` returned).
    ``file_response`` is the only place a response is built here, so "every response" is the 200 and the 304: a 404 is the
    app's JSON error."""

    def __init__(self, directory: Path, *, immutable: bool) -> None:
        super().__init__(directory=directory, check_dir=False, follow_symlink=False)
        self.cache_control = IMMUTABLE_CACHE if immutable else REVALIDATE_CACHE

    async def check_config(self) -> None:
        return None

    def get_path(self, scope: Scope) -> str:
        if is_unsafe_dist_path(route_path_of(scope)):
            raise HTTPException(status_code=404)
        return super().get_path(scope)

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Judges the REQUEST name. A colon is refused in every file branch: Vite output names never hold one, and on Windows
        it is how an NTFS alternate data stream (``favicon.svg::$DATA``) names a file the plain name already reaches, so Linux
        (no such file: 404) and Windows (200) would otherwise disagree. The pages are not affected: a last segment with no
        extension is the app, and never gets here.

        An ``OSError`` that Starlette re-raises (it maps only ENAMETOOLONG to a 404) is a 404 too: Windows answers EINVAL for a
        300-character segment and for a name with ``* ? < > | "``, and a link cycle answers ELOOP. Whatever the system refuses
        to open is, to a client, a file that is not there. A length cap in :func:`is_unsafe_dist_path` would cover only one of
        these, and would turn a long deep link of the app (a 200, R9c) into a 404."""
        if is_html_path(path) or ":" in path:
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except OSError as exc:
            logger.info("a dist lookup raised OSError errno=%s: answered 404", exc.errno)
            raise HTTPException(status_code=404) from exc

    def file_response(self, full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope,
                      status_code: int = 200) -> Response:
        """Judges the RESOLVED file, the one layer that sees what the lookup really opened (on Windows ``realpath`` drops a
        stream suffix and expands a short name, so the request name and the file can differ). It runs before the response is
        built, so a 304 (which carries no content type) cannot slip past it, and it uses the very extension logic
        ``FileResponse`` guesses its content type from."""
        if is_html_path(str(full_path)):
            raise HTTPException(status_code=404)
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = self.cache_control
        response.headers["X-Content-Type-Options"] = NOSNIFF
        # No CSP on a script: the CSP of a response governs a Web Worker built from it (``default-src 'none'`` would stop the
        # worker fetching or importing), and a script opened directly is shown as text. The file NAME decides, so a 304 agrees.
        if not is_javascript_path(str(full_path)):
            response.headers["Content-Security-Policy"] = FILE_CSP
        return response


def read_index(dist: Path) -> str | None:
    """The dist's ``index.html``, or None when it is absent, unreadable, a directory or not UTF-8."""
    try:
        return (dist / "index.html").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


# ---------------------------------------------------------------- GET /api/config (R8)

@config_router.get("/api/config")
async def public_config(request: Request) -> JSONResponse:
    """The one value the client cannot get from its own build: the Turnstile site key (public by design: it is in the old
    page today). The flags are not exposed; the client takes its base path from the URL it was served at."""
    return JSONResponse({"turnstile_site_key": request.app.state.settings.turnstile_site_key},
                        headers={"Cache-Control": NO_STORE})


# ---------------------------------------------------------------- /legacy and /v2/*

@legacy_router.get(LEGACY_PATH, response_class=HTMLResponse)
async def legacy(request: Request) -> HTMLResponse:
    return legacy_response(request)


@v2_router.get("/v2", include_in_schema=False)
async def v2_without_slash() -> RedirectResponse:
    return RedirectResponse(V2_HOME)


@v2_router.get("/v2/{path:path}", include_in_schema=False)
async def v2_path(path: str, request: Request) -> Response:
    """A last segment without an extension is a page of the app (it renders its own not-found view), served with status 200 so
    a deep link works; one with an extension is a file of the dist root, looked up under :func:`is_unsafe_dist_path` and the
    directory containment, and never HTML. ``/v2/assets/*`` is answered by the mount before this route is reached."""
    spa = spa_of(request)
    if spa is None or is_unsafe_dist_path(path):
        raise HTTPException(status_code=404)
    if not os.path.splitext(path.rsplit("/", 1)[-1])[1]:
        return html_response(spa.index_html, noindex=True)
    return await spa.files.get_response(os.path.normpath(os.path.join(*path.split("/"))), request.scope)


# ---------------------------------------------------------------- the root pages (UI_V2)

@root_router.get("/workspace/", include_in_schema=False)
@root_router.get("/freshness/", include_in_schema=False)
@root_router.get("/method/", include_in_schema=False)
async def root_page(request: Request) -> HTMLResponse:
    return spa_page(request, noindex=False)


@root_router.get("/company/{ticker}/", include_in_schema=False)
async def root_company_page(ticker: str, request: Request) -> HTMLResponse:
    """Any ticker serves the app: it renders the not-found view for one it does not know (R25)."""
    return spa_page(request, noindex=False)


@root_router.get("/workspace", include_in_schema=False)
@root_router.get("/freshness", include_in_schema=False)
@root_router.get("/method", include_in_schema=False)
async def root_page_without_slash(request: Request) -> RedirectResponse:
    target = PAGE_REDIRECTS.get(request.scope["path"])
    if target is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(target)


@root_router.get("/company/{ticker}", include_in_schema=False)
async def root_company_without_slash(ticker: str) -> RedirectResponse:
    """The redirect names a ticker of the universe (a key of ``FILERS``), never the request string."""
    canonical = ticker.upper()
    if canonical not in FILERS:
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/company/{canonical}/")


# ---------------------------------------------------------------- wiring

def install(app: FastAPI, settings: object) -> None:
    """Called LAST in ``main.create_app()`` (and by the fixture server's factory). ``settings`` is read with ``getattr`` and a
    default, so it is typed ``object``: settings without the new attributes (a test double) mean flags off."""
    app.include_router(config_router)
    preview, flip = bool(getattr(settings, "ui_v2_preview", False)), bool(getattr(settings, "ui_v2", False))
    if not (preview or flip):
        return
    dist = Path(getattr(settings, "ui_dist_dir", DEFAULT_DIST_DIR))
    index_html = read_index(dist)
    if index_html is None:
        logger.warning("UI_V2_PREVIEW or UI_V2 is on but %s has no readable index.html: the new app is not served and the "
                       "old page keeps answering", dist)
        return
    setattr(app.state, STATE_KEY, SpaState(index_html=index_html, files=DistFiles(dist, immutable=False), root_pages=flip))
    app.mount(ASSETS_PREFIX, DistFiles(dist / "assets", immutable=True), name="v2-assets")     # before the page routes
    for router in (legacy_router, v2_router, *((root_router,) if flip else ())):
        app.include_router(router)

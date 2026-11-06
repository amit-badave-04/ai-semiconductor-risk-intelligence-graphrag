"""serve/ui_v2.py: the flags, the routes of the new single-page app, the header helper (docs/specs/m5b-frontend.md R7-R13, S5,
S9, D6, D10, D16).

Every test builds the REAL ``main.create_app()`` over real ``Settings(_env_file=None)`` (the flags and the dist directory set
per test) and a tmp dist directory (no Node, no ``web/``): ``TestClient`` without ``with`` never starts the lifespan, so no
database, embedder or model is touched. ``app.state.settings`` is set by hand, which is all the pages and ``/api/config`` read.
The CSP is pinned here as a LITERAL (not ``routes.CSP``, which would make the comparison tautological).
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from serve_app_factory import FIXTURE_CHUNK_ID, fake_lifespan, install_fake_state, patch_data_paths
from test_serve_lifespan import M4_PATHS

from semigraph.config import Settings
from semigraph.serve import main, routes, ui_v2

CSP_LITERAL = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
               "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://challenges.cloudflare.com; "
               "img-src 'self' data:; base-uri 'none'; form-action 'none'")
OTHER_SECURITY_HEADERS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                          "referrer-policy": "no-referrer",
                          "strict-transport-security": "max-age=31536000; includeSubDomains"}
SITE_KEY = "dummy-site-key-not-a-cloudflare-key"
SPA_MARKER = "<!-- spa-test-dist -->"
CANARY = "CANARY-OUTSIDE-THE-DIST"
INDEX_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>dist</title>'
              '<script type="module" src="/v2/assets/app-1a2b3c.js"></script></head>'
              f'<body>{SPA_MARKER}<div id="root"></div></body></html>')
IMMUTABLE = "public, max-age=31536000, immutable"
CHUNK_ID = "0001045810-26-000021:I.1:0320"

STATES = [(False, False), (True, False), (False, True), (True, True)]      # (UI_V2_PREVIEW, UI_V2)
ROOT_PAGES = ["/workspace/", "/freshness/", "/method/", "/company/NVDA/"]
V2_PAGES = ["/v2/", "/v2/company/NVDA/", "/v2/workspace/", "/v2/some/deep/link"]
PATH_PARAMS = {"evidence_id": CHUNK_ID, "ticker": "NVDA", "ws": "0" * 32, "job_id": "1" * 32,
               "doc_id": "doc:0123456789ab:v1:0001"}


# ---------------------------------------------------------------- the fixtures

@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell (or a CI job) must neither set nor clear the flags these tests build."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def dist(tmp_path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "app-1a2b3c.js").write_text("console.log('app');", encoding="utf-8")
    (root / "assets" / "app-1a2b3c.css").write_text("body{margin:0}", encoding="utf-8")
    (root / "assets" / "x.js").write_text("console.log('x');", encoding="utf-8")
    (root / "assets" / "x.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (root / "page.html").write_text("<script>alert(2)</script>", encoding="utf-8")
    (root / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    (tmp_path / "canary.txt").write_text(CANARY, encoding="utf-8")        # next to the dist, outside it
    return root


def build_app(monkeypatch, dist_dir, *, preview=False, flip=False, site_key=SITE_KEY):
    settings = Settings(_env_file=None, ui_v2_preview=preview, ui_v2=flip, ui_dist_dir=dist_dir,
                        turnstile_site_key=site_key)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.state.settings = settings
    return app


def build_served_app(monkeypatch, dist_dir, **flags):
    """``create_app()`` with the fake state ``main.lifespan`` would set, the fake lifespan, and the data paths answered from
    the hand-written fixtures: the API routes answer for real, so a walk over them checks answers and not error pages."""
    app = build_app(monkeypatch, dist_dir, **flags)
    install_fake_state(app, app.state.settings)
    app.router.lifespan_context = fake_lifespan(app.state.settings)
    patch_data_paths(monkeypatch)
    return app


def client_of(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def legacy_body(site_key=SITE_KEY) -> str:
    """The old page as ``GET /`` served it before M5b, rebuilt here without ``ui_v2``."""
    page = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return page.replace("__TURNSTILE_SITE_KEY__", site_key)


def classify(response) -> object:
    if response.status_code != 200:
        return response.status_code
    if response.text == legacy_body():
        return "legacy"
    if SPA_MARKER in response.text:
        return "spa-noindex" if response.headers.get("x-robots-tag") == "noindex" else "spa"
    return "other"


def asgi_get(app, decoded_path: str) -> tuple[int, dict, bytes]:
    """One GET straight into the ASGI app with the path already percent-decoded, the way uvicorn hands it over. An HTTP
    client may normalise dot segments before it sends; this cannot, so a traversal test over it is not vacuous."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": decoded_path, "raw_path": decoded_path.encode("utf-8"), "root_path": "", "query_string": b"",
             "headers": [(b"host", b"testserver"), (b"accept", b"text/html")],
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


# ---------------------------------------------------------------- R7: the settings

def test_the_flags_default_off_and_the_dist_directory_defaults_to_the_image_path():
    settings = Settings(_env_file=None)
    assert settings.ui_v2_preview is False and settings.ui_v2 is False
    assert settings.ui_dist_dir == Path("/srv/web")


def test_the_flags_and_the_directory_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("UI_V2_PREVIEW", "true")
    monkeypatch.setenv("UI_V2", "true")
    monkeypatch.setenv("UI_DIST_DIR", str(tmp_path))
    settings = Settings(_env_file=None)
    assert settings.ui_v2_preview is True and settings.ui_v2 is True and settings.ui_dist_dir == tmp_path


# ---------------------------------------------------------------- R7: the state table (the whole table, in each state)

def expected_surface(preview: bool, flip: bool) -> dict[str, object]:
    on = preview or flip
    spa_v2 = "spa-noindex" if on else 404
    return {"/": "spa" if flip else "legacy", "/legacy": "legacy" if on else 404,
            **{path: spa_v2 for path in V2_PAGES},
            **{path: "spa" if flip else 404 for path in ROOT_PAGES}}


@pytest.mark.parametrize("preview,flip", STATES)
def test_the_state_table_of_r7(monkeypatch, dist, preview, flip):
    client = client_of(build_app(monkeypatch, dist, preview=preview, flip=flip))
    got = {path: classify(client.get(path)) for path in expected_surface(preview, flip)}
    assert got == expected_surface(preview, flip)


def test_both_flags_off_is_todays_page_byte_for_byte_with_only_cache_control_added(monkeypatch, dist):
    response = client_of(build_app(monkeypatch, dist)).get("/")
    assert response.content == legacy_body().encode("utf-8")
    assert response.headers["content-security-policy"] == CSP_LITERAL
    for name, value in OTHER_SECURITY_HEADERS.items():
        assert response.headers[name] == value, name
    assert response.headers["cache-control"] == "no-cache" and "x-robots-tag" not in response.headers


def test_both_flags_off_registers_no_v2_route_at_all(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist))
    for path in ("/legacy", "/v2", "/v2/", "/v2/assets/app-1a2b3c.js", "/v2/index.html", "/workspace/", "/method/"):
        response = client.get(path)
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, path


def test_legacy_serves_the_old_page_through_the_same_function_with_the_same_headers(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, preview=True))
    legacy, index = client.get("/legacy"), client.get("/")
    assert legacy.content == index.content == legacy_body().encode("utf-8")
    assert dict(legacy.headers) == dict(index.headers)
    assert "x-robots-tag" not in legacy.headers


@pytest.mark.parametrize("preview,flip", [(True, False), (False, True)])
def test_the_spa_answers_with_the_pinned_headers_and_noindex_only_under_v2(monkeypatch, dist, preview, flip):
    client = client_of(build_app(monkeypatch, dist, preview=preview, flip=flip))
    for path in V2_PAGES:
        response = client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 200 and SPA_MARKER in response.text, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.headers["content-security-policy"] == CSP_LITERAL, path
        assert response.headers["x-robots-tag"] == "noindex" and response.headers["cache-control"] == "no-cache", path
        for name, value in OTHER_SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)


def test_a_deep_link_serves_index_html_so_the_app_can_render_its_not_found_view(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, preview=True))
    for path in ("/v2/company/NVDA/", "/v2/company/NVDA", "/v2/company/NOPE/", "/v2/unknown/route", "/v2/x.js/"):
        assert client.get(path).text == INDEX_HTML, path


def test_the_flip_serves_the_root_pages_and_keeps_the_v2_aliases(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, flip=True))
    for path in ["/", *ROOT_PAGES]:
        response = client.get(path)
        assert response.text == INDEX_HTML and "x-robots-tag" not in response.headers, path
        assert response.headers["content-security-policy"] == CSP_LITERAL, path
        assert response.headers["cache-control"] == "no-cache", path
    assert client.get("/v2/assets/app-1a2b3c.js").status_code == 200        # the build bakes /v2/ into the asset URLs
    assert client.get("/v2/company/NVDA/").headers["x-robots-tag"] == "noindex"
    assert client.get("/legacy").text == legacy_body()


def test_an_unknown_root_path_stays_a_json_404_after_the_flip(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, flip=True))
    for path in ("/nope", "/nope/", "/company/", "/company/NVDA/extra", "/workspace/extra", "/assets/x.js", "/index.html"):
        response = client.get(path)
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, path


# ---------------------------------------------------------------- R9e: a redirect comes from a fixed template

def test_the_non_slash_form_of_a_root_page_redirects_to_the_enumerated_page(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, flip=True))
    for path in ("/workspace", "/freshness", "/method"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (307, 308) and response.headers["location"] == path + "/", path
    assert client.get("/v2", follow_redirects=False).headers["location"] == "/v2/"


def test_a_company_redirect_names_the_ticker_of_the_universe_never_the_request_string(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, flip=True))
    response = client.get("/company/nvda", follow_redirects=False)
    assert response.status_code in (307, 308) and response.headers["location"] == "/company/NVDA/"
    for hostile in ("/company/%2F%2Fevil.example", "/company/evil.example", "/company/NOPE", "/company/..%2f..%2fx"):
        response = client.get(hostile, follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers, hostile


def test_a_redirect_ignores_the_query_string(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, flip=True))
    response = client.get("/workspace?next=//evil.example/x", follow_redirects=False)
    assert response.headers["location"] == "/workspace/"


# ---------------------------------------------------------------- R10: html_response never mutates the shared dict

def test_html_response_builds_a_new_dict_and_leaves_the_shared_one_alone():
    before = dict(routes.SECURITY_HEADERS)
    plain, hidden = ui_v2.html_response("<p>a</p>"), ui_v2.html_response("<p>b</p>", noindex=True)
    assert routes.SECURITY_HEADERS == before and "X-Robots-Tag" not in routes.SECURITY_HEADERS
    assert plain.headers["content-security-policy"] == CSP_LITERAL and plain.headers["cache-control"] == "no-cache"
    assert "x-robots-tag" not in plain.headers and hidden.headers["x-robots-tag"] == "noindex"
    assert "cache-control" not in routes.SECURITY_HEADERS


@pytest.mark.parametrize("order", [("/v2/", "/"), ("/", "/v2/")])
def test_noindex_never_reaches_slash_or_legacy_in_either_request_order(monkeypatch, dist, order):
    before = dict(routes.SECURITY_HEADERS)
    client = client_of(build_app(monkeypatch, dist, preview=True))
    for path in (*order, "/legacy", "/", "/v2/company/NVDA/", "/legacy"):
        response = client.get(path)
        assert ("x-robots-tag" in response.headers) == path.startswith("/v2"), path
    assert routes.SECURITY_HEADERS == before


# ---------------------------------------------------------------- R8: GET /api/config

@pytest.mark.parametrize("preview,flip", STATES)
def test_api_config_answers_the_one_key_in_every_state(monkeypatch, dist, preview, flip):
    response = client_of(build_app(monkeypatch, dist, preview=preview, flip=flip)).get("/api/config")
    assert response.status_code == 200 and response.json() == {"turnstile_site_key": SITE_KEY}
    assert response.headers["cache-control"] == "no-store"


def test_api_config_is_a_known_no_key_when_the_site_key_is_empty(monkeypatch, dist):
    response = client_of(build_app(monkeypatch, dist, site_key="")).get("/api/config")
    assert response.status_code == 200 and response.json() == {"turnstile_site_key": ""}


def test_api_config_exposes_neither_the_flags_nor_the_directory(monkeypatch, dist):
    body = client_of(build_app(monkeypatch, dist, preview=True, flip=True)).get("/api/config").json()
    assert set(body) == {"turnstile_site_key"}


# ---------------------------------------------------------------- R13: a frontend fault never takes the old page down

def missing_dist_cases(tmp_path):
    no_index = tmp_path / "empty-dist"
    no_index.mkdir()
    index_is_a_directory = tmp_path / "odd-dist"
    (index_is_a_directory / "index.html").mkdir(parents=True)
    return {"absent": tmp_path / "no-such-dir", "no index.html": no_index, "index.html is a directory": index_is_a_directory}


@pytest.mark.parametrize("preview,flip", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("case", ["absent", "no index.html", "index.html is a directory"])
def test_a_missing_dist_falls_back_to_the_first_row_and_keeps_api_config(monkeypatch, tmp_path, caplog, preview, flip, case):
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.ui_v2"):
        app = build_app(monkeypatch, missing_dist_cases(tmp_path)[case], preview=preview, flip=flip)   # does not raise
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and r.name == "semigraph.serve.ui_v2"]) == 1
    client = client_of(app)
    assert client.get("/").content == legacy_body().encode("utf-8")                        # even with UI_V2=true
    for path in ("/v2/", "/v2/company/NVDA/", "/v2/assets/app.js", "/legacy", "/workspace/"):
        response = client.get(path)
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, path
    config = client.get("/api/config")
    assert config.status_code == 200 and config.json() == {"turnstile_site_key": SITE_KEY}
    assert config.headers["cache-control"] == "no-store"
    assert client.get("/api/nope").json() == {"detail": "Not Found"}
    assert "/healthz" in app.openapi()["paths"]                                             # the health route is untouched


def test_flags_off_do_not_read_the_dist_or_warn_about_it(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.ui_v2"):
        app = build_app(monkeypatch, tmp_path / "no-such-dir")
    assert not [r for r in caplog.records if r.name == "semigraph.serve.ui_v2"]
    assert client_of(app).get("/api/config").status_code == 200


def test_a_dist_without_an_assets_directory_answers_404_not_500(monkeypatch, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    client = client_of(build_app(monkeypatch, bare, preview=True))
    assert client.get("/v2/assets/app.js").status_code == 404
    assert client.get("/v2/").status_code == 200


# ---------------------------------------------------------------- R9: /api/* is never answered by these routes

@pytest.mark.parametrize("preview,flip", STATES)
def test_an_unknown_api_path_stays_a_json_404_and_openapi_stays_closed(monkeypatch, dist, preview, flip):
    client = client_of(build_app(monkeypatch, dist, preview=preview, flip=flip))
    for path in ("/api/nope", "/api/admin/nope", "/api/config/extra", "/openapi.json", "/docs", "/redoc"):
        for headers in ({}, {"Accept": "text/html"}):
            response = client.get(path, headers=headers)
            assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, path


# ---------------------------------------------------------------- R9a, R9c: assets and files

def test_assets_are_immutable_and_a_missing_asset_is_a_404_never_the_index(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, preview=True))
    response = client.get("/v2/assets/app-1a2b3c.js")
    assert response.status_code == 200 and response.text == "console.log('app');"
    assert response.headers["cache-control"] == IMMUTABLE
    assert client.get("/v2/assets/app-1a2b3c.css").headers["cache-control"] == IMMUTABLE
    for path in ("/v2/assets/missing.js", "/v2/assets/nope/deeper.js", "/v2/assets/missing", "/v2/assets/"):
        response = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 404 and SPA_MARKER not in response.text, path


def test_a_conditional_asset_request_keeps_the_immutable_header_on_the_304(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, preview=True))
    first = client.get("/v2/assets/app-1a2b3c.js")
    second = client.get("/v2/assets/app-1a2b3c.js", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304 and second.headers["cache-control"] == IMMUTABLE


def test_the_assets_directory_has_no_listing(monkeypatch, dist):
    response = client_of(build_app(monkeypatch, dist, preview=True)).get("/v2/assets/")
    assert response.status_code == 404 and "app-1a2b3c" not in response.text


@pytest.mark.parametrize("path", ["/v2/assets/x.html", "/v2/assets/X.HTML", "/v2/index.html", "/v2/page.html",
                                  "/v2/PAGE.HTM", "/v2/assets/../page.html",
                                  # NTFS alternate-data-stream spellings: Windows `realpath` drops the suffix and the lookup
                                  # then opens the HTML file itself (answered 200 text/html with no CSP before the fix)
                                  "/v2/index.html::$DATA", "/v2/INDEX.HTML::$DATA", "/v2/page.html::$DATA",
                                  "/v2/assets/x.html::$DATA", "/v2/assets/X.Html::$DATA", "/v2/index.html:stream",
                                  "/v2/index.html::$DATA.", "/v2/index.html::$DATA "])
def test_no_html_file_ever_leaves_the_file_branch(monkeypatch, dist, path):
    response = client_of(build_app(monkeypatch, dist, preview=True)).get(path, headers={"Accept": "text/html"})
    assert response.status_code == 404 and "alert(" not in response.text, path
    assert "text/html" not in response.headers.get("content-type", ""), path


@pytest.mark.parametrize("path,content", [
    ("/v2/favicon.svg::$DATA", "<svg"), ("/v2/favicon.svg:stream", "<svg"), ("/v2/assets/x.js::$DATA", "console.log"),
    ("/v2/assets/app-1a2b3c.js::$DATA", "console.log"), ("/v2/assets/app-1a2b3c.css::$DATA", "margin")])
def test_an_alternate_data_stream_spelling_of_any_dist_file_answers_404_on_both_operating_systems(monkeypatch, dist, path, content):
    """Not only HTML: a file is reachable by exactly one name. Vite output names never hold a colon, so a colon in a file
    branch is refused before any lookup (the same answer on Linux, where the file simply does not exist, and on Windows)."""
    client = client_of(build_app(monkeypatch, dist, preview=True))
    response = client.get(path)
    assert response.status_code == 404 and content not in response.text, path
    status, _, body = asgi_get(build_app(monkeypatch, dist, preview=True), path)       # the decoded path, as uvicorn hands it over
    assert status == 404 and content.encode() not in body, path


def test_a_colon_in_an_extensionless_deep_link_is_still_the_app_not_a_file_lookup(monkeypatch, dist):
    """R9c is unchanged: a last segment with no extension is a page of the app (it renders its own not-found view). The colon
    refusal belongs to the file branches only, so `/v2/a:b` and `/v2/company/A:B/` keep answering the app."""
    client = client_of(build_app(monkeypatch, dist, preview=True))
    for path in ("/v2/a:b", "/v2/company/A:B/", "/v2/x::$DATA"):
        response = client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 200 and SPA_MARKER in response.text, path


def file_response_scope() -> dict:
    return {"type": "http", "method": "GET", "headers": []}


@pytest.mark.parametrize("name", ["index.html", "page.html", "assets/x.html"])
def test_the_resolved_file_is_judged_not_the_request_name(dist, name):
    """The request name can differ from the file the lookup resolved (an NTFS stream suffix, an 8.3 short name): so
    ``file_response`` refuses an HTML file whatever the request called it. This is the only layer that sees the resolved
    path, and a direct call tests it on every operating system, where the end-to-end probes above only reach it on Windows."""
    files = ui_v2.DistFiles(dist, immutable=False)
    target = dist / name
    with pytest.raises(HTTPException) as refused:
        files.file_response(target, os.stat(target), file_response_scope())
    assert refused.value.status_code == 404


def test_the_resolved_file_check_leaves_a_non_html_file_alone_and_keeps_the_cache_header(dist):
    files = ui_v2.DistFiles(dist, immutable=False)
    response = files.file_response(dist / "favicon.svg", os.stat(dist / "favicon.svg"), file_response_scope())
    assert response.status_code == 200 and response.headers["cache-control"] == ui_v2.REVALIDATE_CACHE


@pytest.mark.parametrize("name,html", [("index.html", True), ("index.html::$DATA", True), ("INDEX.HTML::$data", True),
                                       ("a/b/page.htm:stream", True), ("x.xhtml::$DATA", True), ("favicon.svg", False),
                                       ("app.js::$DATA", False), ("C:/Windows/win.ini", False)])
def test_is_html_path_ignores_a_stream_suffix(name, html):
    assert ui_v2.is_html_path(name) is html


def test_a_public_file_of_the_dist_root_is_served_and_a_missing_one_is_a_404(monkeypatch, dist):
    client = client_of(build_app(monkeypatch, dist, preview=True))
    response = client.get("/v2/favicon.svg")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    assert "text/html" not in response.headers["content-type"]
    for path in ("/v2/missing.png", "/v2/deeper/missing.png", "/v2/a.b.c"):
        assert client.get(path).status_code == 404, path


# ---------------------------------------------------------------- R9b, S9: the lookup cannot leave the dist

@pytest.mark.parametrize("path,unsafe", [
    ("../canary.txt", True), ("a/../../canary.txt", True), ("assets/../../canary.txt", True), ("..", True),
    ("..\\canary.txt", True), ("assets\\app.js", True), ("x\x00.js", True), ("x.js\x00.html", True),
    ("assets/app-1a2b3c.js", False), ("company/NVDA/", False), ("", False), ("a..b/c", False), ("...", False),
    ("%2e%2e/canary.txt", False),    # a literal percent sign is a file name; the decoded spelling is the first row
])
def test_the_containment_check_judges_the_decoded_path(path, unsafe):
    assert ui_v2.is_unsafe_dist_path(path) is unsafe


@pytest.mark.parametrize("decoded", [
    "/v2/../canary.txt", "/v2/..//canary.txt", "/v2/assets/../../canary.txt", "/v2/assets/..\\..\\canary.txt",
    "/v2/..\\canary.txt", "/v2/a/../../canary.txt", "/v2/canary.txt\x00.js", "/v2/assets/canary.txt\x00.js"])
def test_a_decoded_traversal_answers_404_and_never_reads_outside_the_dist(monkeypatch, dist, decoded):
    app = build_app(monkeypatch, dist, preview=True)
    status, headers, body = asgi_get(app, decoded)
    assert status == 404, decoded
    assert CANARY.encode() not in body and "text/html" not in headers.get("content-type", "")


@pytest.mark.parametrize("encoded", ["/v2/%2e%2e/canary.txt", "/v2/%2e%2e%2fcanary.txt", "/v2/assets/%2e%2e/%2e%2e/canary.txt",
                                     "/v2/..%5ccanary.txt", "/v2/canary.txt%00.js", "/v2/C:/Windows/win.ini",
                                     "/v2/assets/C:/Windows/win.ini", "/v2//Windows/win.ini"])
def test_an_encoded_traversal_through_the_client_answers_404(monkeypatch, dist, encoded):
    response = client_of(build_app(monkeypatch, dist, preview=True)).get(encoded)
    assert response.status_code == 404 and CANARY not in response.text, encoded


def test_a_link_that_leaves_the_dist_is_not_followed(monkeypatch, dist, tmp_path):
    try:
        os.symlink(tmp_path / "canary.txt", dist / "assets" / "link.js")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    client = client_of(build_app(monkeypatch, dist, preview=True))
    assert client.get("/v2/assets/link.js").status_code == 404
    os.symlink(tmp_path / "canary.txt", dist / "link.txt")
    assert client.get("/v2/link.txt").status_code == 404


# ---------------------------------------------------------------- S5: every HTML response carries the pinned CSP

GET_PROBES = ["/v2/assets/x.js", "/v2/assets/x.html", "/v2/index.html", "/v2/page.html", "/v2/favicon.svg", "/v2",
              "/v2/", "/v2/company/NVDA/", "/v2/some/deep/link", "/workspace/", "/workspace", "/freshness/", "/freshness",
              "/method/", "/method", "/company/NVDA/", "/company/NVDA", "/legacy", "/", "/api/config", "/healthz"]
# Routes registered with include_in_schema=False in src/semigraph/serve: the OpenAPI walk cannot list them, so each is probed.
HIDDEN_ROUTE_PROBES = {"/healthz": "/healthz", "/v2": "/v2", "/v2/": "/v2/", "/v2/{path:path}": "/v2/some/deep/link",
                       "/workspace": "/workspace", "/workspace/": "/workspace/", "/freshness": "/freshness",
                       "/freshness/": "/freshness/", "/method": "/method", "/method/": "/method/",
                       "/company/{ticker}": "/company/NVDA", "/company/{ticker}/": "/company/NVDA/"}


def html_allow_list(preview: bool, flip: bool) -> set[str]:
    allowed = {"/"}
    if preview or flip:
        allowed |= {"/legacy", "/v2/", "/v2/company/NVDA/", "/v2/some/deep/link"}
    if flip:
        allowed |= {"/workspace/", "/freshness/", "/method/", "/company/NVDA/"}
    return allowed


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PATH_PARAMS[m.group(1)], path)


def walk_paths(app) -> list[str]:
    """Every GET path of the schema; a path parameter with no table entry fails the test (KeyError names it)."""
    return [concrete(path) for path, methods in app.openapi()["paths"].items() if "get" in methods]


API_ANSWERS = ["/api/config", "/api/stats", "/api/examples", "/api/freshness", "/api/company/NVDA/dossier",
               "/api/company/NVDA/risk-changes", f"/api/evidence/{FIXTURE_CHUNK_ID}"]


@pytest.mark.parametrize("preview,flip", STATES)
def test_every_html_response_is_on_the_allow_list_and_carries_the_literal_csp(monkeypatch, dist, preview, flip):
    app = build_served_app(monkeypatch, dist, preview=preview, flip=flip)
    schema_paths = walk_paths(app)
    seen = {}
    with TestClient(app, raise_server_exceptions=False) as client:                  # the fake lifespan: one loop, real limiters
        for path in dict.fromkeys([*schema_paths, *GET_PROBES]):
            seen[path] = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    for path, response in seen.items():
        if response.headers.get("content-type", "").startswith("text/html"):
            assert path in html_allow_list(preview, flip), f"{path} answered HTML but is not an allowed HTML route"
            assert response.headers["content-security-policy"] == CSP_LITERAL, path
            for name, value in OTHER_SECURITY_HEADERS.items():
                assert response.headers[name] == value, (path, name)
    # JSON stays untouched (R10): no CSP, no robots tag, never the app, and no route died with a 500
    for path, response in seen.items():
        if path.startswith("/api/"):
            assert response.status_code in {200, 400, 404, 422, 503}, (path, response.status_code)   # 422: a required query; 503: uploads off
            assert response.headers["content-type"].startswith("application/json"), path
            assert "content-security-policy" not in response.headers and "x-robots-tag" not in response.headers, path
            assert SPA_MARKER not in response.text, path
    for path in API_ANSWERS:
        assert seen[path].status_code == 200, path                                  # answered for real, over the fakes
    assert seen["/healthz"].status_code == 200
    assert seen["/api/company/NVDA/dossier"].json()["company"]["ticker"] == "NVDA"
    # non-vacuity: the walk reached the pages, the mount and every GET path of the M4 table
    assert "/" in seen and seen["/"].status_code == 200
    assert (seen["/legacy"].status_code == 200) is (preview or flip)
    assert (seen["/v2/"].status_code == 200) is (preview or flip)
    assert (seen["/v2/assets/x.js"].status_code == 200) is (preview or flip)
    for template, methods in M4_PATHS.items():
        if "get" in methods:
            assert concrete(template) in schema_paths, template
    assert {"/api/examples", "/api/stats", "/api/config", "/api/freshness"} <= set(schema_paths)


def test_every_route_hidden_from_the_schema_is_probed():
    """A new ``include_in_schema=False`` route in serve/ must be added to HIDDEN_ROUTE_PROBES (and so to the walk)."""
    found = {}
    for source in sorted(Path(routes.__file__).parent.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"""\.get\(\s*["']([^"']+)["'][^)]*include_in_schema=False""", text):
            found[match.group(1)] = str(source.relative_to(Path(routes.__file__).parent))
    unprobed = {path: name for path, name in found.items() if path not in HIDDEN_ROUTE_PROBES}
    assert not unprobed, f"hidden routes with no probe: {unprobed}"
    assert {"/v2/{path:path}", "/healthz"} <= set(found)
    assert set(HIDDEN_ROUTE_PROBES.values()) <= set(GET_PROBES)


def test_the_walk_reaches_the_flag_dependent_routes_in_the_schema_per_state(monkeypatch, dist):
    off = set(build_app(monkeypatch, dist).openapi()["paths"])
    on = set(build_app(monkeypatch, dist, preview=True).openapi()["paths"])
    assert "/legacy" not in off and "/legacy" in on and "/api/config" in off and "/api/config" in on


# ---------------------------------------------------------------- order: a v2 page cannot shadow an API route

def test_the_v2_routes_come_after_every_api_route(monkeypatch, dist):
    app = build_app(monkeypatch, dist, flip=True)
    client = client_of(app)
    for path in walk_paths(app):
        if path.startswith("/api/"):
            response = client.get(path, headers={"Accept": "text/html"})
            assert SPA_MARKER not in response.text, path
    assert client.get("/api/config").json() == {"turnstile_site_key": SITE_KEY}


# ---------------------------------------------------------------- the existing page's own test still holds with the delegate

def test_the_index_with_the_hand_built_test_app_of_the_api_tests_still_serves_the_old_page():
    """``tests/test_serve_api.py`` builds an app that never ran ``create_app``: no state, no flags, settings without the
    new attributes. The delegate must read all three with defaults."""
    from fastapi import FastAPI

    class OldSettings:
        turnstile_site_key = "k"

    app = FastAPI()
    app.include_router(routes.router)
    app.state.settings = OldSettings()
    response = TestClient(app).get("/")
    assert response.status_code == 200 and response.content == legacy_body("k").encode("utf-8")
    assert response.headers["content-security-policy"] == CSP_LITERAL


@pytest.mark.parametrize("preview,flip", [(True, False), (False, True), (True, True)])
def test_the_server_serves_every_page_of_spa_pages_where_the_state_table_says(monkeypatch, dist, preview, flip):
    """The client's page list is generated from ``SPA_PAGES`` (R5), so the routes must follow the same table."""
    client = client_of(build_app(monkeypatch, dist, preview=preview, flip=flip))
    for page in ui_v2.SPA_PAGES:
        path = page.replace("{ticker}", "NVDA")
        under_v2, at_root = client.get("/v2" + path), client.get(path)
        assert under_v2.text == INDEX_HTML and under_v2.headers["x-robots-tag"] == "noindex", path
        if flip:
            assert at_root.text == INDEX_HTML and "x-robots-tag" not in at_root.headers, path
        elif path != "/":
            assert at_root.status_code == 404, path


def test_the_spa_pages_table_lists_the_five_pages():
    assert ui_v2.SPA_PAGES == ("/", "/company/{ticker}/", "/workspace/", "/freshness/", "/method/")
    assert json.dumps(list(ui_v2.SPA_PAGES))     # plain strings: the generator writes them into TypeScript

def test_the_page_helpers_fail_closed_without_state_or_a_known_redirect():
    """Two guards the routes cannot reach through the table (a route is registered only with its state and its path), kept so
    a later change to the wiring cannot turn them into a 500 or an open redirect."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), scope={"path": "/elsewhere"})
    with pytest.raises(HTTPException) as no_state:
        ui_v2.spa_page(bare, noindex=False)
    assert no_state.value.status_code == 404
    with pytest.raises(HTTPException) as no_target:
        asyncio.run(ui_v2.root_page_without_slash(bare))
    assert no_target.value.status_code == 404

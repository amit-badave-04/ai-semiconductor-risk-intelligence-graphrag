"""The IP-hash pepper (M5a I3: docs/v2/M5A_BUILD_PLAN.md Step 0-I3, docs/v2/M5_DECISIONS.md decisions 6 and 12).

``ip_hash`` used to be an unsalted SHA-256 cut to 64 bits: the whole IPv4 space can be hashed in minutes, so a ledger
row named its client. It is now an HMAC-SHA-256 under ``IP_HASH_PEPPER`` (a Fly secret), IPv6 clients are bucketed by
their /64, every ledger row carries the id of the pepper that made its hash (``ip_hash_v``), and the hashes written
before the pepper are nulled once, by hand, at the cutover. What is pinned here:

* the hash: keyed, deterministic, never the legacy one, never raising on hostile text; the version is STORED beside
  the hash and never MIXED into it (a version bump alone must not reset every per-address window);
* ``hash_request_ip``: the one helper every call site uses; an unset pepper is tolerated outside production only, with
  a process-random one that is logged once; production refuses to boot without a pepper of at least 32 bytes, and the
  refusal never prints the pepper (or any other secret in the environment);
* the ledger writes (``log_query`` with ``ip_hash_v``), the one-off null (``null_legacy_ip_hashes`` and its CLI, which
  does nothing without ``--yes`` and never prints a credential) and the Fly secret push.

Runs in the serve-shipped CI job: no pandas, no sentence-transformers, no Neo4j server (fake drivers throughout).
"""

import ast
import asyncio
import hashlib
import hmac
import importlib.util
import logging
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from serve_state_fakes import (
    FakeStateBackend,
    InMemoryLedger,
    RouteSettings,
    build_route_app,
    fresh_drain,  # noqa: F401 - a fixture
)
from starlette.requests import Request
from test_state_inprocess import state_settings

from semigraph.config import Settings
from semigraph.graph.client import NO_UNRECOGNIZED_NOTIFICATIONS
from semigraph.serve import dossier_routes, drain, guard, monitor_routes, routes, store, workspace_routes
from semigraph.serve.limiters import make_limiters
from semigraph.serve.state import Lease, StateDrivers, make_backend
from semigraph.serve.stream_runtime import PaidStream

pytestmark = pytest.mark.usefixtures("fresh_drain")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "semigraph"
SERVE = SRC / "serve"
PEPPER = "pepper-for-the-tests-0123456789-abcdefgh"          # gitleaks:allow
OTHER_PEPPER = "another-pepper-for-the-tests-9876543210"     # gitleaks:allow
# 31 bytes: one under the production minimum
SHORT_PEPPER = "short-pepper-" + "p" * 18                     # gitleaks:allow
DB_PASSWORD = "db-password-in-the-environment"               # gitleaks:allow
ADMIN_TOKEN = "admin-token-in-the-environment-0123456789"    # gitleaks:allow - 41 characters: production wants 32 or more
Q = "Which HBM suppliers does Nvidia depend on, and which export rules apply?"
LEGACY_ROW_COUNT = 12


def legacy_hash(ip: str) -> str:
    """What ``ip_hash`` produced before the pepper: unsalted SHA-256, 64 bits."""
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def hmac_hash(text: str, pepper: str = PEPPER) -> str:
    return hmac.new(pepper.encode(), text.encode(), hashlib.sha256).hexdigest()[:16]


def request_from(host: str = "203.0.113.9", headers: dict[str, str] | None = None, app=None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "client": (host, 50000),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    if app is not None:
        scope["app"] = app
    return Request(scope)


def cfg(**overrides) -> SimpleNamespace:
    """A settings double with the four attributes ``hash_request_ip`` reads."""
    values = {"client_ip_header": "", "ip_hash_pepper": PEPPER, "ip_hash_version": 2, "is_production": False}
    return SimpleNamespace(**{**values, **overrides})


@pytest.fixture
def fresh_process_pepper(monkeypatch):
    """The module's process-random pepper starts unset, and is put back as it was afterwards."""
    monkeypatch.setattr(guard, "_process_pepper", None)


# ---------------------------------------------------------------- guard.ip_hash

def test_the_same_address_and_pepper_always_give_the_same_16_hex_characters():
    h = guard.ip_hash("203.0.113.9", PEPPER)
    assert h == guard.ip_hash("203.0.113.9", PEPPER)
    assert re.fullmatch(r"[0-9a-f]{16}", h)


def test_a_different_pepper_gives_a_different_hash():
    assert guard.ip_hash("203.0.113.9", PEPPER) != guard.ip_hash("203.0.113.9", OTHER_PEPPER)


def test_the_hash_is_not_the_legacy_unsalted_one_and_is_the_documented_hmac():
    h = guard.ip_hash("203.0.113.9", PEPPER)
    assert h != legacy_hash("203.0.113.9")
    assert h == hmac_hash("203.0.113.9")


def test_a_str_pepper_and_the_same_bytes_are_the_same_key():
    assert guard.ip_hash("203.0.113.9", PEPPER) == guard.ip_hash("203.0.113.9", PEPPER.encode())


def test_the_version_is_stored_beside_the_hash_never_mixed_into_it():
    base = guard.ip_hash("203.0.113.9", PEPPER)
    assert guard.ip_hash("203.0.113.9", PEPPER, 2) == guard.ip_hash("203.0.113.9", PEPPER, 3) == base


@pytest.mark.parametrize("version", [0, -1, True, 2.0, "2"])
def test_a_version_that_is_not_a_positive_integer_is_refused(version):
    """0 marks a row whose hash was nulled at the cutover, so it can never be a pepper's id."""
    with pytest.raises(ValueError, match="version"):
        guard.ip_hash("203.0.113.9", PEPPER, version)


@pytest.mark.parametrize("pepper", ["", b""])
def test_an_empty_pepper_is_refused(pepper):
    with pytest.raises(ValueError, match="IP_HASH_PEPPER"):
        guard.ip_hash("203.0.113.9", pepper)


def test_ipv4_is_hashed_as_it_is_written():
    assert guard.canonical_ip("203.0.113.9") == "203.0.113.9"
    assert guard.ip_hash("203.0.113.9", PEPPER) != guard.ip_hash("203.0.113.10", PEPPER)


def test_ipv6_clients_of_one_slash_64_share_a_hash_and_other_networks_do_not():
    same_network = ["2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::1", "2001:db8:1:2::", "2001:DB8:1:2:0:0:0:ffff"]
    assert len({guard.ip_hash(ip, PEPPER) for ip in same_network}) == 1
    assert guard.ip_hash(same_network[0], PEPPER) == hmac_hash("2001:db8:1:2::")
    for other in ("2001:db8:1:3::1", "2001:db8:2:2::1", "2001:db9:1:2::1"):
        assert guard.ip_hash(other, PEPPER) != guard.ip_hash(same_network[0], PEPPER)


def test_ipv6_spellings_of_one_address_are_one_hash():
    spellings = ["2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1", "2001:DB8::1", "2001:db8:0:0::1"]
    assert len({guard.ip_hash(ip, PEPPER) for ip in spellings}) == 1


def test_an_ipv6_zone_id_is_ignored():
    zoned = ["fe80::1%eth0", "fe80::1%lo0", "fe80::1%12", "fe80::2%eth0", "fe80::1"]
    assert len({guard.ip_hash(ip, PEPPER) for ip in zoned}) == 1


@pytest.mark.parametrize("mapped", ["::ffff:203.0.113.9", "::FFFF:203.0.113.9", "::ffff:cb00:7109",
                                    "0:0:0:0:0:ffff:203.0.113.9"])
def test_an_ipv4_mapped_ipv6_address_is_that_ipv4(mapped):
    assert guard.canonical_ip(mapped) == "203.0.113.9"
    assert guard.ip_hash(mapped, PEPPER) == guard.ip_hash("203.0.113.9", PEPPER)


def test_an_ipv6_address_never_collides_with_an_ipv4_one():
    assert guard.ip_hash("::1", PEPPER) != guard.ip_hash("0.0.0.1", PEPPER)


HOSTILE = ["", " ", "not an ip", "x" * 10_000, "1.2.3.4.5", "999.1.1.1", "ünï-cødé-😀", "\ud800", "\x00\x01",
           "fe80::1%", "::1%" + "z" * 10_000, "1.2.3.4%eth0", ":" * 10_000, "::ffff:1.2.3", "%", "2001:db8::1/64",
           "203.0.113.9, 198.51.100.1", "unknown", "٠.٠.٠.١", "::1\n"]


def test_hostile_text_never_raises_and_is_hashed_deterministically_as_the_raw_string():
    hashes = [guard.ip_hash(text, PEPPER) for text in HOSTILE]
    assert hashes == [guard.ip_hash(text, PEPPER) for text in HOSTILE]
    assert all(re.fullmatch(r"[0-9a-f]{16}", h) for h in hashes)
    assert len(set(hashes)) == len(HOSTILE)                 # distinct text, distinct hash: nothing is folded together
    assert guard.canonical_ip("not an ip") == "not an ip"
    assert guard.ip_hash("not an ip", PEPPER) == hmac_hash("not an ip")


# ---------------------------------------------------------------- guard.hash_request_ip

def test_the_configured_pepper_hashes_the_client_address():
    assert guard.hash_request_ip(request_from("203.0.113.9"), cfg()) == guard.ip_hash("203.0.113.9", PEPPER)


def test_only_a_trusted_header_names_the_client():
    request = request_from("10.9.9.9", {"fly-client-ip": "198.51.100.7, 10.0.0.1", "x-forwarded-for": "192.0.2.1"})
    trusting = cfg(client_ip_header="fly-client-ip")
    assert guard.hash_request_ip(request, trusting) == guard.ip_hash("198.51.100.7", PEPPER)
    assert guard.hash_request_ip(request, cfg()) == guard.ip_hash("10.9.9.9", PEPPER)      # no header named: the socket


@pytest.mark.parametrize("named", [" fly-client-ip", "fly-client-ip ", "\tFly-Client-IP\r\n"])
def test_the_trusted_header_is_found_whatever_whitespace_or_case_the_name_carries(named):
    """``Settings`` stores the name normalized; the guard still never looks up a name with whitespace around it (a
    settings double or a hand-built call), because no header has one and the visitor would silently get the proxy's key."""
    request = request_from("10.9.9.9", {"fly-client-ip": "198.51.100.7"})
    assert guard.client_ip(request, named) == "198.51.100.7"
    assert guard.client_ip(request, "   ") == "10.9.9.9"                 # a blank name is "no header named"


def test_two_clients_of_one_ipv6_slash_64_are_one_window_key():
    a = request_from("2001:db8:aa:bb::1")
    b = request_from("2001:db8:aa:bb:1234:5678:9abc:def0")
    assert guard.hash_request_ip(a, cfg()) == guard.hash_request_ip(b, cfg())


def test_without_a_pepper_outside_production_a_process_random_one_is_used_and_is_stable(fresh_process_pepper):
    settings = cfg(ip_hash_pepper="")
    first = guard.hash_request_ip(request_from(), settings)
    assert first == guard.hash_request_ip(request_from(), settings)
    assert first != legacy_hash("203.0.113.9") and first != guard.ip_hash("203.0.113.9", PEPPER)
    assert len(guard._process_pepper) == 32


def test_a_new_process_gets_a_new_random_pepper(monkeypatch, fresh_process_pepper):
    first = guard.hash_request_ip(request_from(), cfg(ip_hash_pepper=""))
    monkeypatch.setattr(guard, "_process_pepper", None)               # what a restart does
    assert guard.hash_request_ip(request_from(), cfg(ip_hash_pepper="")) != first


def test_the_process_random_pepper_is_announced_once_at_info(caplog, fresh_process_pepper):
    caplog.set_level(logging.INFO, logger="semigraph.serve.guard")
    for _ in range(3):
        guard.hash_request_ip(request_from(), cfg(ip_hash_pepper=""))
    notes = [r for r in caplog.records if "IP_HASH_PEPPER" in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO
    assert "restart" in notes[0].getMessage()
    assert guard._process_pepper.hex() not in caplog.text            # the random pepper itself is never logged


def test_a_configured_pepper_never_creates_the_process_random_one(fresh_process_pepper):
    guard.hash_request_ip(request_from(), cfg())
    assert guard._process_pepper is None


def test_production_without_a_pepper_fails_closed_here_too(fresh_process_pepper):
    """The settings validator keeps this unreachable at boot; if it is reached anyway, no hash is made."""
    with pytest.raises(ValueError, match="IP_HASH_PEPPER"):
        guard.hash_request_ip(request_from(), cfg(ip_hash_pepper="", is_production=True))
    assert guard._process_pepper is None


def test_a_settings_double_without_the_new_attributes_still_works(fresh_process_pepper):
    bare = SimpleNamespace(client_ip_header="")
    assert re.fullmatch(r"[0-9a-f]{16}", guard.hash_request_ip(request_from(), bare))


def test_the_ledger_carries_the_pepper_version_when_the_settings_have_one():
    assert guard.ip_hash_version_fields(cfg(ip_hash_version=3)) == {"ip_hash_v": 3}
    assert guard.ip_hash_version_fields(SimpleNamespace()) == {}


# ---------------------------------------------------------------- settings

PRODUCTION_ENVIRONMENT_VARIABLES = (
    "IP_HASH_PEPPER", "IP_HASH_VERSION", "ENVIRONMENT", "FLY_APP_NAME", "ADMIN_TOKEN", "TURNSTILE_REQUIRED",
    "TURNSTILE_SECRET_KEY", "CLIENT_IP_HEADER", "MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_PER_IP_PER_DAY",
    "MAX_CONCURRENT_ANSWERS", "RATE_LIMIT_QUESTIONS", "FREE_RATE_LIMIT_QUESTIONS", "READ_RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_WINDOW_SECONDS", "CACHE_READ_BUDGET_PER_S", "KILL_SWITCH_STALE_S", "KILL_SWITCH_REFRESH_S")


@pytest.fixture
def clean_environment(monkeypatch):
    """A variable left in the developer's shell must neither fail nor satisfy these tests."""
    for name in PRODUCTION_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


# What production ALSO requires besides the pepper (the live validators, docs/v2/M5_DECISIONS.md 2.2): the bot check on
# with its secret, and the one client-address header Fly's proxy sets. The caps stay at their defaults, which are the
# approved ones. A production Settings built from this base boots, so a refusal of a case below can only be the setting
# that case changes.
TURNSTILE_SECRET = "turnstile-secret-for-the-tests-0123456789"        # gitleaks:allow
PRODUCTION_BASE = {"environment": "production", "turnstile_required": True, "turnstile_secret_key": TURNSTILE_SECRET,
                   "client_ip_header": "fly-client-ip"}
PRODUCTION_SETTING_NAMES = ("IP_HASH_PEPPER", "TURNSTILE_REQUIRED", "TURNSTILE_SECRET_KEY", "CLIENT_IP_HEADER",
                            "MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_PER_IP_PER_DAY",
                            "MAX_CONCURRENT_ANSWERS")


def production(**changes) -> Settings:
    """A production Settings with a valid pepper, unless ``changes`` say otherwise."""
    return settings(**{**PRODUCTION_BASE, "ip_hash_pepper": PEPPER, **changes})


# One entry per way a production Settings is refused in this file, with the setting its message must name.
PRODUCTION_REFUSALS = [
    ({"ip_hash_pepper": ""}, "IP_HASH_PEPPER"),
    ({"ip_hash_pepper": SHORT_PEPPER}, "IP_HASH_PEPPER"),
    ({"ip_hash_pepper": "p" * 31}, "IP_HASH_PEPPER"),
    ({"ip_hash_pepper": "é" * 15 + "p"}, "IP_HASH_PEPPER"),                 # 31 UTF-8 bytes (30 + 1)
    ({"turnstile_required": False}, "TURNSTILE_REQUIRED"),
    ({"turnstile_secret_key": ""}, "TURNSTILE_SECRET_KEY"),
    ({"client_ip_header": ""}, "CLIENT_IP_HEADER"),
]


def test_a_production_boot_without_a_pepper_is_refused(clean_environment):
    with pytest.raises(ValidationError, match="IP_HASH_PEPPER"):
        production(ip_hash_pepper="")


def test_the_production_base_boots_and_each_refusal_names_its_own_setting_and_no_other(clean_environment):
    """The control for every pepper test below: the same settings with a valid pepper boot, and each way of being
    refused in this file names the one setting it changes. pydantic stops at the FIRST failing validator, so "no other
    setting is named" alone would hold for a pepper refusal that hid a second problem; it is the control (this base
    boots) that makes the pepper the only reason a pepper case fails, and the Turnstile or header cases (built on a
    valid pepper) the only reason theirs do."""
    assert production().is_production
    for changes, name in PRODUCTION_REFUSALS:
        with pytest.raises(ValidationError) as refused:
            production(**changes)
        named = [each for each in PRODUCTION_SETTING_NAMES if each in str(refused.value)]
        assert named == [name], f"{changes.keys()} named {named}, expected only {name}"


def test_a_production_pepper_of_31_bytes_is_refused_and_32_bytes_pass(clean_environment):
    with pytest.raises(ValidationError, match="IP_HASH_PEPPER"):
        production(ip_hash_pepper="p" * 31)
    assert production(ip_hash_pepper="p" * 32).is_production
    assert production(environment="Production", ip_hash_pepper="p" * 48).is_production


def test_the_pepper_length_is_counted_in_utf8_bytes(clean_environment):
    exactly_32 = production(ip_hash_pepper="é" * 16)                                    # 16 characters, 32 bytes
    assert exactly_32.ip_hash_pepper == "é" * 16
    with pytest.raises(ValidationError, match="IP_HASH_PEPPER"):
        production(ip_hash_pepper="é" * 15 + "p")                                      # 31 bytes


def test_the_refusal_never_prints_the_pepper_or_another_secret(clean_environment):
    """A pydantic error normally ends with the input it rejected (``input_value=...``): for a settings object that is
    the environment, shortened to its first and last characters, so the secret last in it would show. Any slice of a
    secret in the text is a leak. Checked for a refused pepper and for a refusal of anything else, which must not
    print the (valid) pepper either."""
    refusals = (({"ip_hash_pepper": ""}, "IP_HASH_PEPPER"), ({"ip_hash_pepper": SHORT_PEPPER}, "IP_HASH_PEPPER"),
                ({"turnstile_required": False}, "TURNSTILE_REQUIRED"))
    for changes, name in refusals:
        with pytest.raises(ValidationError) as refused:
            production(neo4j_password=DB_PASSWORD, admin_token=ADMIN_TOKEN, **changes)
        text = str(refused.value)
        assert name in text and "input_value" not in text
        for secret in (PEPPER, SHORT_PEPPER, TURNSTILE_SECRET, DB_PASSWORD, ADMIN_TOKEN):
            assert secret[:8] not in text and secret[-8:] not in text


def test_outside_production_no_pepper_or_a_short_one_is_fine(clean_environment):
    assert settings().ip_hash_pepper == ""
    assert settings(ip_hash_pepper="short").ip_hash_pepper == "short"
    assert not settings(environment="staging").is_production


def test_the_pepper_stays_out_of_the_repr(clean_environment):
    assert PEPPER not in repr(settings(ip_hash_pepper=PEPPER))


def test_the_pepper_version_defaults_to_2_and_cannot_be_below_1(clean_environment):
    assert settings().ip_hash_version == 2
    assert settings(ip_hash_version=1).ip_hash_version == 1
    for bad in (0, -1):                                               # 0 is the marker of a nulled legacy row
        with pytest.raises(ValidationError, match="ip_hash_version"):
            settings(ip_hash_version=bad)


def test_a_real_production_settings_object_hashes_through_the_helper_and_names_its_version(clean_environment):
    live = production(ip_hash_version=3)
    request = request_from("10.0.0.1", {"fly-client-ip": "198.51.100.7"})
    assert guard.hash_request_ip(request, live) == guard.ip_hash("198.51.100.7", PEPPER)
    assert guard.ip_hash_version_fields(live) == {"ip_hash_v": 3}


@pytest.mark.parametrize("raw", [" fly-client-ip", "Fly-Client-IP ", "\tFLY-CLIENT-IP\r\n"])
def test_a_header_name_with_stray_whitespace_or_case_still_names_the_visitor_not_the_proxy(clean_environment, raw):
    """The old validator stripped the name for its own comparison but stored it raw, and the guard looked the raw name up:
    no header matched, every visitor was keyed by the proxy's address, and the whole site shared one 20-per-day bucket."""
    live = production(client_ip_header=raw)
    one = guard.hash_request_ip(request_from("10.0.0.1", {"fly-client-ip": "198.51.100.7"}), live)
    two = guard.hash_request_ip(request_from("10.0.0.1", {"fly-client-ip": "203.0.113.50"}), live)
    assert (one, two) == (guard.ip_hash("198.51.100.7", PEPPER), guard.ip_hash("203.0.113.50", PEPPER))
    assert one != two != guard.ip_hash("10.0.0.1", PEPPER)


def test_the_pepper_and_its_version_are_read_from_the_environment(monkeypatch, clean_environment):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("TURNSTILE_REQUIRED", "true")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", TURNSTILE_SECRET)
    monkeypatch.setenv("CLIENT_IP_HEADER", "fly-client-ip")
    monkeypatch.setenv("IP_HASH_PEPPER", PEPPER)
    monkeypatch.setenv("IP_HASH_VERSION", "3")
    s = settings()
    assert (s.ip_hash_pepper, s.ip_hash_version, s.is_production) == (PEPPER, 3, True)
    monkeypatch.delenv("IP_HASH_PEPPER")
    with pytest.raises(ValidationError, match="IP_HASH_PEPPER"):         # the pepper is what boots it, not the rest
        settings()


# ---------------------------------------------------------------- every call site goes through hash_request_ip

def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    """Calls of ``name(...)`` or ``<anything>.name(...)``."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == name)
        or (isinstance(node.func, ast.Attribute) and node.func.attr == name))]


@pytest.mark.parametrize("module,expected", [("routes.py", 2), ("dossier_routes.py", 1), ("monitor_routes.py", 1),
                                             ("workspace_routes.py", 3)])
def test_every_ip_hash_call_site_calls_the_one_helper(module, expected):
    assert len(_calls_named(_parse(SERVE / module), "hash_request_ip")) == expected


def test_ip_hash_is_called_only_by_the_helper_in_guard():
    offenders = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
                 if path.name != "guard.py" and _calls_named(_parse(path), "ip_hash")]
    assert offenders == []
    inside = [fn.name for fn in ast.walk(_parse(SERVE / "guard.py")) if isinstance(fn, ast.FunctionDef)
              and _calls_named(fn, "ip_hash")]
    assert inside == ["hash_request_ip"]


@pytest.mark.parametrize("module,gate", [(routes, "_read_gate"), (dossier_routes, "_read_gate"),
                                         (monitor_routes, "_read_gate"), (workspace_routes, "_require_read_rate")])
def test_the_read_gates_key_their_window_by_the_peppered_hash(module, gate):
    keys: list[str] = []
    app = FastAPI()
    app.state.settings = cfg(client_ip_header="fly-client-ip")
    app.state.read_rate_limiter = SimpleNamespace(allow=lambda key: keys.append(key) or True)
    getattr(module, gate)(request_from("10.0.0.1", {"fly-client-ip": "198.51.100.7"}, app))
    assert keys == [guard.ip_hash("198.51.100.7", PEPPER)]


class PepperedRouteSettings(RouteSettings):
    client_ip_header = "fly-client-ip"
    ip_hash_pepper = PEPPER
    ip_hash_version = 3


def test_a_cached_answer_is_ledgered_under_the_peppered_hash_with_its_version(monkeypatch):
    """The cached hit's row is written by the route (``store.log_query``) with the hash the helper made and the
    settings' pepper version. The same ask through the real route app is also pinned in
    ``tests/test_serve_state_wiring.py`` (version 2 there); this one is about the pepper: the version 3 of the settings
    is the one stored, and the stored hash is the keyed one, never the legacy unsalted one."""
    rows: list[dict] = []
    monkeypatch.setattr(store, "log_query", lambda driver, **kw: rows.append(kw))
    backend = FakeStateBackend()
    backend.cache[store.cache_key(Q, "hybrid", "")] = {"answer": "cached", "citations": [], "hallucinated": [],
                                                      "source": "benchmark"}
    with TestClient(build_route_app(backend=backend, settings=PepperedRouteSettings())) as client:
        response = client.post("/api/ask", json={"question": Q}, headers={"fly-client-ip": "198.51.100.7"})
    assert response.status_code == 200 and '"cached": true' in response.text
    assert rows == [{"ip_hash": guard.ip_hash("198.51.100.7", PEPPER), "strategy": "hybrid", "cached": True,
                     "ip_hash_v": 3}]
    assert rows[0]["ip_hash"] != legacy_hash("198.51.100.7")


# ---------------------------------------------------------------- the paid ledger row

DONE = {"event": "done", "answer": "Nvidia depends on suppliers.", "citations": [], "hallucinated": [],
        "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.00007,
        "strategy": "hybrid", "question": Q}


def _paid_row(monkeypatch, **backend_settings) -> dict:
    """One ask streamed by a ``PaidStream`` that holds a lease of the REAL in-process backend (its ledger rows kept in
    memory), and the ledger row it left. ``ip_hash_version=None`` builds the backend from settings that do not have the
    attribute at all, as a double that predates the pepper cutover. The row is written by the backend when it takes the
    lease (``reserve``) from its own settings: the stream only settles it. The end-to-end route version of this is in
    ``tests/test_serve_state_wiring.py``; this keeps the pepper's own assertion."""
    monkeypatch.setattr(store, "get_policy", lambda driver, key: None)         # nothing stored: the kill level is off
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: None)
    backend_config = state_settings(**backend_settings)
    if backend_config.ip_hash_version is None:
        del backend_config.ip_hash_version
    ledger = InMemoryLedger()
    backend = make_backend(backend_config, StateDrivers(state=object()), ledger=ledger)
    backend.refresh_kill_level()                    # a level that was never read denies every ask

    async def twin(question, driver, embedder, **kw):
        yield DONE

    async def main():
        s = SimpleNamespace(llm_request_timeout_s=5, llm_answer_max_tokens=100, escalation_model="", embed_slots=1,
                            db_thread_limit=2, max_concurrent_answers=1)
        st = SimpleNamespace(settings=s, driver=object(), embedder=object(), limiters=make_limiters(s),
                             state=backend, tracer=None)
        lease = backend.reserve(ip_hash="iph", strategy="hybrid", workspace=False, estimate_micro=60_000,
                                now_wall=time.time(), now_mono=time.monotonic())
        assert isinstance(lease, Lease)
        drain.DRAIN.enter()                         # the route counts the ask on the drain before it reserves
        stream = PaidStream(st, Q, "hybrid", "iph", "snap-1", None, twin=twin, lease=lease)
        async for _ in stream.events():
            pass
        await stream.finalize()

    asyncio.run(main())
    row, = ledger.rows.values()
    return row


def test_the_paid_ledger_row_carries_the_pepper_version(monkeypatch):
    row = _paid_row(monkeypatch, ip_hash_version=3)
    assert row["ip_hash"] == "iph" and row["ip_hash_v"] == 3
    assert (row["status"], row["outcome"]) == ("settled", "done")


def test_settings_without_a_version_leave_the_paid_row_as_it_was(monkeypatch):
    # Neo4j does not store a null property, so this row looks like one written before the pepper existed.
    row = _paid_row(monkeypatch, ip_hash_version=None)
    assert row["ip_hash"] == "iph" and row["ip_hash_v"] is None


# ---------------------------------------------------------------- store: log_query, the count and the null

class FakeSession:
    def __init__(self, driver, config):
        self._driver, self._config = driver, config

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):          # the only entry point: no execute_write, no explicit transaction
        self._driver.calls.append((query, params, self._config))
        return iter(self._driver.answer(query, params))


class FakeDriver:
    def __init__(self, answer=lambda query, params: []):
        self.calls, self.answer, self.closed = [], answer, False

    def session(self, **config):
        return FakeSession(self, config)

    def close(self):
        self.closed = True


class LedgerFake(FakeDriver):
    """A ledger with ``legacy`` rows that have an ip_hash and no ip_hash_v. ``stubborn`` rows survive the null."""

    def __init__(self, legacy: int, stubborn: int = 0):
        super().__init__(self._answer)
        self.legacy, self.stubborn = legacy, stubborn

    def _answer(self, query, params):
        if "RETURN count" in query:
            return [{"n": self.legacy}]
        assert "IN TRANSACTIONS" in query, query
        self.legacy = self.stubborn
        return []

    def statements(self) -> list[str]:
        return ["count" if "RETURN count" in q else "null" for q, _, _ in self.calls]


def test_log_query_writes_ip_hash_v_only_when_it_is_given():
    plain, versioned = FakeDriver(), FakeDriver()
    store.log_query(plain, ip_hash="h", strategy="hybrid", cached=False)
    store.log_query(versioned, ip_hash="h", strategy="hybrid", cached=True, ip_hash_v=2)
    query, params, _ = plain.calls[0]
    assert "ip_hash_v" not in query and "ipv" not in params
    query, params, _ = versioned.calls[0]
    assert re.search(r"SET q\.ip_hash_v = \$ipv\b", query) and params["ipv"] == 2 and params["ip"] == "h"


def test_the_legacy_count_asks_for_rows_with_an_ip_hash_and_no_version():
    drv = FakeDriver(lambda query, params: [{"n": 7}])
    assert store.count_legacy_ip_hashes(drv) == 7
    query, _, config = drv.calls[0]
    assert re.search(r"q\.ip_hash IS NOT NULL AND q\.ip_hash_v IS NULL", query) and "RETURN count(q)" in query
    assert config == dict(NO_UNRECOGNIZED_NOTIFICATIONS)       # ip_hash_v may not exist in the database yet


def test_the_null_touches_only_legacy_rows_in_batches_and_returns_how_many_it_nulled():
    drv = LedgerFake(LEGACY_ROW_COUNT)
    assert store.null_legacy_ip_hashes(drv) == LEGACY_ROW_COUNT
    assert drv.statements() == ["count", "null", "count"]
    null = drv.calls[1][0]
    assert re.search(r"MATCH \(q:SvcQuery\) WHERE q\.ip_hash IS NOT NULL AND q\.ip_hash_v IS NULL", null)
    assert re.search(r"CALL \(q\) \{\s*SET q\.ip_hash = null, q\.ip_hash_v = 0\s*\} IN TRANSACTIONS OF 5000 ROWS", null)
    assert drv.calls[1][2] == dict(NO_UNRECOGNIZED_NOTIFICATIONS)


def test_the_null_batch_size_is_the_callers():
    drv = LedgerFake(3)
    store.null_legacy_ip_hashes(drv, batch=250)
    assert "IN TRANSACTIONS OF 250 ROWS" in drv.calls[1][0]


def test_running_the_null_again_nulls_nothing_and_writes_nothing():
    drv = LedgerFake(LEGACY_ROW_COUNT)
    store.null_legacy_ip_hashes(drv)
    writes_before = drv.statements().count("null")
    assert store.null_legacy_ip_hashes(drv) == 0
    assert drv.statements().count("null") == writes_before


def test_the_null_reports_only_what_it_nulled_when_rows_remain():
    drv = LedgerFake(10, stubborn=4)
    assert store.null_legacy_ip_hashes(drv) == 6


@pytest.mark.parametrize("batch", [0, -5, True, 2.5, "10", None])
def test_a_batch_that_is_not_a_positive_integer_is_refused_before_the_database_is_touched(batch):
    drv = LedgerFake(3)
    with pytest.raises(ValueError, match="batch"):
        store.null_legacy_ip_hashes(drv, batch=batch)
    assert drv.calls == []


# ---------------------------------------------------------------- scripts/null_legacy_ip_hashes.py

def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def null_cli():
    return load_script("null_legacy_ip_hashes")


def test_without_yes_the_cli_only_counts_and_says_the_null_is_irreversible(null_cli, capsys):
    drv = LedgerFake(LEGACY_ROW_COUNT)
    assert null_cli.main([], connect=lambda: drv) == 0
    out = capsys.readouterr().out
    assert str(LEGACY_ROW_COUNT) in out and "IRREVERSIBLE" in out and "--yes" in out
    assert drv.statements() == ["count"] and drv.legacy == LEGACY_ROW_COUNT       # nothing written
    assert drv.closed


def test_with_yes_the_cli_runs_the_null_and_reports_the_counts(null_cli, capsys):
    drv = LedgerFake(LEGACY_ROW_COUNT)
    assert null_cli.main(["--yes"], connect=lambda: drv) == 0
    out = capsys.readouterr().out
    assert drv.legacy == 0 and drv.statements().count("null") == 1
    assert f"nulled: {LEGACY_ROW_COUNT}" in out and "remaining: 0" in out and "IRREVERSIBLE" in out
    assert drv.closed


def test_the_cli_passes_the_batch_size_through(null_cli):
    drv = LedgerFake(3)
    assert null_cli.main(["--yes", "--batch", "250"], connect=lambda: drv) == 0
    assert any("IN TRANSACTIONS OF 250 ROWS" in query for query, _, _ in drv.calls)


URI_WITH_PASSWORD = "bolt://neo4j:hunter2-in-a-uri@semigraph-neo4j.internal:7687"       # gitleaks:allow


@pytest.mark.parametrize("uri,database,shown", [
    (URI_WITH_PASSWORD, "", "semigraph-neo4j.internal:7687, database <server default>"),
    ("neo4j+s://db.example.test", "graph1", "db.example.test, database graph1"),
    ("not a uri", "", "unknown host, database <server default>"),
])
def test_the_default_connection_says_where_the_null_would_act_and_never_shows_the_user_info(
        null_cli, monkeypatch, capsys, uri, database, shown):
    """The one thing to check before ``--yes``: which database it is pointed at. The URI may carry a password."""
    import semigraph.config
    import semigraph.graph.client

    drv = LedgerFake(LEGACY_ROW_COUNT)
    monkeypatch.setattr(semigraph.config, "get_settings",
                        lambda: SimpleNamespace(neo4j_uri=uri, neo4j_database=database))
    monkeypatch.setattr(semigraph.graph.client, "get_driver", lambda settings: drv)
    assert null_cli.main([]) == 0
    out = capsys.readouterr().out
    assert f"target: {shown}" in out
    assert "hunter2" not in out and "@" not in out
    assert drv.statements() == ["count"] and drv.closed


@pytest.mark.parametrize("bad", ["0", "-1", "many"])
def test_a_bad_batch_size_is_a_usage_error(null_cli, bad):
    with pytest.raises(SystemExit) as stop:
        null_cli.main(["--batch", bad], connect=lambda: pytest.fail("connected for a usage error"))
    assert stop.value.code == 2


def test_the_help_says_the_null_is_irreversible(null_cli, capsys):
    with pytest.raises(SystemExit) as stop:
        null_cli.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "IRREVERSIBLE" in out and "--yes" in out


def test_rows_left_after_the_null_are_a_failure(null_cli, capsys):
    drv = LedgerFake(10, stubborn=2)
    assert null_cli.main(["--yes"], connect=lambda: drv) == 1
    assert "remaining: 2" in capsys.readouterr().out


def test_a_failure_prints_the_class_of_the_error_and_never_its_text(null_cli, capsys):
    secret_uri = "bolt://neo4j:hunter2-in-a-uri@db.internal:7687"       # gitleaks:allow

    def refuse():
        raise RuntimeError(f"cannot reach {secret_uri}")

    assert null_cli.main(["--yes"], connect=refuse) == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "hunter2-in-a-uri" not in captured.out + captured.err

    broken = FakeDriver(lambda query, params: (_ for _ in ()).throw(ValueError(f"auth failed for {secret_uri}")))
    assert null_cli.main(["--yes"], connect=lambda: broken) == 1
    captured = capsys.readouterr()
    assert "ValueError" in captured.err and "hunter2-in-a-uri" not in captured.out + captured.err
    assert broken.closed


# ---------------------------------------------------------------- scripts/push_fly_secrets.py

@pytest.fixture(scope="module")
def push_script():
    return load_script("push_fly_secrets")


def test_the_pepper_and_its_version_are_pushed_to_the_api_app_only(push_script):
    keys = push_script.FLY_KEYS
    assert "IP_HASH_PEPPER" in keys["semigraph"] and "IP_HASH_VERSION" in keys["semigraph"]
    assert not any(k.startswith("IP_HASH") for k in keys["semigraph-neo4j"])


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env.fly"
    lines = [f"IP_HASH_PEPPER={PEPPER}", "IP_HASH_VERSION=3", f"ADMIN_TOKEN={ADMIN_TOKEN}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_dry_run_names_the_pepper_and_never_prints_it(push_script, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["push_fly_secrets", "--env", str(_env_file(tmp_path)), "--dry-run"])
    push_script.main()
    out = capsys.readouterr().out
    assert "IP_HASH_PEPPER" in out and "dry run" in out
    assert PEPPER not in out and ADMIN_TOKEN not in out


def test_the_push_sends_the_pepper_on_stdin_only_and_honours_stage(push_script, tmp_path, monkeypatch, capsys):
    sent = {}

    def fake_run(cmd, input=None, **kw):
        sent.update(cmd=cmd, input=input)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(push_script.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["push_fly_secrets", "--env", str(_env_file(tmp_path)), "--stage",
                                      "--flyctl", "flyctl", "--only", "IP_HASH_PEPPER,IP_HASH_VERSION"])
    with pytest.raises(SystemExit) as stop:
        push_script.main()
    assert stop.value.code == 0
    assert f"IP_HASH_PEPPER={PEPPER}\n" in sent["input"] and "IP_HASH_VERSION=3\n" in sent["input"]
    assert "ADMIN_TOKEN" not in sent["input"]                    # --only is a subset
    assert "--stage" in sent["cmd"] and PEPPER not in " ".join(sent["cmd"])
    assert PEPPER not in capsys.readouterr().out

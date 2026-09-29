"""M4: the SEC identity reaches production as a Fly secret, never through the public fly.toml (docs/v2/M4_PLAN.md D4)."""

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fly_keys() -> dict:
    spec = importlib.util.spec_from_file_location("push_fly_secrets", ROOT / "scripts" / "push_fly_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FLY_KEYS


def test_the_sec_identity_is_pushed_as_an_api_secret():
    assert "SEC_USER_AGENT" in _fly_keys()["semigraph"]


def test_the_public_fly_toml_never_carries_the_sec_identity():
    env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8")).get("env", {})
    assert "SEC_USER_AGENT" not in env

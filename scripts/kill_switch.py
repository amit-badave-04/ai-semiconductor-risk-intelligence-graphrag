"""Emergency stop for paid answers: read or set the three-level kill switch.

    python -m scripts.kill_switch get|status|on|retrieval_only|off [--env .env.fly]      (through the admin endpoint)
    python -m scripts.kill_switch get|status|on|retrieval_only|off --direct [--env FILE]  (straight to the Neo4j policy
    node)

``on``             paid questions answer 503; cached and benchmark answers keep working, the page stays up.
``retrieval_only`` paid questions are off as well, until the page offers the retrieval-only view (503 with its own
copy).
``off``            paid questions are accepted again (subject to the daily caps and the rate limit).
``get``            prints the level (``status`` is an alias of it; over the admin endpoint it also prints the spend
ledger).

A pre-M5 image treats ``retrieval_only`` as ``off`` (it only knows ``on`` as stopped): set ``on`` or ``off`` before
rolling back to one (docs/v2/M5A_BUILD_PLAN.md section 1, I4 rollback). The image that is live today is such an image,
so ``--direct`` refuses to STORE any level it does not understand (``retrieval_only``) unless
``--i-know-the-live-image-treats-it-as-off`` is given: a database that image reads would then let paid questions
through while the operator believes they are paused. Reading a level never needs the flag.

Two ways in. The default talks to the running app's admin endpoint, which is what ``scripts/ops.ps1`` and the runbook
use, because Neo4j is private on Fly and a laptop cannot reach it: it reads APP_BASE_URL and ADMIN_TOKEN from the env
file (default .env.fly) unless they are already set in the environment, and a stopped machine is woken by the first call
(~10 s). ``--direct`` is for a local or staging database: it builds the app's Settings (the repo's ``.env``, or ``--env
FILE``, plus the environment) and reads or writes the ``SvcPolicy`` ``kill_switch`` node itself, so it works with the
app stopped. Neither mode prints a secret.

Over HTTP ``on`` and ``off`` send the boolean the pre-M5 endpoint takes; ``retrieval_only`` sends the level name and
needs the endpoint widened by the M5a wiring (until then the endpoint answers 422 and nothing changes).
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

POLICY_KEY = "kill_switch"
LEVELS = ("on", "retrieval_only", "off")
READ_COMMANDS = ("get", "status")
DEFAULT_ENV_FILE = ".env.fly"
HTTP_TIMEOUT_S = 60
LIVE_IMAGE_LEVELS = ("on", "off")      # the levels the image that is live today understands
KNOW_FLAG = "--i-know-the-live-image-treats-it-as-off"
EFFECT = {"on": "live questions declined (503)", "retrieval_only": "live questions declined (503), retrieval only",
          "off": "live questions accepted"}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read or set the paid-answer kill switch.")
    parser.add_argument("command", choices=[*READ_COMMANDS, *LEVELS], nargs="?", default="status")
    parser.add_argument("--direct", action="store_true",
                        help="talk to the Neo4j policy node instead of the admin endpoint")
    parser.add_argument(KNOW_FLAG, dest="know_live_image", action="store_true",
                        help="--direct only: store a level the live image does not understand (retrieval_only)")
    parser.add_argument("--env", default=None,
                        help=f"env file (admin endpoint: default {DEFAULT_ENV_FILE}, needs APP_BASE_URL + "
                             "ADMIN_TOKEN; --direct: Settings, default the repo .env)")
    return parser.parse_args(argv)


def level_of(value: object) -> str:
    """A level name from what the endpoint or the policy node holds: a bool (the pre-M5 endpoint) or a name."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value) if value else "off"


# ---- directly through the policy node ---------------------------------------------------------------------------

def open_driver(args: argparse.Namespace):
    """The app's own driver (Settings: the repo .env, or --env FILE, plus the environment)."""
    from semigraph.config import Settings, get_settings
    from semigraph.graph.client import get_driver

    settings = Settings(_env_file=args.env) if args.env else get_settings()
    try:
        return get_driver(settings)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")


def refuse_a_level_the_live_image_misreads(args: argparse.Namespace) -> None:
    """Exit before any connection when ``args`` would store a level that the live image reads as ``off``."""
    if args.command in LEVELS and args.command not in LIVE_IMAGE_LEVELS and not args.know_live_image:
        sys.exit(f"ERROR: refusing to store {args.command!r} directly: the image that is live today reads only 'on' as "
                 f"stopped, so it treats {args.command!r} as 'off' and would accept paid questions. Pass {KNOW_FLAG} "
                 "if the database is read by no such image.")


def run_direct(args: argparse.Namespace) -> int:
    from semigraph.serve import store

    refuse_a_level_the_live_image_misreads(args)
    driver = open_driver(args)
    try:
        if args.command in LEVELS:
            store.set_policy(driver, POLICY_KEY, args.command)
        print(f"kill switch: {level_of(store.get_policy(driver, POLICY_KEY))}")
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()
    return 0


# ---- through the admin endpoint (what ops.ps1 uses) -------------------------------------------------------------

def run_http(args: argparse.Namespace) -> int:
    env = load_env(Path(args.env or DEFAULT_ENV_FILE))
    base = os.environ.get("APP_BASE_URL") or env.get("APP_BASE_URL")
    token = os.environ.get("ADMIN_TOKEN") or env.get("ADMIN_TOKEN")
    if not base or not token:
        sys.exit("ERROR: APP_BASE_URL and ADMIN_TOKEN are required (env or --env file)")
    client = httpx.Client(base_url=base.rstrip("/"), headers={"X-Admin-Token": token}, timeout=HTTP_TIMEOUT_S)

    if args.command in LEVELS:
        body = {"kill_switch": args.command == "on"} if args.command in ("on", "off") else {"kill_switch": args.command}
        response = client.post("/api/admin/policy", json=body)
        response.raise_for_status()
        state = level_of(response.json()["kill_switch"])
        print(f"KILL SWITCH {state.upper()}: {EFFECT.get(state, state)}; cached answers unaffected.")
    response = client.get("/api/admin/policy")
    if response.status_code == 404:
        sys.exit("ERROR: admin endpoint rejected the token (or ADMIN_TOKEN is not set on the app)")
    response.raise_for_status()
    payload = response.json()
    print(f"kill switch: {level_of(payload['kill_switch'])}")
    print(f"ledger: {payload['ledger']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_direct(args) if args.direct else run_http(args)


if __name__ == "__main__":
    sys.exit(main())

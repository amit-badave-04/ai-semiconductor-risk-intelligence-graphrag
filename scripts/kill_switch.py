"""Emergency stop for paid answers — flips the persisted kill switch through the
admin endpoint (no direct database access needed; Neo4j is private on Fly).

    python -m scripts.kill_switch on|off|status [--env .env.fly]

"on"  -> POST /api/ask returns 503 for live questions; cached/benchmark answers
         keep working, the page stays up. The flag lives in Neo4j, so it survives
         restarts and auto-stops.
"off" -> live questions accepted again (subject to the daily ceiling + rate limit).
"status" prints the flag and the spend ledger.

Reads APP_BASE_URL and ADMIN_TOKEN from the env file (default .env.fly) unless
they are already set in the environment. Note: if the API machine is stopped by
autoscaling, the first call wakes it (~10 s).
"""

import argparse
import os
import sys
from pathlib import Path

import httpx


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["on", "off", "status"], nargs="?", default="status")
    ap.add_argument("--env", default=".env.fly", help="env file with APP_BASE_URL + ADMIN_TOKEN")
    args = ap.parse_args()

    env = load_env(Path(args.env))
    base = os.environ.get("APP_BASE_URL") or env.get("APP_BASE_URL")
    token = os.environ.get("ADMIN_TOKEN") or env.get("ADMIN_TOKEN")
    if not base or not token:
        sys.exit("ERROR: APP_BASE_URL and ADMIN_TOKEN are required (env or --env file)")
    client = httpx.Client(base_url=base.rstrip("/"), headers={"X-Admin-Token": token}, timeout=60)

    if args.command in ("on", "off"):
        r = client.post("/api/admin/policy", json={"kill_switch": args.command == "on"})
        r.raise_for_status()
        state = r.json()["kill_switch"]
        print(f"KILL SWITCH {state.upper()}: live questions "
              f"{'declined (503)' if state == 'on' else 'accepted'}; cached answers unaffected.")
    r = client.get("/api/admin/policy")
    if r.status_code == 404:
        sys.exit("ERROR: admin endpoint rejected the token (or ADMIN_TOKEN is not set on the app)")
    r.raise_for_status()
    body = r.json()
    print(f"kill switch: {body['kill_switch']}")
    print(f"ledger: {body['ledger']}")


if __name__ == "__main__":
    main()

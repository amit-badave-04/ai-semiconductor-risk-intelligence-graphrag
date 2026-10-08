"""The window table and the quote arithmetic of ``scripts/staging.py`` (M5a I5), split out to keep each file cohesive.

``deploy/staging/windows.json`` holds the constants (30-day prices, machines, hours, quote, cap); this module turns them into
hourly rates, a derived cost, the quote row printed before any ``flyctl`` call, and the machine class flags. Stdlib only, no
``flyctl``. The section 2 quotes are rounded up with margin, so they are stored, not computed; the arithmetic column is computed
and a test proves derived <= quote <= cap for every window.
"""

import json
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIVE_APP_NAMES = frozenset({"semigraph", "semigraph-neo4j"})


class UsageError(Exception):
    """The command line or the state is wrong: nothing was run."""


def load_windows(path: Path | None = None) -> dict:
    doc = json.loads(Path(path or REPO / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))
    names = [app["name"] for app in doc["apps"].values()]
    if len(set(names)) != len(names) or set(names) & LIVE_APP_NAMES:
        raise ValueError("windows.json must list distinct staging apps and no live app")
    for key, window in doc["windows"].items():
        for machine in [*window["machines"], *(m for o in window.get("options", ()) for m in o.get("extra_machines", ()))]:
            if machine["class"] not in doc["rates_30_day_usd"] or machine["app"] not in doc["apps"]:
                raise ValueError(f"{key}: unknown class or app in {machine}")
    return doc


def hourly_rate(windows: dict, klass: str) -> float:
    return windows["rates_30_day_usd"][klass] / windows["hours_per_month"]


def _option(window: dict, name: str | None) -> dict | None:
    if not name:
        return None
    found = [o for o in window.get("options", ()) if name.lower() in o["name"].lower()]
    if len(found) != 1:
        raise UsageError(f"option {name!r} matches {len(found)} options ({[o['name'] for o in window.get('options', ())]})")
    return found[0]


def _terms(windows: dict, key: str, option: str | None) -> list[tuple[str, float]]:
    window = windows["windows"][key]
    opt = _option(window, option)
    hours = opt.get("hours") if opt else None
    terms = [(f"{m['count'] * (hours or m['hours']):g} h x ${hourly_rate(windows, m['class']):.4f}",
              m["count"] * (hours or m["hours"]) * hourly_rate(windows, m["class"])) for m in window["machines"]]
    for m in (opt or {}).get("extra_machines", ()):
        terms.append((f"{m['count'] * m['hours']:g} h x ${hourly_rate(windows, m['class']):.4f}",
                      m["count"] * m["hours"] * hourly_rate(windows, m["class"])))
    per_gb_hour = windows["volume_usd_per_gb_30_day"] / windows["hours_per_month"]
    volume = sum(v["gb"] * (hours or v["hours"]) * per_gb_hour for v in window.get("volume_gb_hours", ()))
    if volume:
        terms.append((f"volume ${volume:.4f}", volume))
    if window.get("egress_gb_max"):
        egress = window["egress_gb_max"] * windows["egress_usd_per_gb"]
        terms.append((f"egress <= ${egress:.4f}", egress))
    return terms


def derived_usd(windows: dict, key: str, option: str | None = None) -> float:
    return sum(value for _, value in _terms(windows, key, option))


def quote_usd(windows: dict, key: str, option: str | None = None) -> tuple[float, float]:
    window = windows["windows"][key]
    opt = _option(window, option)
    if opt is None:
        return window["quote_usd"], window["cap_usd"]
    if "extra_quote_usd" in opt:
        return window["quote_usd"] + opt["extra_quote_usd"], window["cap_usd"] + opt["extra_quote_usd"]
    return opt["quote_usd"], opt["cap_usd"]


def _machine_text(window: dict, opt: dict | None) -> str:
    """The machines a row prices: the window's plus the option's extra ones, so the column agrees with the arithmetic and the
    quote beside it. Entries of the same app, class and hours are one count ('5 x', not '3 x' plus an unlisted 2)."""
    counts: dict[tuple, int] = {}
    for m in [*window["machines"], *(opt or {}).get("extra_machines", ())]:
        key = (m["app"], m["class"], m["hours"])
        counts[key] = counts.get(key, 0) + m["count"]
    return " + ".join(f"{count} x {klass.replace(':', ' ')} ({app})" for (app, klass, _), count in counts.items()) or "none"


def _duration_text(window: dict, opt: dict | None) -> str:
    """The window's phases, unless the option is a part of them: its own ``duration`` text if the file has one, else the
    hours it is priced at (the arithmetic's number), never the window's full list."""
    if opt and opt.get("duration"):
        return opt["duration"]
    if opt and opt.get("hours"):
        return f"{opt['hours']:g} h (this option's hours, not the whole window's)"
    return window["duration"]


def _row(windows: dict, key: str, option: str | None, label: str) -> str:
    window = windows["windows"][key]
    opt = _option(window, option)
    quote, cap = quote_usd(windows, key, option)
    machines = _machine_text(window, opt)
    if option:
        machines += " [" + option + "]"
    arithmetic = " + ".join(text for text, _ in _terms(windows, key, option)) or "provider spend only"
    money = f"${quote:.2f} (${cap:.2f})"
    if window.get("provider_cap_usd"):
        arithmetic += f"; --max-usd {window['provider_cap_usd']:.2f}"
        money += f" + provider <= ${window['provider_cap_usd']:.2f}"
    return f"{label} | {machines} | {_duration_text(window, opt)} | {arithmetic} | {money}"


def quote_lines(windows: dict, keys: Sequence[str], option: str | None = None) -> list[str]:
    lines = ["Window | Machines | Duration | Arithmetic (30-day price / 720 x hours) | Quote (cap)"]
    for key in keys:
        window = windows["windows"][key]
        lines.append(_row(windows, key, option if option and has_option(window, option) else None, f"{key} {window['title']}"))
        for opt in window.get("options", ()):
            if not option or opt["name"].lower() != option.lower():
                lines.append("  " + _row(windows, key, opt["name"], f"option {opt['name']}"))
    return lines


def has_option(window: dict, name: str) -> bool:
    return any(name.lower() in o["name"].lower() for o in window.get("options", ()))


def parse_class(klass: str) -> tuple[str, int]:
    size, memory = klass.split(":")
    return size, int(memory.removesuffix("gb")) * 1024

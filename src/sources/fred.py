"""Macro series and the economic release calendar — FRED.

Needs FRED_API_KEY (free, unlimited).

WHY THERE IS NO ``consensus`` FIELD
-----------------------------------
Finnhub's ``/calendar/economic`` is Enterprise-gated (their docs payload says
``"freeTier": null``), and consensus/expectation figures have no free source at
all — they are licensed data. Rather than substitute a paid tier or let the
model supply the number, ``calendar.economic`` carries date, time, event,
importance and a *prior* sourced from FRED. The consensus key is omitted
entirely rather than set to null: a null field tells the model a consensus
exists and it merely lacks it, which is exactly the condition that produces a
confident invented number. This follows the same reasoning SPEC.md §1 used to
cut options flow and FedWatch probabilities.

``time_et`` and ``importance`` come from the curated table below. Release times
are schedule metadata ("CPI drops at 8:30am ET"), not market figures, and are
not exposed by any free API.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from .common import SourceError, get_json, num, pct

log = logging.getLogger(__name__)

NAME = "fred"
IMPACT = "macro series and economic calendar unavailable"

BASE = "https://api.stlouisfed.org/fred"
FORWARD_DAYS = 14

# Series from SPEC.md Appendix A.
#   units: FRED-computed transform, so we never hand-derive a rate.
#     lin = as published, pc1 = % change from year ago, chg = change from prior,
#     pca = compounded annual rate.
SERIES: list[dict] = [
    {"label": "CPI YoY",              "id": "CPIAUCSL", "units": "pc1", "fmt": "pct1"},
    {"label": "Core CPI YoY",         "id": "CPILFESL", "units": "pc1", "fmt": "pct1"},
    {"label": "Core PCE YoY",         "id": "PCEPILFE", "units": "pc1", "fmt": "pct1"},
    {"label": "Unemployment rate",    "id": "UNRATE",   "units": "lin", "fmt": "pct1"},
    {"label": "Nonfarm payrolls m/m", "id": "PAYEMS",   "units": "chg", "fmt": "thousands"},
    {"label": "Initial jobless claims", "id": "ICSA",   "units": "lin", "fmt": "count"},
    {"label": "Effective fed funds rate", "id": "DFF",  "units": "lin", "fmt": "pct2"},
    {"label": "Real GDP QoQ SAAR",    "id": "GDPC1",    "units": "pca", "fmt": "pct1"},
]

# Releases worth putting on the calendar, with their scheduled ET time and how
# much they can move the tape. Anything not matched here is dropped — SPEC.md
# §3 section 4 says skip the macro section rather than pad it.
#
# Checked before CURATED_RELEASES. FRED publishes research and state-level
# variants whose names match the headline patterns ("Research Consumer Price
# Index", "State Unemployment Insurance Weekly Claims Report") but which do not
# move the tape.
RELEASE_EXCLUSIONS = (
    r"research|chained|experimental|state unemployment|r-cpi"
    r"|debt to|ratios|median|trimmed"
)

CURATED_RELEASES: list[tuple[str, str, str]] = [
    (r"consumer price index",              "08:30", "high"),
    (r"employment situation",              "08:30", "high"),
    (r"personal income and outlays",       "08:30", "high"),
    (r"gross domestic product",            "08:30", "high"),
    (r"advance monthly sales for retail",  "08:30", "high"),
    (r"producer price index",              "08:30", "medium"),
    (r"unemployment insurance weekly claims", "08:30", "medium"),
    (r"job openings and labor turnover",   "10:00", "medium"),
    (r"industrial production",             "09:15", "medium"),
    (r"new residential construction",      "08:30", "medium"),
    (r"advance report on durable goods",   "08:30", "medium"),
    (r"existing home sales",               "10:00", "low"),
    (r"consumer confidence|surveys of consumers", "10:00", "medium"),
]


def fetch(ctx) -> dict:
    """Return ``macro_latest`` plus the ``economic`` calendar block."""
    if not ctx.fred_key:
        raise SourceError("FRED_API_KEY not set", IMPACT)

    # Resolved once and threaded through: it is one call serving both blocks.
    upcoming = _upcoming_release_dates(ctx)

    macro = _macro_latest(ctx, upcoming)
    economic = _economic_calendar(macro, upcoming)

    # _release_id is internal plumbing for matching a release to its series;
    # it must not reach market_data.json.
    for entry in macro:
        entry.pop("_release_id", None)

    return {"macro_latest": macro, "economic": economic}


# --------------------------------------------------------------------------
# macro_latest
# --------------------------------------------------------------------------

def _macro_latest(ctx, upcoming: dict[int, dict]) -> list[dict]:
    out = []

    for spec in SERIES:
        try:
            observations = _observations(ctx, spec["id"], spec["units"])
        except SourceError:
            continue  # one dead series must not cost the whole macro block

        usable = [o for o in observations if o["value"] != "."]
        if not usable:
            continue

        latest = usable[0]
        entry = {
            "series": spec["label"],
            "fred_id": spec["id"],
            "latest": _format(float(latest["value"]), spec["fmt"]),
            "as_of": latest["date"],
        }
        if len(usable) > 1:
            entry["prior"] = _format(float(usable[1]["value"]), spec["fmt"])

        release_id = _release_id(ctx, spec["id"])
        if release_id is not None:
            entry["_release_id"] = release_id
            if release_id in upcoming:
                entry["next_release"] = upcoming[release_id]["date"]
        out.append(entry)

    if not out:
        raise SourceError("no FRED series returned usable observations", IMPACT)
    return out


def _observations(ctx, series_id: str, units: str) -> list[dict]:
    data = _call(ctx, "series/observations", {
        "series_id": series_id,
        "units": units,
        "sort_order": "desc",
        "limit": 6,
    })
    return data.get("observations", [])


def _release_id(ctx, series_id: str) -> int | None:
    try:
        data = _call(ctx, "series/release", {"series_id": series_id})
    except SourceError:
        return None
    releases = data.get("releases") or []
    return releases[0].get("id") if releases else None


# --------------------------------------------------------------------------
# economic calendar
# --------------------------------------------------------------------------

def _upcoming_release_dates(ctx) -> dict[int, dict]:
    """Earliest scheduled date per release over the forward window."""
    horizon = ctx.trading_day + dt.timedelta(days=FORWARD_DAYS)
    try:
        data = _call(ctx, "releases/dates", {
            "realtime_start": ctx.trading_day.isoformat(),
            "realtime_end": horizon.isoformat(),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
            "limit": 1000,
        })
    except SourceError as exc:
        # Not fatal — macro_latest still stands — but never silent: without this
        # the economic calendar comes back empty with no trace of why.
        log.warning("FRED releases/dates failed, economic calendar will be empty: %s", exc)
        return {}

    earliest: dict[int, dict] = {}
    for row in data.get("release_dates", []):
        release_id, date = row.get("release_id"), row.get("date")
        if release_id is None or not date:
            continue
        if release_id not in earliest or date < earliest[release_id]["date"]:
            earliest[release_id] = {"date": date, "name": row.get("release_name", "")}
    return earliest


def _economic_calendar(macro: list[dict], upcoming: dict[int, dict]) -> list[dict]:
    """Curated upcoming releases. No consensus field — see module docstring."""
    # setdefault, not assignment: CPIAUCSL and CPILFESL share release 10, and
    # the headline series (first in SERIES) is the right prior for "Consumer
    # Price Index". Overwriting would label it with Core CPI's number.
    priors: dict[int, dict] = {}
    for entry in macro:
        if "_release_id" in entry:
            priors.setdefault(entry["_release_id"], entry)

    out = []
    for release_id, info in upcoming.items():
        name = info.get("name") or ""
        curated = _curate(name)
        if curated is None:
            continue
        time_et, importance = curated

        entry = {
            "date": info["date"],
            "time_et": time_et,
            "event": name,
            "importance": importance,
        }
        # The last published value of the matching series *is* the prior.
        source_series = priors.get(release_id)
        if source_series is not None:
            entry["prior"] = source_series["latest"]
            entry["prior_series"] = source_series["series"]
        out.append(entry)

    out.sort(key=lambda e: (e["date"], e["time_et"]))
    return out


def _curate(name: str) -> tuple[str, str] | None:
    lowered = name.lower()
    if re.search(RELEASE_EXCLUSIONS, lowered):
        return None
    for pattern, time_et, importance in CURATED_RELEASES:
        if re.search(pattern, lowered):
            return time_et, importance
    return None


# --------------------------------------------------------------------------

def _format(value: float, fmt: str) -> dict:
    if fmt == "pct1":
        return pct(value, 1, signed=False)
    if fmt == "pct2":
        return pct(value, 2, signed=False)
    if fmt == "thousands":
        # PAYEMS is in thousands of persons; speak it as jobs.
        jobs = value * 1000
        sign = "+" if jobs > 0 else ""
        return num(jobs, f"{sign}{jobs:,.0f}")
    if fmt == "count":
        return num(value, f"{value:,.0f}")
    raise SourceError(f"unknown format {fmt!r}")


def _call(ctx, path: str, params: dict) -> dict:
    return get_json(
        f"{BASE}/{path}",
        params={**params, "api_key": ctx.fred_key, "file_type": "json"},
        impact=IMPACT,
    )

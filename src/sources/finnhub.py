"""Earnings calendar — Finnhub.

Needs FINNHUB_API_KEY (free tier).

Tier confirmed in Phase 1 from Finnhub's own docs payload:
    /calendar/earnings   "freeTier": "1 month of historical earnings and new updates"
    /calendar/economic   "freeTier": null   <- Enterprise-gated, not used here

A live 403-vs-200 check was not possible because Finnhub validates the token
before the tier (a bogus key returns 401 on every endpoint, including free
ones), so the free/premium split above rests on their published metadata.

Two fields SPEC.md §2 wants are absent from the response: company ``name`` and
``importance``. Both are resolved from ``universe.py`` rather than by extra API
calls, which also bounds the result set to names that can actually move the tape.

Response fields: date, symbol, epsEstimate, epsActual, revenueEstimate,
revenueActual, hour ("bmo"/"amc"/"dmh"), quarter, year.
"""

from __future__ import annotations

import datetime as dt

from .common import SourceError, get_json, price
from .universe import UNIVERSE, importance_for, name_for

NAME = "finnhub_earnings_calendar"
IMPACT = "earnings calendar unavailable"

URL = "https://finnhub.io/api/v1/calendar/earnings"
FORWARD_DAYS = 14

# Finnhub's "hour" -> the schema's "session".
SESSIONS = {
    "bmo": "before_open",
    "amc": "after_close",
    "dmh": "during_market_hours",
}


def fetch(ctx) -> dict:
    """Return ``{"earnings": [...]}`` for the calendar block."""
    if not ctx.finnhub_key:
        raise SourceError("FINNHUB_API_KEY not set", IMPACT)

    horizon = ctx.trading_day + dt.timedelta(days=FORWARD_DAYS)
    data = get_json(
        URL,
        params={
            "from": ctx.trading_day.isoformat(),
            "to": horizon.isoformat(),
            "token": ctx.finnhub_key,
        },
        impact=IMPACT,
    )

    if isinstance(data, dict) and data.get("error"):
        raise SourceError(f"Finnhub: {data['error']}", IMPACT)

    rows = (data or {}).get("earningsCalendar")
    if rows is None:
        raise SourceError("response had no 'earningsCalendar' array", IMPACT)

    out = []
    for row in rows:
        symbol = (row.get("symbol") or "").upper()
        if symbol not in UNIVERSE:
            continue  # keeps the list to names that can reprice the tape
        date = row.get("date")
        if not date:
            continue

        entry = {
            "date": date,
            "symbol": symbol,
            "name": name_for(symbol),
            "session": SESSIONS.get(row.get("hour"), "unspecified"),
            "importance": importance_for(symbol),
        }

        estimate = row.get("epsEstimate")
        if estimate is not None:
            try:
                entry["eps_consensus"] = price(float(estimate), 2, currency=True)
            except (TypeError, ValueError):
                pass

        out.append(entry)

    out.sort(key=lambda e: (e["date"], e["importance"] != "high", e["symbol"]))
    return {"earnings": out}

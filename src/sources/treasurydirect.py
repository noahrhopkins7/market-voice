"""Treasury auction schedule — treasurydirect.gov TA_WS.

Verified live 2026-08-12. Free, no key.

SPEC.md Appendix A asked which of announced/auctioned to use. Answer:

* ``/TA_WS/securities/announced`` is the forward schedule — use this one.
* ``/TA_WS/securities/auctioned`` is *results* (highYield, etc.), retrospective.

The trap: ``days=N`` is a **lookback window on announcementDate**, not a
forward window. A ``days=14`` call on 2026-08-12 returned 16 records whose
auctionDates spanned 2026-08-11 (past) through 2026-08-13 (future). We
therefore filter on ``auctionDate >= trading_day`` ourselves.

Records carry ~120 fields; six are useful here.
"""

from __future__ import annotations

import datetime as dt
import re

from .common import SourceError, get_json, usd_large

NAME = "treasury_auctions"
IMPACT = "Treasury auction schedule unavailable"

URL = "https://www.treasurydirect.gov/TA_WS/securities/announced"

LOOKBACK_DAYS = 14   # announcement lookback, wide enough to catch next 1-2 weeks
FORWARD_DAYS = 14    # SPEC.md §3 section 7 covers the next 1-2 weeks


def fetch(ctx) -> dict:
    """Return ``{"treasury_auctions": [...]}`` for the calendar block."""
    records = get_json(
        URL,
        params={"format": "json", "days": LOOKBACK_DAYS},
        impact=IMPACT,
    )
    if not isinstance(records, list):
        raise SourceError("expected a JSON array from TA_WS", IMPACT)

    horizon = ctx.trading_day + dt.timedelta(days=FORWARD_DAYS)
    out = []
    for rec in records:
        auction_date = _date(rec.get("auctionDate"))
        if auction_date is None or not (ctx.trading_day <= auction_date <= horizon):
            continue

        entry = {
            "date": auction_date.isoformat(),
            "tenor": _tenor(rec.get("securityTerm", "")),
            "security_type": rec.get("securityType", ""),
        }

        amount = rec.get("offeringAmount")
        if amount not in (None, ""):
            try:
                entry["size"] = usd_large(float(amount))
            except (TypeError, ValueError):
                pass  # no size beats a wrong size

        out.append(entry)

    out.sort(key=lambda e: (e["date"], e["tenor"]))
    return {"treasury_auctions": out}


def _date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _tenor(term: str) -> str:
    """``30-Year`` -> ``30Y``, ``4-Week`` -> ``4W``, ``17-Week`` -> ``17W``."""
    match = re.match(r"\s*(\d+)\s*-?\s*(Year|Month|Week|Day)", term, re.I)
    if not match:
        return term.strip()
    count, unit = match.group(1), match.group(2).lower()
    return f"{count}{ {'year': 'Y', 'month': 'M', 'week': 'W', 'day': 'D'}[unit] }"

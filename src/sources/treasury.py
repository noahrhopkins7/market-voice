"""Daily Treasury par yield curve — home.treasury.gov XML.

Verified live 2026-08-12. Free, no key.

Endpoint shape confirmed in Phase 1:
    .../pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=YYYYMM

The month-scoped variant is used rather than the year-scoped one: 13KB and ~8
entries versus 239KB and 154. We only ever need the latest two sessions.

Two behaviours worth knowing:

* Treasury publishes around 3:30-4:00pm ET, so a 6:15am run always sees the
  prior session as the newest row. That is the ``curve_is_prior_day`` case the
  schema already anticipates (SPEC.md §2).
* ``change_bps`` needs two consecutive sessions. On the first trading day of a
  month the current month has only one row, so we also pull the prior month.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

from .common import SourceError, bps, get_text, num

NAME = "treasury_yield_curve"
IMPACT = "Treasury yield curve and spreads unavailable"

URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}

# (schema tenor label, XML element name). Full curve — SPEC.md §3 section 3
# asks for the whole thing with daily change in bps.
TENORS: list[tuple[str, str]] = [
    ("1M", "BC_1MONTH"),
    ("3M", "BC_3MONTH"),
    ("6M", "BC_6MONTH"),
    ("1Y", "BC_1YEAR"),
    ("2Y", "BC_2YEAR"),
    ("3Y", "BC_3YEAR"),
    ("5Y", "BC_5YEAR"),
    ("7Y", "BC_7YEAR"),
    ("10Y", "BC_10YEAR"),
    ("20Y", "BC_20YEAR"),
    ("30Y", "BC_30YEAR"),
]


def fetch(ctx) -> dict:
    """Return the ``rates`` block of market_data.json."""
    rows = _load_month(ctx.trading_day)

    # First trading day of a month: reach back for the prior session.
    if len(rows) < 2:
        first = ctx.trading_day.replace(day=1)
        prior_month = first - dt.timedelta(days=1)
        rows = _load_month(prior_month) + rows

    if not rows:
        raise SourceError("no yield curve rows returned", IMPACT)

    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else None

    curve = []
    for label, field in TENORS:
        yld = latest["yields"].get(field)
        if yld is None:
            continue  # Treasury drops tenors from time to time (20Y, 30Y have both vanished historically)
        entry = {"tenor": label, "yield": num(yld, f"{yld:.2f}%")}
        if previous is not None:
            prior_yield = previous["yields"].get(field)
            if prior_yield is not None:
                entry["change_bps"] = bps((yld - prior_yield) * 100)
        curve.append(entry)

    if not curve:
        raise SourceError("yield curve row contained no usable tenors", IMPACT)

    return {
        "rates": {
            "curve": curve,
            "spreads": _spreads(latest["yields"]),
            "curve_date": latest["date"].isoformat(),
            "curve_is_prior_day": latest["date"] < ctx.trading_day,
        }
    }


def _spreads(yields: dict[str, float]) -> dict:
    """2s10s and 3m10y, in basis points. Omitted if a leg is missing."""
    out = {}
    for key, (long_leg, short_leg) in {
        "2s10s": ("BC_10YEAR", "BC_2YEAR"),
        "3m10y": ("BC_10YEAR", "BC_3MONTH"),
    }.items():
        if long_leg in yields and short_leg in yields:
            out[key] = bps((yields[long_leg] - yields[short_leg]) * 100)
    return out


def _load_month(day: dt.date) -> list[dict]:
    """Fetch one month of curve rows, oldest first."""
    xml = get_text(
        URL,
        params={
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value_month": f"{day.year}{day.month:02d}",
        },
        impact=IMPACT,
    )

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError(f"could not parse Treasury XML: {exc}", IMPACT) from exc

    rows = []
    for props in root.iterfind(".//m:properties", _NS):
        date_el = props.find("d:NEW_DATE", _NS)
        if date_el is None or not date_el.text:
            continue
        try:
            row_date = dt.datetime.fromisoformat(date_el.text).date()
        except ValueError:
            continue

        yields = {}
        for _, field in TENORS:
            el = props.find(f"d:{field}", _NS)
            if el is not None and el.text:
                try:
                    yields[field] = float(el.text)
                except ValueError:
                    pass
        if yields:
            rows.append({"date": row_date, "yields": yields})

    rows.sort(key=lambda r: r["date"])
    return rows

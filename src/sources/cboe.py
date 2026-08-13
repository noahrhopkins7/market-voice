"""VIX and VIX3M — CBOE official daily history.

Free, no key, and authoritative: CBOE calculates these indices.

WHY THIS DEVIATES FROM SPEC.md APPENDIX A
------------------------------------------
Appendix A specifies yfinance for VOL. Verified against CBOE's own history in
Phase 2, yfinance's VIX series is *correct but incomplete* — every close it
carries matches CBOE to the cent, but it drops sessions:

    ^VIX     250 rows where NYSE had 252   (missing 07/21, 07/22, 07/31)
    ^VIX3M   234 rows where NYSE had 251   (17 gaps)
    ^GSPC/^IXIC/^DJI/stocks/ETFs   no gaps at all

Point-in-time values are therefore fine either way, and ``vix3m.last`` was never
stale. What breaks is any *rolling window*: a 20-session mean taken over a
gappy series silently reaches further back in calendar time than 20 sessions.
Measured on 2026-08-12 that pushed vix_20d_avg to 16.945 against a true 16.9625
— which rounds to "16.9" instead of "17.0". A wrong display string is exactly
what the model speaks aloud and the validator then blesses.

CBOE's CSV has no gaps, so the window is right by construction. yfinance stays
wired in as the fallback in fetch_data.py, so if CBOE changes this URL the
section degrades to slightly-imprecise rather than absent.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

from .common import SourceError, get_text, price, pct

NAME = "cboe_volatility"
IMPACT = "VIX and volatility term structure unavailable"

BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices"
VIX_URL = f"{BASE}/VIX_History.csv"
VIX3M_URL = f"{BASE}/VIX3M_History.csv"

AVERAGE_WINDOW = 20


def fetch(ctx) -> dict:
    """Return the ``volatility`` block."""
    vix = _history(VIX_URL)
    if len(vix) < 2:
        raise SourceError("CBOE VIX history too short to compute a change", IMPACT)

    last, prior = vix[-1][1], vix[-2][1]
    block: dict = {
        "vix": {
            "last": price(last, 2),
            "change_pct": pct((last / prior - 1) * 100, 1),
        }
    }

    if len(vix) >= AVERAGE_WINDOW:
        window = [close for _, close in vix[-AVERAGE_WINDOW:]]
        block["vix_20d_avg"] = price(sum(window) / len(window), 1)

    # A VIX3M failure must not cost us the VIX block.
    try:
        vix3m = _history(VIX3M_URL)
    except SourceError:
        vix3m = []

    if vix3m:
        vix3m_last = vix3m[-1][1]
        block["vix3m"] = {"last": price(vix3m_last, 2)}
        # Only compare like with like: a term structure built from two
        # different sessions is a fabricated signal.
        if vix3m[-1][0] == vix[-1][0]:
            block["term_structure"] = (
                "contango" if vix3m_last > last else "backwardation"
            )

    return {"volatility": block}


def _history(url: str) -> list[tuple[dt.date, float]]:
    """Parse a CBOE daily-price CSV into (date, close), oldest first."""
    text = get_text(url, impact=IMPACT)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        raw_date, raw_close = row.get("DATE"), row.get("CLOSE")
        if not raw_date or not raw_close:
            continue
        try:
            when = dt.datetime.strptime(raw_date.strip(), "%m/%d/%Y").date()
            rows.append((when, float(raw_close)))
        except ValueError:
            continue  # header repeats and blank rows appear in these files

    if not rows:
        raise SourceError(f"no usable rows in {url}", IMPACT)
    rows.sort(key=lambda r: r[0])
    return rows

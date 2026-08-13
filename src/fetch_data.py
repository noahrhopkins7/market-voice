"""Build market_data.json — the deterministic half of the briefing.

    python -m src.fetch_data --fixture          # no network, no rate limits
    python -m src.fetch_data --cache            # real fetch, caches result
    python -m src.fetch_data --dry-run          # print, don't write

Every source runs inside ``_run_source``, which is the only place failures are
handled. A source that raises produces one ``fetch_errors`` entry and costs its
own section — never the run (CLAUDE.md: one dead API degrades the briefing, it
never kills it).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .sources import (
    cboe, coingecko, fedcal, finnhub, fred, treasury, treasurydirect, yahoo,
)
from .sources.common import SourceError

log = logging.getLogger("fetch_data")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "market_data_sample.json"
OUTPUT_PATH = DATA_DIR / "market_data.json"
CACHE_PATH = DATA_DIR / "last_fetch.json"

ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = "1.0"

# Keys that belong under "calendar" rather than at the top level.
CALENDAR_KEYS = {"economic", "earnings", "fed_speakers", "treasury_auctions"}

# Schema order from SPEC.md §2, so diffs between runs stay readable.
KEY_ORDER = [
    "meta", "fetch_errors", "us_futures", "us_indices_prior_close",
    "volatility", "rates", "macro_latest", "fx", "commodities", "crypto",
    "global_indices", "sectors", "movers", "technicals", "calendar",
]


@dataclass
class Context:
    """Everything a source needs to know about this run."""
    trading_day: dt.date
    prior_session: dt.date
    now_utc: dt.datetime
    now_et: dt.datetime
    is_nyse_holiday: bool
    fred_key: str | None = None
    finnhub_key: str | None = None


def with_fallback(primary, secondary):
    """Try ``primary``; on SourceError fall back to ``secondary``.

    Used for volatility: CBOE is authoritative and gap-free, but if that URL
    moves we would rather have yfinance's slightly-imprecise rolling average
    than no VIX at all.
    """
    def run(ctx):
        try:
            return primary(ctx)
        except SourceError as exc:
            log.warning("primary source failed (%s); trying fallback", exc)
            return secondary(ctx)
    return run


def build_tasks() -> list[tuple[str, callable]]:
    """(source_name, callable) pairs, each independently failable."""
    return [
        # Volatility comes from CBOE rather than yfinance — see cboe.py for the
        # measured reason. yfinance remains the fallback.
        *[s for s in yahoo.SECTIONS if s[0] != "yahoo_volatility"],
        (cboe.NAME, with_fallback(cboe.fetch, yahoo.fetch_volatility)),
        (treasury.NAME, treasury.fetch),
        (treasurydirect.NAME, treasurydirect.fetch),
        (fedcal.NAME, fedcal.fetch),
        (coingecko.NAME, coingecko.fetch),
        (fred.NAME, fred.fetch),
        (finnhub.NAME, finnhub.fetch),
    ]


# --------------------------------------------------------------------------

def fetch_all(ctx: Context, use_cache: bool = False) -> dict:
    """Run every source and assemble market_data.json."""
    data: dict = {"meta": _meta(ctx), "fetch_errors": []}
    calendar: dict = {}
    cache = _load_cache() if use_cache else {}
    fresh: dict = {}

    for name, func in build_tasks():
        fragment = _run_source(ctx, name, func, data["fetch_errors"], cache)
        if not fragment:
            continue
        fresh[name] = fragment
        for key, value in fragment.items():
            if key in CALENDAR_KEYS:
                calendar[key] = value
            else:
                data[key] = value

    data["calendar"] = calendar

    if use_cache and fresh:
        _write_cache(fresh)

    return {k: data[k] for k in KEY_ORDER if k in data}


def _run_source(ctx, name, func, errors: list, cache: dict) -> dict | None:
    """Run one source. Never raises."""
    try:
        fragment = func(ctx)
        if not fragment:
            raise SourceError("source returned nothing")
        log.info("ok   %s", name)
        return fragment

    except SourceError as exc:
        return _degrade(name, str(exc), getattr(exc, "impact", ""), errors, cache)

    except Exception as exc:  # noqa: BLE001 - a source bug must not kill the run
        log.exception("unexpected failure in %s", name)
        return _degrade(name, f"{type(exc).__name__}: {exc}", "", errors, cache)


def _degrade(name, message, impact, errors: list, cache: dict) -> dict | None:
    """Record the failure, and fall back to cache if one was requested."""
    log.warning("FAIL %s: %s", name, message)
    cached = (cache.get("sources") or {}).get(name)

    if cached:
        # Stale data is served loudly, never silently: the model is told the
        # figures are carried over so it can say so.
        as_of = cache.get("cached_at_utc", "an earlier run")
        errors.append({
            "source": name,
            "error": message,
            "impact": f"{impact or 'section unavailable'} — "
                      f"served from cache captured {as_of}, figures may be stale",
        })
        return cached

    errors.append({
        "source": name,
        "error": message,
        "impact": impact or f"{name} data unavailable",
    })
    return None


# --------------------------------------------------------------------------

def _meta(ctx: Context) -> dict:
    return {
        "generated_at_utc": ctx.now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_et": ctx.now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "trading_day": ctx.trading_day.isoformat(),
        "prior_session": ctx.prior_session.isoformat(),
        "session_phase": "pre-market",  # SPEC.md Appendix E: always pre-market
        "is_nyse_holiday": ctx.is_nyse_holiday,
        "schema_version": SCHEMA_VERSION,
    }


def build_context() -> Context:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "API_KEYS.env")  # accepted as an alternative location
    import os

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_et = now_utc.astimezone(ET)
    trading_day, prior_session, is_holiday = _sessions(now_et.date())

    return Context(
        trading_day=trading_day,
        prior_session=prior_session,
        now_utc=now_utc,
        now_et=now_et,
        is_nyse_holiday=is_holiday,
        fred_key=os.getenv("FRED_API_KEY") or None,
        finnhub_key=os.getenv("FINNHUB_API_KEY") or None,
    )


def _sessions(today: dt.date) -> tuple[dt.date, dt.date, bool]:
    """Resolve the trading day and the prior session via the NYSE calendar."""
    try:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=(today - dt.timedelta(days=20)).isoformat(),
            end_date=(today + dt.timedelta(days=10)).isoformat(),
        )
        sessions = [d.date() for d in schedule.index]
        is_holiday = today not in sessions

        upcoming = [d for d in sessions if d >= today]
        trading_day = upcoming[0] if upcoming else today
        earlier = [d for d in sessions if d < trading_day]
        prior = earlier[-1] if earlier else trading_day - dt.timedelta(days=1)
        return trading_day, prior, is_holiday

    except Exception:  # noqa: BLE001 - calendar must never block a fetch
        log.warning("NYSE calendar unavailable; falling back to weekday arithmetic")
        trading_day = today
        while trading_day.weekday() >= 5:
            trading_day += dt.timedelta(days=1)
        prior = trading_day - dt.timedelta(days=1)
        while prior.weekday() >= 5:
            prior -= dt.timedelta(days=1)
        return trading_day, prior, False


# --------------------------------------------------------------------------

def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        log.warning("cache at %s unreadable; ignoring", CACHE_PATH)
        return {}


def _write_cache(sources: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "cached_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sources,
    }, indent=2))
    log.info("cache written to %s", CACHE_PATH)


def load_fixture() -> dict:
    if not FIXTURE_PATH.exists():
        raise SystemExit(
            f"fixture not found at {FIXTURE_PATH}\n"
            "Generate it with a live run: python -m src.fetch_data"
        )
    return json.loads(FIXTURE_PATH.read_text())


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.fetch_data",
        description="Fetch deterministic market data into market_data.json",
    )
    parser.add_argument("--fixture", action="store_true",
                        help="load tests/fixtures/market_data_sample.json instead of live APIs")
    parser.add_argument("--cache", action="store_true",
                        help="cache successful fetches to data/last_fetch.json and reuse on failure")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the JSON instead of writing market_data.json")
    parser.add_argument("--out", type=Path, default=OUTPUT_PATH,
                        help=f"output path (default: {OUTPUT_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    if args.fixture:
        data = load_fixture()
        log.info("loaded fixture from %s", FIXTURE_PATH)
    else:
        data = fetch_all(build_context(), use_cache=args.cache)

    payload = json.dumps(data, indent=2)

    if args.dry_run:
        print(payload)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n")
        print(f"wrote {args.out}", file=sys.stderr)

    errors = data.get("fetch_errors", [])
    if errors:
        print(f"\n{len(errors)} source(s) failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err['source']}: {err['error']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Everything sourced from yfinance.

This is the largest source module by design. Splitting it per schema section
(futures / FX / commodities / sectors / technicals / movers) would mean several
independent batch downloads against the same unofficial API, which is both
slower and more likely to trip Yahoo's rate limiting. One module, one set of
batched calls, one failure domain.

CLAUDE.md flags yfinance as unofficial and prone to breaking when Yahoo changes
endpoints. Each public section below is therefore fetched separately by
``fetch_data.py`` via ``SECTIONS``, so a break in, say, the movers download
does not take the futures block down with it.

Tickers are exactly as pinned in SPEC.md Appendix A — symbol typos produce
silent nulls, not errors.
"""

from __future__ import annotations

import datetime as dt
import logging
import warnings

import pandas as pd

from .common import SourceError, level, multiple, pct, points, price
from .universe import UNIVERSE, name_for

log = logging.getLogger(__name__)

# yfinance is chatty about individual ticker failures; we handle them ourselves.
warnings.filterwarnings("ignore", category=FutureWarning)

FUTURES = {"ES=F": "S&P 500 futures", "NQ=F": "Nasdaq 100 futures",
           "YM=F": "Dow futures", "RTY=F": "Russell 2000 futures"}

INDICES = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq Composite",
           "^DJI": "Dow Jones Industrial Average", "^RUT": "Russell 2000"}

VOL = {"^VIX": "VIX", "^VIX3M": "3-month VIX"}

GLOBAL = {"^N225": ("Nikkei 225", "Asia"), "^HSI": ("Hang Seng", "Asia"),
          "000001.SS": ("Shanghai Composite", "Asia"),
          "^STOXX": ("Stoxx 600", "Europe"), "^GDAXI": ("DAX", "Europe"),
          "^FTSE": ("FTSE 100", "Europe")}

FX = {"DX-Y.NYB": "Dollar index", "EURUSD=X": "Euro/dollar",
      "USDJPY=X": "Dollar/yen", "GBPUSD=X": "Sterling/dollar",
      "USDCNY=X": "Dollar/yuan"}

COMMODS = {"CL=F": "WTI crude", "BZ=F": "Brent crude", "GC=F": "Gold",
           "SI=F": "Silver", "HG=F": "Copper", "NG=F": "Natural gas"}

SECTORS = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
           "XLV": "Health care", "XLI": "Industrials",
           "XLY": "Consumer discretionary", "XLP": "Consumer staples",
           "XLU": "Utilities", "XLRE": "Real estate", "XLB": "Materials",
           "XLC": "Communication services"}

TECHNICALS_UNIVERSE = ["^GSPC", "^IXIC", "^RUT"]

MOVERS_PER_LIST = 5


# --------------------------------------------------------------------------
# Section entry points. fetch_data.py runs each independently.
# --------------------------------------------------------------------------

def fetch_futures(ctx) -> dict:
    bars = _daily(list(FUTURES), "5d", "US index futures unavailable")
    out = []
    for symbol, name in FUTURES.items():
        quote = _last_and_prior(bars, symbol)
        if quote is None:
            continue
        last, prior = quote
        out.append({
            "symbol": symbol,
            "name": name,
            "last": price(last, 2),
            "change_pct": pct((last / prior - 1) * 100),
            "change_pts": points(last - prior, 2),
        })
    return _require(out, "us_futures", "US index futures unavailable")


def fetch_indices(ctx) -> dict:
    bars = _daily(list(INDICES), "1y", "US index prior closes unavailable")
    out = []
    for symbol, name in INDICES.items():
        frame = bars.get(symbol)
        quote = _last_and_prior(bars, symbol)
        if quote is None or frame is None:
            continue
        close, prior = quote

        entry = {
            "symbol": symbol,
            "name": name,
            "close": price(close, 2),
            "change_pct": pct((close / prior - 1) * 100),
        }
        ytd = _ytd_pct(frame, close, ctx.trading_day)
        if ytd is not None:
            entry["ytd_pct"] = pct(ytd, 1)
        out.append(entry)
    return _require(out, "us_indices_prior_close", "US index prior closes unavailable")


def fetch_volatility(ctx) -> dict:
    impact = "VIX and volatility term structure unavailable"
    bars = _daily(list(VOL), "3mo", impact)

    vix = _last_and_prior(bars, "^VIX")
    if vix is None:
        raise SourceError("no VIX data returned", impact)
    last, prior = vix

    block: dict = {
        "vix": {"last": price(last, 2), "change_pct": pct((last / prior - 1) * 100, 1)}
    }

    vix3m_frame = bars.get("^VIX3M")
    if vix3m_frame is not None and not vix3m_frame.empty:
        vix3m_last = float(vix3m_frame["Close"].iloc[-1])
        block["vix3m"] = {"last": price(vix3m_last, 2)}
        block["term_structure"] = "contango" if vix3m_last > last else "backwardation"

    vix_frame = bars.get("^VIX")
    if vix_frame is not None and len(vix_frame) >= 20:
        block["vix_20d_avg"] = price(float(vix_frame["Close"].tail(20).mean()), 1)

    return {"volatility": block}


def fetch_fx(ctx) -> dict:
    return _require(_simple_quotes(FX, "FX rates unavailable"),
                    "fx", "FX rates unavailable")


def fetch_commodities(ctx) -> dict:
    return _require(_simple_quotes(COMMODS, "commodity prices unavailable"),
                    "commodities", "commodity prices unavailable")


def fetch_global(ctx) -> dict:
    impact = "global overnight indices unavailable"
    bars = _daily(list(GLOBAL), "5d", impact)
    out = []
    for symbol, (name, region) in GLOBAL.items():
        quote = _last_and_prior(bars, symbol)
        if quote is None:
            continue
        last, prior = quote
        out.append({
            "symbol": symbol,
            "name": name,
            "region": region,
            "last": price(last, 2),
            "change_pct": pct((last / prior - 1) * 100),
            "session_status": _session_status(region, ctx.now_et),
        })
    return _require(out, "global_indices", impact)


def fetch_sectors(ctx) -> dict:
    impact = "sector performance unavailable"
    bars = _daily(list(SECTORS), "5d", impact)
    rows = []
    for etf, name in SECTORS.items():
        quote = _last_and_prior(bars, etf)
        if quote is None:
            continue
        last, prior = quote
        rows.append({"etf": etf, "name": name,
                     "_change": (last / prior - 1) * 100})

    rows.sort(key=lambda r: r["_change"], reverse=True)
    out = [{"etf": r["etf"], "name": r["name"],
            "change_pct": pct(r["_change"]), "rank": i}
           for i, r in enumerate(rows, start=1)]
    return _require(out, "sectors", impact)


def _warn_on_gaps(symbol: str, frame: pd.DataFrame) -> None:
    """Flag a daily series that is missing NYSE sessions.

    yfinance returns correct closes but occasionally drops whole sessions
    (measured on 2026-08-12: ^RUT and ^VIX each missing 3 of ~251, ^VIX3M
    missing 17). Point values are unaffected; rolling windows are not, because
    a 20-row tail over a gappy series spans more than 20 sessions. Nothing here
    can reconstruct the missing bars, so the goal is simply that it is never
    silent.
    """
    try:
        import pandas_market_calendars as mcal

        have = {d.date() for d in frame.index}
        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=min(have).isoformat(), end_date=max(have).isoformat()
        )
        missing = {d.date() for d in schedule.index} - have
        if missing:
            log.warning(
                "%s is missing %d of %d NYSE sessions (most recent: %s); "
                "moving averages and 52-week extremes are approximate",
                symbol, len(missing), len(schedule), sorted(missing)[-3:],
            )
    except Exception:  # noqa: BLE001 - diagnostics must never break a fetch
        pass


def fetch_technicals(ctx) -> dict:
    impact = "technical levels unavailable"
    bars = _daily(TECHNICALS_UNIVERSE, "1y", impact)
    out = []

    for symbol in TECHNICALS_UNIVERSE:
        frame = bars.get(symbol)
        if frame is None or frame.empty:
            continue
        _warn_on_gaps(symbol, frame)
        closes = frame["Close"].dropna()
        if closes.empty:
            continue

        last = float(closes.iloc[-1])
        entry: dict = {
            "symbol": symbol,
            "name": INDICES.get(symbol, symbol),
            "last": price(last, 2),
        }

        for window, key in ((20, "sma20"), (50, "sma50"), (200, "sma200")):
            if len(closes) >= window:
                entry[key] = level(float(closes.tail(window).mean()))

        high_52w = float(frame["High"].dropna().max())
        low_52w = float(frame["Low"].dropna().min())
        entry["week52_high"] = level(high_52w)
        entry["week52_low"] = level(low_52w)

        if len(frame) >= 2:
            prior_bar = frame.iloc[-2]
            entry["prior_day_high"] = level(float(prior_bar["High"]))
            entry["prior_day_low"] = level(float(prior_bar["Low"]))

        drawdown = (last / high_52w - 1) * 100
        # Schema shows the magnitude in display, sign retained in value.
        entry["pct_from_52w_high"] = {
            "value": round(drawdown, 2),
            "display": f"{abs(drawdown):.1f}%",
        }
        if "sma50" in entry:
            entry["position_vs_sma50"] = (
                "above" if last > entry["sma50"]["value"] else "below"
            )
        out.append(entry)

    return _require(out, "technicals", impact)


def fetch_movers(ctx) -> dict:
    """Pre-market and prior-session movers, ranked within the large-cap universe.

    Finnhub's free tier has no market-wide screener, so movers are computed
    from the universe in universe.py rather than from a vendor's top-movers
    list. That biases toward large caps — which is what a macro briefing wants
    anyway — but it will miss a small-cap that gapped 40%.

    ``volume_vs_avg`` is emitted only for prior-session movers, where it is
    well defined (session volume / 20-day average). For pre-market movers it is
    deliberately omitted: comparing partial pre-market volume against a
    full-day average would produce a number that looks meaningful and is not.
    """
    impact = "movers unavailable"
    symbols = list(UNIVERSE)
    daily = _daily(symbols, "2mo", impact)

    prior_rows = []
    for symbol in symbols:
        frame = daily.get(symbol)
        if frame is None or len(frame) < 2:
            continue
        closes = frame["Close"].dropna()
        if len(closes) < 2:
            continue
        last, prior = float(closes.iloc[-1]), float(closes.iloc[-2])
        if prior == 0:
            continue

        row = {
            "symbol": symbol,
            "name": name_for(symbol),
            "_change": (last / prior - 1) * 100,
            "_last": last,
        }
        volumes = frame["Volume"].dropna()
        if len(volumes) >= 21 and volumes.tail(21).iloc[:-1].mean() > 0:
            row["_vol_ratio"] = float(volumes.iloc[-1]) / float(
                volumes.tail(21).iloc[:-1].mean()
            )
        prior_rows.append(row)

    if not prior_rows:
        raise SourceError("no usable quotes for the movers universe", impact)

    prior_rows.sort(key=lambda r: r["_change"], reverse=True)
    gainers = [_mover(r) for r in prior_rows[:MOVERS_PER_LIST]]
    losers = [_mover(r) for r in reversed(prior_rows[-MOVERS_PER_LIST:])]

    return {
        "movers": {
            "premarket": _premarket_movers(ctx, symbols, daily),
            "prior_session_gainers": gainers,
            "prior_session_losers": losers,
        }
    }


# Sections are (name, callable). Each fails independently.
SECTIONS = [
    ("yahoo_futures", fetch_futures),
    ("yahoo_indices", fetch_indices),
    ("yahoo_volatility", fetch_volatility),
    ("yahoo_fx", fetch_fx),
    ("yahoo_commodities", fetch_commodities),
    ("yahoo_global_indices", fetch_global),
    ("yahoo_sectors", fetch_sectors),
    ("yahoo_technicals", fetch_technicals),
    ("yahoo_movers", fetch_movers),
]


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _premarket_movers(ctx, symbols: list[str], daily: dict) -> list[dict]:
    """Pre-market moves from 1-minute bars including the pre/post session.

    Thin or absent pre-market bars are normal at 6:15am ET; symbols without
    them are skipped rather than backfilled from the regular session.
    """
    import yfinance as yf

    try:
        intraday = yf.download(
            tickers=" ".join(symbols), period="1d", interval="1m",
            prepost=True, group_by="ticker", auto_adjust=False,
            progress=False, threads=True,
        )
    except Exception as exc:  # noqa: BLE001 - yfinance raises bare Exceptions
        log.warning("pre-market download failed: %s", exc)
        return []

    rows = []
    for symbol in symbols:
        frame = _extract(intraday, symbol)
        daily_frame = daily.get(symbol)
        if frame is None or frame.empty or daily_frame is None or daily_frame.empty:
            continue
        closes = frame["Close"].dropna()
        if closes.empty:
            continue

        last = float(closes.iloc[-1])
        prior_close = float(daily_frame["Close"].dropna().iloc[-1])
        if prior_close == 0 or last == prior_close:
            continue
        rows.append({
            "symbol": symbol,
            "name": name_for(symbol),
            "_change": (last / prior_close - 1) * 100,
            "_last": last,
        })

    rows.sort(key=lambda r: abs(r["_change"]), reverse=True)
    return [_mover(r) for r in rows[:MOVERS_PER_LIST]]


def _mover(row: dict) -> dict:
    entry = {
        "symbol": row["symbol"],
        "name": row["name"],
        "change_pct": pct(row["_change"], 1),
        "last": price(row["_last"], 2, currency=True),
    }
    if "_vol_ratio" in row:
        entry["volume_vs_avg"] = multiple(row["_vol_ratio"])
    return entry


def _simple_quotes(mapping: dict[str, str], impact: str) -> list[dict]:
    bars = _daily(list(mapping), "5d", impact)
    out = []
    for symbol, name in mapping.items():
        quote = _last_and_prior(bars, symbol)
        if quote is None:
            continue
        last, prior = quote
        places = 4 if symbol.endswith("=X") and last < 10 else 2
        out.append({
            "symbol": symbol,
            "name": name,
            "last": price(last, places),
            "change_pct": pct((last / prior - 1) * 100),
        })
    return out


def _daily(symbols: list[str], period: str, impact: str) -> dict[str, pd.DataFrame]:
    """Batch daily bars, keyed by symbol. Raises only if the call itself fails."""
    import yfinance as yf

    try:
        raw = yf.download(
            tickers=" ".join(symbols), period=period, interval="1d",
            group_by="ticker", auto_adjust=False, progress=False, threads=True,
        )
    except Exception as exc:  # noqa: BLE001 - yfinance raises bare Exceptions
        raise SourceError(f"yfinance download failed: {exc}", impact) from exc

    if raw is None or raw.empty:
        raise SourceError("yfinance returned an empty frame", impact)

    out = {}
    for symbol in symbols:
        frame = _extract(raw, symbol)
        if frame is not None and not frame.empty:
            out[symbol] = frame.dropna(how="all")
    if not out:
        raise SourceError("yfinance returned no usable symbols", impact)
    return out


def _extract(raw: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Pull one symbol out of a grouped download, single-symbol case included."""
    if isinstance(raw.columns, pd.MultiIndex):
        if symbol not in raw.columns.get_level_values(0):
            return None
        return raw[symbol]
    return raw


def _last_and_prior(bars: dict, symbol: str) -> tuple[float, float] | None:
    frame = bars.get(symbol)
    if frame is None:
        return None
    closes = frame["Close"].dropna()
    if len(closes) < 2:
        return None
    last, prior = float(closes.iloc[-1]), float(closes.iloc[-2])
    if prior == 0:
        return None
    return last, prior


def _ytd_pct(frame: pd.DataFrame, close: float, trading_day: dt.date) -> float | None:
    """Percent change from the last close of the prior calendar year."""
    try:
        index = pd.to_datetime(frame.index)
        prior_year = index[index < pd.Timestamp(trading_day.year, 1, 1)]
        if len(prior_year):
            base = float(frame.loc[prior_year[-1], "Close"])
        else:
            this_year = frame[index >= pd.Timestamp(trading_day.year, 1, 1)]
            if this_year.empty:
                return None
            base = float(this_year["Close"].dropna().iloc[0])
        if base == 0:
            return None
        return (close / base - 1) * 100
    except Exception:  # noqa: BLE001 - index shapes vary across yfinance versions
        return None


def _session_status(region: str, now_et: dt.datetime) -> str:
    """Rough exchange status at run time. Asia is shut by 6:15am ET; Europe is open."""
    minutes = now_et.hour * 60 + now_et.minute
    if region == "Europe":
        return "open" if 3 * 60 <= minutes < 11 * 60 + 30 else "closed"
    if region == "Asia":
        return "open" if minutes < 3 * 60 or minutes >= 19 * 60 else "closed"
    return "closed"


def _require(rows: list, key: str, impact: str) -> dict:
    if not rows:
        raise SourceError(f"no usable data for {key}", impact)
    return {key: rows}

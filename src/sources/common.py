"""Shared plumbing for every source module.

Two things live here and nowhere else:

1. ``num()`` and friends — the *only* place a float becomes a display string.
   SPEC.md §2: round once, at fetch time, and everything downstream agrees.
   If rounding happened in two places they would eventually disagree and the
   validator would reject correct output.

2. ``SourceError`` — the single exception type every source raises on failure.
   Sources never return partial or defaulted data; they raise, and
   ``fetch_data.py`` turns that into a ``fetch_errors`` entry.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 20
USER_AGENT = "market-voice/1.0 (personal market briefing; contact via repo)"


class SourceError(Exception):
    """A source could not produce trustworthy data.

    Raised rather than returning a default. CLAUDE.md: never substitute a
    default value for missing market data.
    """

    def __init__(self, message: str, impact: str = "") -> None:
        super().__init__(message)
        self.impact = impact


# --------------------------------------------------------------------------
# The value/display pattern (SPEC.md §2)
# --------------------------------------------------------------------------

def num(value: Any, display: str) -> dict[str, Any]:
    """Build a ``{"value": float, "display": str}`` pair.

    ``display`` is what the model reproduces verbatim and what the validator
    checks against, so it is always a pre-rounded string.
    """
    if value is None:
        raise SourceError("refusing to build a numeric field from None")
    return {"value": float(value), "display": display}


def _commas(value: float, places: int) -> str:
    return f"{value:,.{places}f}"


def price(value: float, places: int = 2, currency: bool = False) -> dict[str, Any]:
    """A price level: ``6,412.25`` or ``$182.40``."""
    body = _commas(value, places)
    return num(value, f"${body}" if currency else body)


def level(value: float, places: int = 0) -> dict[str, Any]:
    """A rounded level, used for moving averages and 52-week extremes."""
    return num(value, _commas(value, places))


def pct(value: float, places: int = 2, signed: bool = True) -> dict[str, Any]:
    """A percentage: ``+0.34%`` / ``-0.42%`` / ``2.7%``."""
    sign = "+" if signed and value > 0 else ""
    return num(value, f"{sign}{value:.{places}f}%")


def points(value: float, places: int = 2) -> dict[str, Any]:
    """A signed point change: ``+21.75``."""
    sign = "+" if value > 0 else ""
    return num(value, f"{sign}{_commas(value, places)}")


def bps(value: float) -> dict[str, Any]:
    """A basis-point change: ``+3 bps`` / ``-1 bp`` / ``unchanged``.

    Singular "bp" at exactly 1 because the script is read aloud and "1 bps"
    is audibly wrong.
    """
    rounded = round(value)
    if rounded == 0:
        return num(value, "unchanged")
    sign = "+" if rounded > 0 else "-"
    unit = "bp" if abs(rounded) == 1 else "bps"
    return num(value, f"{sign}{abs(rounded)} {unit}")


def usd_large(value: float) -> dict[str, Any]:
    """A large dollar amount, spoken: ``$25 billion``, ``$1.2 trillion``."""
    for cutoff, suffix in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if abs(value) >= cutoff:
            scaled = value / cutoff
            body = f"{scaled:.0f}" if scaled == int(scaled) else f"{scaled:.1f}"
            return num(value, f"${body} {suffix}")
    return num(value, f"${value:,.0f}")


def multiple(value: float, places: int = 1) -> dict[str, Any]:
    """A ratio spoken as a multiple: ``3.2x average``."""
    return num(value, f"{value:.{places}f}x average")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def get_json(url: str, params: dict | None = None, impact: str = "", **kwargs) -> Any:
    """GET returning parsed JSON, or raise SourceError."""
    resp = _get(url, params=params, impact=impact, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"{url} returned non-JSON body", impact) from exc


def get_text(url: str, params: dict | None = None, impact: str = "", **kwargs) -> str:
    """GET returning decoded text, or raise SourceError."""
    return _get(url, params=params, impact=impact, **kwargs).text


def _get(url: str, params: dict | None, impact: str, **kwargs) -> requests.Response:
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    try:
        resp = requests.get(
            url, params=params, headers=headers, timeout=HTTP_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise SourceError(f"{type(exc).__name__}: {exc}", impact) from exc

    if resp.status_code != 200:
        body = resp.text[:180].replace("\n", " ")
        raise SourceError(f"HTTP {resp.status_code} from {url}: {body}", impact)
    return resp

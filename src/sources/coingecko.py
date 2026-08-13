"""Crypto spot + 24h change — CoinGecko simple/price.

Verified live 2026-08-12. Free, no key, no registration for this endpoint.
Public tier allows roughly 30 calls/min; we make one call per run.

Returns full float precision (e.g. -0.3440595185964687), so rounding happens
here at fetch time like every other source.
"""

from __future__ import annotations

from .common import SourceError, get_json, pct, price

NAME = "coingecko"
IMPACT = "crypto prices unavailable"

URL = "https://api.coingecko.com/api/v3/simple/price"

# CoinGecko id -> (schema symbol, spoken name)
COINS = {
    "bitcoin": ("BTC", "Bitcoin"),
    "ethereum": ("ETH", "Ethereum"),
}


def fetch(ctx) -> dict:
    """Return ``{"crypto": [...]}``."""
    data = get_json(
        URL,
        params={
            "ids": ",".join(COINS),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        },
        impact=IMPACT,
    )

    out = []
    for coin_id, (symbol, name) in COINS.items():
        quote = data.get(coin_id)
        if not quote or quote.get("usd") is None:
            continue

        last = float(quote["usd"])
        # Bitcoin at 5 figures wants no decimals; Ethereum does.
        entry = {
            "symbol": symbol,
            "name": name,
            "last": price(last, places=0 if last >= 1000 else 2, currency=True),
        }
        change = quote.get("usd_24h_change")
        if change is not None:
            entry["change_pct_24h"] = pct(float(change))
        out.append(entry)

    if not out:
        raise SourceError("CoinGecko returned no usable quotes", IMPACT)
    return {"crypto": out}

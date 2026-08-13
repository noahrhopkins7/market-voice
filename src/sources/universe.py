"""Large-cap universe: ticker -> spoken company name.

Why this exists
---------------
Two gaps make it necessary:

1. Finnhub's ``/calendar/earnings`` returns ``symbol`` but **no company name and
   no importance ranking**, both of which SPEC.md §2 requires. Resolving names
   via ``/stock/profile2`` would cost one API call per symbol per day.
2. Movers have to be ranked against *some* universe. Finnhub's free tier has no
   market-wide screener.

A static reference table solves both with zero API calls. This is the same kind
of constant as the SECTORS and FUTURES dicts in SPEC.md Appendix A — tickers and
names, no market figures, so it does not violate the no-hardcoded-figures rule.

Names are written the way a narrator should say them, since SPEC.md §4 requires
tickers be converted to company names on first mention.
"""

from __future__ import annotations

UNIVERSE: dict[str, str] = {
    # Mega-cap tech
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
    "ORCL": "Oracle", "CRM": "Salesforce", "AMD": "AMD", "ADBE": "Adobe",
    "NFLX": "Netflix", "CSCO": "Cisco", "INTC": "Intel", "QCOM": "Qualcomm",
    "TXN": "Texas Instruments", "IBM": "IBM", "NOW": "ServiceNow",
    "INTU": "Intuit", "AMAT": "Applied Materials", "MU": "Micron",
    "LRCX": "Lam Research", "KLAC": "KLA", "ADI": "Analog Devices",
    "PANW": "Palo Alto Networks", "SNPS": "Synopsys", "CDNS": "Cadence",
    "ANET": "Arista Networks", "PLTR": "Palantir", "UBER": "Uber",
    "DELL": "Dell", "SMCI": "Super Micro Computer",

    # Financials
    "BRK-B": "Berkshire Hathaway", "JPM": "JPMorgan", "V": "Visa",
    "MA": "Mastercard", "BAC": "Bank of America", "WFC": "Wells Fargo",
    "GS": "Goldman Sachs", "MS": "Morgan Stanley", "C": "Citigroup",
    "SCHW": "Charles Schwab", "BLK": "BlackRock", "AXP": "American Express",
    "SPGI": "S&P Global", "PGR": "Progressive", "CB": "Chubb",
    "PYPL": "PayPal", "COF": "Capital One",

    # Health care
    "LLY": "Eli Lilly", "UNH": "UnitedHealth", "JNJ": "Johnson & Johnson",
    "ABBV": "AbbVie", "MRK": "Merck", "TMO": "Thermo Fisher",
    "ABT": "Abbott", "PFE": "Pfizer", "DHR": "Danaher", "AMGN": "Amgen",
    "ISRG": "Intuitive Surgical", "GILD": "Gilead", "CVS": "CVS Health",
    "MDT": "Medtronic", "BMY": "Bristol Myers Squibb", "VRTX": "Vertex",

    # Consumer
    "WMT": "Walmart", "COST": "Costco", "PG": "Procter & Gamble",
    "HD": "Home Depot", "KO": "Coca-Cola", "PEP": "PepsiCo",
    "MCD": "McDonald's", "NKE": "Nike", "SBUX": "Starbucks",
    "TGT": "Target", "LOW": "Lowe's", "TJX": "TJX", "BKNG": "Booking",
    "DIS": "Disney", "CMG": "Chipotle", "MDLZ": "Mondelez",
    "PM": "Philip Morris", "MO": "Altria", "GM": "General Motors",
    "F": "Ford", "LULU": "Lululemon",

    # Industrials / energy / materials
    "XOM": "Exxon Mobil", "CVX": "Chevron", "COP": "ConocoPhillips",
    "SLB": "SLB", "EOG": "EOG Resources", "CAT": "Caterpillar",
    "DE": "Deere", "BA": "Boeing", "GE": "GE Aerospace", "HON": "Honeywell",
    "UPS": "UPS", "UNP": "Union Pacific", "RTX": "RTX", "LMT": "Lockheed Martin",
    "MMM": "3M", "LIN": "Linde", "FDX": "FedEx",

    # Comms / utilities / real estate
    "T": "AT&T", "VZ": "Verizon", "TMUS": "T-Mobile", "CMCSA": "Comcast",
    "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Company",
    "AMT": "American Tower", "PLD": "Prologis",
}

# Ranked by how reliably a surprise here moves the whole tape, not just the name.
BELLWETHERS: frozenset[str] = frozenset({
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "JPM", "WMT", "LLY", "UNH", "XOM", "BRK-B", "V", "MA", "COST", "ORCL",
})


def name_for(symbol: str) -> str:
    """Spoken name for a ticker, falling back to the ticker itself."""
    return UNIVERSE.get(symbol.upper(), symbol.upper())


def importance_for(symbol: str) -> str:
    """Derived, not invented: bellwether membership is the only signal we have."""
    return "high" if symbol.upper() in BELLWETHERS else "medium"

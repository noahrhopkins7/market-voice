# Morning Market Briefing — Build Spec

Daily automated audio market briefing, delivered as a private podcast feed.

**Target:** 10–12 minutes of audio (~1,750 spoken words), published to a private RSS feed by 6:50am ET on NYSE trading days, consumed in Apple Podcasts on iPhone.

**Budget ceiling:** $25/month all-in.

---

## 0. Architecture in one line

```
cron (6:15am ET)
  → fetch_data.py    : deterministic market data  → market_data.json
  → generate.py      : Pass 1 (research/report)   → report.md
                       Pass 2 (spoken script)     → script.txt
  → validate.py      : every figure in script must exist in market_data.json
  → tts.py           : chunked TTS + concat       → episode.mp3
  → publish.py       : update feed.xml, prune old episodes, commit
```

**Non-negotiable design rule:** the language model never states a number it was not handed. It synthesizes, ranks, and explains. All figures come from `market_data.json`. This is enforced mechanically in `validate.py`, not by trusting the prompt.

---

## 1. Scope decisions (what's in, what's cut, and why)

### In — deterministic (fetched, never model-recalled)

| Category | Source | Cost |
|---|---|---|
| US index futures (ES, NQ, YM, RTY) | `yfinance` (`ES=F` etc.) | free |
| US index prior closes (SPX, NDX, DJI, RUT) | `yfinance` | free |
| VIX + VIX3M (term structure) | `yfinance` (`^VIX`, `^VIX3M`) | free |
| Treasury curve (1M–30Y) + spreads | treasury.gov daily yield curve XML | free |
| Macro series (CPI, unemployment, fed funds) | FRED API | free |
| FX majors (DXY, EURUSD, USDJPY, USDCNY) | `yfinance` | free |
| Commodities (WTI, Brent, gold, copper, nat gas) | `yfinance` | free |
| Crypto (BTC, ETH) | CoinGecko | free |
| Global overnight (Nikkei, Hang Seng, Shanghai, Stoxx 600, DAX, FTSE) | `yfinance` | free |
| Sector performance (11 SPDR ETFs) | `yfinance` | free |
| Technical levels (20/50/200DMA, 52w H/L, prior day H/L) | computed from `yfinance` history | free |
| Movers + volume | Finnhub / `yfinance` screener | free tier |
| Earnings calendar | Finnhub | free tier |
| Economic calendar | Finnhub or FMP | free tier |
| Treasury auction schedule | treasurydirect.gov API | free |
| Fed speaker calendar | federalreserve.gov RSS | free |

### In — narrative only (web search, zero new numbers)

Market-moving news, macro developments, Fed commentary and tone, geopolitics, trade/tariffs/sanctions, M&A, AI developments, regulation, *why* the movers moved, and forward catalysts. The model may describe and interpret. It may not quantify beyond what's in the JSON.

### Cut — and this matters

**Options activity, insider buying/selling, institutional fund flows, ETF flows, market breadth internals, sentiment surveys.**

These are not available free and reliably. They live in paid feeds (options tape, Form 4/13F processing, exchange breadth data). If you leave them in the prompt without a source, the model will produce confident, plausible, fabricated numbers — read to you in an authoritative voice at 7am. That is strictly worse than omitting them.

At 10–12 minutes you don't have airtime for them anyway. If you later want them, add the paid feed *first*, then the prompt section.

**Note on technical levels:** you asked for support/resistance and I kept it — but computed deterministically (moving averages, 52-week extremes, prior-session high/low), not model-invented. That's the only honest version.

**Note on rate expectations:** CME FedWatch has no clean free API. Treat implied-probability language as best-effort narrative from web search, explicitly labeled as such, or derive from fed funds futures yourself later. Don't let the model state a probability figure.

---

## 2. `market_data.json` schema

### The `value` / `display` pattern — read this before implementing

Every numeric field carries two forms:

```json
{ "value": 4.3217, "display": "4.32%" }
```

- `value` — raw float, for your own computation
- `display` — **pre-rounded string, exactly as it should be spoken**

The model is instructed to reproduce `display` strings verbatim. The validator checks the script's figures against the set of all `display` strings. Without this, the fetcher emits `4.3217`, the model sensibly says `4.32%`, and naive validation fails on a correct output. Round once, at fetch time, and everything downstream agrees.

### Schema

```json
{
  "meta": {
    "generated_at_utc": "2026-08-13T10:20:04Z",
    "generated_at_et": "2026-08-13 06:20:04 EDT",
    "trading_day": "2026-08-13",
    "prior_session": "2026-08-12",
    "session_phase": "pre-market",
    "is_nyse_holiday": false,
    "schema_version": "1.0"
  },

  "fetch_errors": [
    { "source": "finnhub_economic_calendar", "error": "429 rate limited", "impact": "economic calendar unavailable" }
  ],

  "us_futures": [
    {
      "symbol": "ES=F", "name": "S&P 500 futures",
      "last": { "value": 6412.25, "display": "6,412.25" },
      "change_pct": { "value": 0.34, "display": "+0.34%" },
      "change_pts": { "value": 21.75, "display": "+21.75" }
    }
  ],

  "us_indices_prior_close": [
    {
      "symbol": "^GSPC", "name": "S&P 500",
      "close": { "value": 6390.50, "display": "6,390.50" },
      "change_pct": { "value": -0.42, "display": "-0.42%" },
      "ytd_pct": { "value": 8.7, "display": "+8.7%" }
    }
  ],

  "volatility": {
    "vix": { "last": { "value": 14.82, "display": "14.82" },
             "change_pct": { "value": 3.1, "display": "+3.1%" } },
    "vix3m": { "last": { "value": 17.20, "display": "17.20" } },
    "term_structure": "contango",
    "vix_20d_avg": { "value": 15.4, "display": "15.4" }
  },

  "rates": {
    "curve": [
      { "tenor": "3M",  "yield": { "value": 4.28, "display": "4.28%" }, "change_bps": { "value": -1.0, "display": "-1 bp" } },
      { "tenor": "2Y",  "yield": { "value": 3.86, "display": "3.86%" }, "change_bps": { "value": -3.0, "display": "-3 bps" } },
      { "tenor": "10Y", "yield": { "value": 4.32, "display": "4.32%" }, "change_bps": { "value": 2.0, "display": "+2 bps" } },
      { "tenor": "30Y", "yield": { "value": 4.79, "display": "4.79%" }, "change_bps": { "value": 3.0, "display": "+3 bps" } }
    ],
    "spreads": {
      "2s10s": { "value": 46.0, "display": "+46 bps" },
      "3m10y": { "value": 4.0,  "display": "+4 bps" }
    },
    "curve_date": "2026-08-12",
    "curve_is_prior_day": true
  },

  "macro_latest": [
    {
      "series": "CPI YoY", "fred_id": "CPIAUCSL",
      "latest": { "value": 2.7, "display": "2.7%" },
      "prior":  { "value": 2.9, "display": "2.9%" },
      "as_of": "2026-07-31", "next_release": "2026-08-13"
    }
  ],

  "fx":          [ { "symbol": "DX-Y.NYB", "name": "Dollar index", "last": {...}, "change_pct": {...} } ],
  "commodities": [ { "symbol": "CL=F", "name": "WTI crude", "last": {...}, "change_pct": {...} } ],
  "crypto":      [ { "symbol": "BTC", "name": "Bitcoin", "last": {...}, "change_pct_24h": {...} } ],

  "global_indices": [
    { "symbol": "^N225", "name": "Nikkei 225", "region": "Asia",
      "last": {...}, "change_pct": {...}, "session_status": "closed" }
  ],

  "sectors": [
    { "etf": "XLK", "name": "Technology", "change_pct": { "value": 1.24, "display": "+1.24%" }, "rank": 1 }
  ],

  "movers": {
    "premarket": [
      { "symbol": "NVDA", "name": "Nvidia",
        "change_pct": { "value": 4.8, "display": "+4.8%" },
        "last": { "value": 182.40, "display": "$182.40" },
        "volume_vs_avg": { "value": 3.2, "display": "3.2x average" },
        "catalyst_hint": "Q2 earnings released after close" }
    ],
    "prior_session_gainers": [ ],
    "prior_session_losers":  [ ]
  },

  "technicals": [
    {
      "symbol": "^GSPC", "name": "S&P 500",
      "last": { "value": 6390.50, "display": "6,390.50" },
      "sma20":  { "value": 6355.1, "display": "6,355" },
      "sma50":  { "value": 6280.4, "display": "6,280" },
      "sma200": { "value": 5940.2, "display": "5,940" },
      "week52_high": { "value": 6455.0, "display": "6,455" },
      "week52_low":  { "value": 5120.0, "display": "5,120" },
      "prior_day_high": { "value": 6410.2, "display": "6,410" },
      "prior_day_low":  { "value": 6371.8, "display": "6,372" },
      "pct_from_52w_high": { "value": -1.0, "display": "1.0%" },
      "position_vs_sma50": "above"
    }
  ],

  "calendar": {
    "economic": [
      { "date": "2026-08-13", "time_et": "08:30", "event": "CPI (July)",
        "consensus": { "value": 0.2, "display": "0.2% m/m" },
        "prior": { "value": 0.3, "display": "0.3% m/m" }, "importance": "high" }
    ],
    "earnings": [
      { "date": "2026-08-13", "symbol": "WMT", "name": "Walmart", "session": "before_open",
        "eps_consensus": { "value": 0.74, "display": "$0.74" }, "importance": "high" }
    ],
    "fed_speakers":      [ { "date": "2026-08-14", "time_et": "13:00", "speaker": "…", "topic": "…" } ],
    "treasury_auctions": [ { "date": "2026-08-14", "tenor": "30Y", "size": { "value": 25e9, "display": "$25 billion" } } ]
  }
}
```

### `fetch_errors` is load-bearing

Any source that fails gets an entry. That array is injected into the prompt, and the model is instructed to say "that data wasn't available this morning" rather than fill the gap. A silent fetch failure is how fabricated numbers get in through the back door.

---

## 3. Pass 1 — research and report

Model: Claude Sonnet class, web search enabled. Output: `report.md` (~1,750 words of prose plus tables). This is also the podcast show-notes text.

```
You are an institutional macro strategist and equity research analyst writing a
pre-market briefing for a single reader: a finance-focused reader who will listen
to this as audio at 7:00am ET before the US open. Assume fluency — no definitions
of standard terms, no hedging filler, no throat-clearing.

CURRENT MARKET DATA (authoritative):
<market_data>
{market_data_json}
</market_data>

ABSOLUTE RULES ON FIGURES
1. Every number you write must appear as a "display" string in the market data
   above. Reproduce it character-for-character, including sign, comma, currency
   symbol, and percent sign.
2. You may not calculate, derive, estimate, round, or convert any figure. If you
   want to express a relationship not present in the data, describe it
   qualitatively ("the curve steepened") rather than numerically.
3. If a figure you want is absent, or its source appears in "fetch_errors",
   state plainly that the data was unavailable. Never approximate.
4. Exceptions permitted: calendar dates, times, ordinal counts of items you are
   listing, and years.

USE OF WEB SEARCH
Search for narrative context only: what happened overnight, why it happened, how
it is being interpreted, and what is expected next. Do not import numbers from
search results into the briefing — search tells you the story, the market data
block tells you the figures. If a search result's numbers conflict with the
market data block, the market data block wins and you do not mention the conflict.

EPISTEMIC DISCIPLINE
- Label interpretation as interpretation. "The tape is reading this as dovish" is
  fine; presenting it as fact is not.
- Where the market's reaction is ambiguous, say so rather than manufacturing a
  clean narrative.
- Rank by expected market impact, not by how interesting the story is.
- Skip anything that does not have a plausible path to moving prices. No
  human-interest, no social media chatter, no company news below material size.

STRUCTURE AND WORD BUDGET (~1,750 words of prose; tables do not count)

1. THE SETUP (180 words)
   Three sentences on where we are, then the three things that matter today in
   priority order, one sentence each.

2. OVERNIGHT TAPE (200 words + table)
   Futures, Asia, Europe, vol, dollar. What moved and the one-line reason.
   Table: futures, global indices, VIX.

3. RATES AND THE FED (250 words + table)
   Curve, key spreads, what's driving them, Fed commentary and its read.
   Table: full curve with daily change in bps.

4. MACRO (200 words)
   Latest prints, what today's calendar could do to the rates picture. Skip
   entirely if nothing material — say so in one sentence rather than padding.

5. RANKED DEVELOPMENTS (450 words, 3–5 items)
   For each: what happened / why it matters / how markets reacted or are
   positioned / bullish, bearish, or neutral and for what / what to watch next.

6. MOVERS (250 words + table)
   Biggest pre-market and prior-session moves with the actual catalyst. If you
   cannot establish why something moved, say the move is unexplained. Do not
   invent a reason.
   Table: symbol, move, catalyst.

7. CATALYSTS, NEXT 1–2 WEEKS (200 words + table)
   Economic releases, Fed events, earnings, Treasury auctions. Flag the two or
   three that could actually reprice something.
   Table: date, event, why it matters.

Write in flowing analytical prose. Tables supplement, never replace, the
explanation. Markdown headings only.
```

---

## 4. Pass 2 — spoken script

Model: a cheap fast model is fine here (Haiku class) — this is a format transform, not analysis. Output: `script.txt`, plain text, ~1,750 words.

```
Rewrite the briefing below as a script to be read aloud by a single narrator.
This is a pure format transformation. The analysis, the ordering, the judgments,
and every figure stay exactly as they are.

HARD RULE ON NUMBERS
Reproduce every numeric value character-for-character as it appears in the
source. Do not round, reformat, convert, spell out, or combine figures. You may
change the words around a number; you may never change the number. A downstream
validator will reject the script if any figure differs.

REMOVE
- All markdown: headings, bullets, bold, tables.
- Tables entirely. If a table carried a fact not stated in the prose, fold that
  fact into a sentence. Otherwise drop it.
- Any phrase referring to visual layout ("as shown below", "the table above",
  "see the following").

CONVERT FOR THE EAR
- Tickers to company names on first mention: "NVDA" becomes "Nvidia". A bare
  ticker read aloud is unintelligible.
- Abbreviations to words: "bps" to "basis points", "YoY" to "year over year",
  "m/m" to "month over month", "EPS" to "earnings per share".
- Acronyms spelled out on first mention only: "FOMC" becomes "the FOMC, the
  Fed's rate-setting committee". Thereafter just "the FOMC".
- Index shorthand: "10Y" becomes "the ten-year", "2s10s" becomes "the two-year
  to ten-year spread".
- Leave numerals as numerals — "4.32%", "6,412.25", "$182.40". The speech engine
  reads these correctly, and spelling them out breaks validation.

STRUCTURE FOR THE EAR
- Continuous prose. Where the source had a heading, write a spoken transition
  instead: "Turning to rates." / "On the corporate side."
- Signpost lists in speech: "Three things stand out this morning. First, …"
- Break sentences over about 30 words into two. Listeners lose long clauses.
- Open with: "Good morning. It's {weekday}, {month} {day}. Here's your
  pre-market briefing." Close with one sentence naming the single thing to watch
  today, then: "That's your briefing. Have a good one."

Target 1,700–1,800 words. Output the script text only — no preamble, no
commentary, no markdown.

BRIEFING:
{report_markdown}
```

---

## 5. Validator logic

`validate.py` is the whole reason this project is trustworthy. Build it early, not last.

```
allowed = set of every "display" string in market_data.json
        + all dates/times appearing in the calendar section
        + whitelist: years 1900–2100, integers 1–20 (list counts),
          "one", "two", "three" … "twenty"

extract from script.txt every token matching:
    currency amounts     $1,234.56  $25 billion
    percentages          4.32%  -0.42%  +3.1%
    decimals             6,412.25  14.82
    bps figures          46 bps  -3 bps
    large integers       >20 with or without commas

for each extracted token:
    normalize (strip +, unify comma/space) and test membership in allowed

FAIL  → log the offending tokens, regenerate Pass 1 once with the failures
        appended as a correction note. Fail twice → publish the previous day's
        episode? No. Publish nothing and send yourself a failure notification.
        A missing episode is recoverable; a wrong one you acted on is not.
```

Tune the whitelist over the first two weeks — expect false positives early from legitimate phrasing. Log every rejection so you can see the pattern rather than guessing.

---

## 6. TTS

- **Model:** OpenAI `gpt-4o-mini-tts`, ~$0.015/min → roughly $5/month at 12 min/day.
- **Voice:** test `ash`, `sage`, `cedar`, `marin` on the same 300-word sample and pick by ear. Don't pick from documentation adjectives.
- **`instructions` field:** this is the "non-robotic" lever. Something like — *"Measured and conversational, like a colleague reading you a desk note over coffee. Unhurried. Slight downward inflection at sentence ends. Do not sound like a newsreader or an advertisement."* Iterate on this before you touch anything else; it moves quality more than the voice choice does.
- **Chunking is mandatory.** `gpt-4o-mini-tts` caps at 2,000 input tokens per request; a 1,750-word script is roughly 2,750. Split on **paragraph then sentence boundaries** — never mid-sentence, or you get an audible prosody break at the seam. Concatenate with `pydub`/ffmpeg.
- **Encoding:** 64kbps mono is plenty for speech. ~5.5MB for 12 minutes.
- Prepend 0.5s of silence so podcast apps don't clip the first word.

---

## 7. Scheduling and publishing

### Cron — the DST trap

GitHub Actions cron is UTC and does not observe daylight saving. Both entries, weekdays only:

```yaml
on:
  schedule:
    - cron: '15 10 * * 1-5'   # 06:15 EDT (summer)
    - cron: '15 11 * * 1-5'   # 06:15 EST (winter)
  workflow_dispatch:           # keep this — you'll want manual runs constantly
```

Then guard inside the script: exit immediately unless `America/New_York` local time is between 06:00 and 06:59. Otherwise you publish twice a day for half the year.

Also skip NYSE holidays — use `pandas_market_calendars`, not a hardcoded list.

GitHub's cron is best-effort and can drift 5–20 minutes under load. 6:15am start gives room for drift plus a ~4 minute pipeline.

### Feed

RSS 2.0 with the `itunes` namespace. Required per episode: `<title>`, `<pubDate>` (RFC 2822), `<enclosure url length type="audio/mpeg">`, `<guid isPermaLink="false">`, `<itunes:duration>`. Put `report.md` (rendered to HTML) in `<description>` — that's your background text version.

Host on GitHub Pages from a `gh-pages` branch. Privacy is by unguessable URL: put the feed at a random path like `/f/a7c3e91b4d/feed.xml`. Not real security — don't treat it as such — but adequate for a personal feed.

Prune episodes older than 14 days in `publish.py` and rewrite the feed each run, or the repo grows unbounded.

---

## 8. Repo structure

```
market-brief/
├── CLAUDE.md
├── SPEC.md                      ← this file
├── .github/workflows/brief.yml
├── src/
│   ├── fetch_data.py            # → market_data.json
│   ├── sources/                 # one module per data source, each independently failable
│   │   ├── yahoo.py
│   │   ├── treasury.py
│   │   ├── fred.py
│   │   ├── finnhub.py
│   │   └── coingecko.py
│   ├── generate.py              # Pass 1 + Pass 2
│   ├── validate.py
│   ├── tts.py
│   ├── publish.py
│   └── prompts/
│       ├── research.txt
│       └── script.txt
├── tests/
│   ├── test_validate.py
│   └── fixtures/market_data_sample.json
├── data/                        # gitignored, local runs only
└── requirements.txt
```

### Secrets (GitHub repo → Settings → Secrets → Actions)

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`

---

## 9. What to tell Claude Code

Save this spec as `SPEC.md` in an empty repo, then open Claude Code and paste:

> Read SPEC.md in full before writing any code. It's the complete design for a daily automated market-briefing podcast. Build it in the phases below and stop at each checkpoint so I can verify before you continue.
>
> **Phase 1 — data layer.** Build `src/sources/*` and `src/fetch_data.py` to produce `market_data.json` exactly matching the schema in SPEC.md §2, including the `value`/`display` pattern and the `fetch_errors` array. Every source module must fail independently — one dead API degrades the briefing, it never kills the run. Then run it and show me the actual output JSON. Do not proceed until I've confirmed the numbers are right.
>
> **Phase 2 — validator, before the generator.** Build `src/validate.py` per SPEC.md §5, plus `tests/test_validate.py` with fixtures covering: a clean script, a script with one fabricated percentage, a script with a rounded-differently figure, and a script with legitimate list-count integers that must not trip it. All tests pass before moving on.
>
> **Phase 3 — generation.** Wire up both prompt passes from SPEC.md §3 and §4 as files in `src/prompts/`, called from `src/generate.py`. Use Claude Sonnet with web search for Pass 1 and Haiku for Pass 2. Run it against yesterday's real data and show me `report.md` and `script.txt`, then run the validator against them and show me what it catches.
>
> **Phase 4 — audio.** `src/tts.py` per SPEC.md §6. Chunk on paragraph-then-sentence boundaries, never mid-sentence. Generate one full episode and let me listen before you go further.
>
> **Phase 5 — publish and schedule.** `src/publish.py` and the workflow YAML per SPEC.md §7. Both cron entries for DST, the local-time guard, NYSE holiday skip, 14-day pruning.
>
> Constraints throughout: Python 3.11+, no framework, standard library plus `yfinance`, `requests`, `anthropic`, `openai`, `pydub`, `pandas_market_calendars`. Log every API call's cost so I can track against a $25/month ceiling. Never hardcode a market figure anywhere, including in tests outside `tests/fixtures/`. If a design decision in SPEC.md turns out to be wrong once you're in the code, tell me rather than working around it silently.

### Why this order

The validator comes before the generator on purpose. If you build generation first you'll spend a week reading plausible output and assuming it's correct. Build the thing that catches lies before you build the thing that can tell them.

---

## 10. Acceptance tests before you trust it

Run for two weeks without acting on anything in it. Check:

1. **Spot-check five figures daily** against a terminal or brokerage. Any mismatch is a fetcher bug, not a model bug — find it in `fetch_data.py`.
2. **Kill a data source deliberately** (bad API key) and confirm the briefing says the data was unavailable rather than inventing it.
3. **Check a mover explanation** against the actual news. This is where fabrication is most likely, because "why did it move" is the one question with no deterministic source.
4. **Time it end to end.** If the pipeline exceeds ~8 minutes you'll miss 7am on slow days.
5. **Listen on AirPods while walking.** Density that reads fine on screen is often unfollowable in audio. Expect to cut the word budget after week one.

## 11. Known cost risks

| Risk | Mitigation |
|---|---|
| Pass 1 with web search is the dominant cost; token spend scales with search result volume | Cap searches per run (~15). Log per-run cost. Alert above $0.60/day. |
| Validator failures trigger regeneration, doubling cost that day | Cap at one retry, then fail loudly. |
| `yfinance` is unofficial and breaks when Yahoo changes endpoints | Finnhub free tier as fallback for quotes. Alert on fetch_errors, don't just log. |
| Free-tier rate limits (Finnhub, FMP) | One run/day is well inside limits — but don't loop retries without backoff. |

---

## Appendix A — Exact identifiers

Do not guess these. Symbol typos produce silent nulls, not errors.

### yfinance tickers

```python
FUTURES   = {"ES=F": "S&P 500 futures", "NQ=F": "Nasdaq 100 futures",
             "YM=F": "Dow futures",     "RTY=F": "Russell 2000 futures"}

INDICES   = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq Composite",
             "^DJI": "Dow Jones Industrial Average", "^RUT": "Russell 2000"}

VOL       = {"^VIX": "VIX", "^VIX3M": "3-month VIX"}

GLOBAL    = {"^N225": ("Nikkei 225", "Asia"),  "^HSI": ("Hang Seng", "Asia"),
             "000001.SS": ("Shanghai Composite", "Asia"),
             "^STOXX": ("Stoxx 600", "Europe"), "^GDAXI": ("DAX", "Europe"),
             "^FTSE": ("FTSE 100", "Europe")}

FX        = {"DX-Y.NYB": "Dollar index", "EURUSD=X": "Euro/dollar",
             "USDJPY=X": "Dollar/yen",   "GBPUSD=X": "Sterling/dollar",
             "USDCNY=X": "Dollar/yuan"}

COMMODS   = {"CL=F": "WTI crude", "BZ=F": "Brent crude", "GC=F": "Gold",
             "SI=F": "Silver",    "HG=F": "Copper",      "NG=F": "Natural gas"}

SECTORS   = {"XLK": "Technology", "XLF": "Financials",  "XLE": "Energy",
             "XLV": "Health care","XLI": "Industrials",  "XLY": "Consumer discretionary",
             "XLP": "Consumer staples", "XLU": "Utilities", "XLRE": "Real estate",
             "XLB": "Materials",  "XLC": "Communication services"}

# Compute technicals for these plus the top 5 pre-market movers
TECHNICALS_UNIVERSE = ["^GSPC", "^IXIC", "^RUT"]
```

### FRED series IDs

| ID | Series |
|---|---|
| `CPIAUCSL` | CPI, all items |
| `CPILFESL` | Core CPI |
| `PCEPILFE` | Core PCE price index |
| `UNRATE` | Unemployment rate |
| `PAYEMS` | Nonfarm payrolls |
| `ICSA` | Initial jobless claims |
| `DFF` | Effective fed funds rate |
| `GDPC1` | Real GDP |

Free API key: `fred.stlouisfed.org/docs/api/api_key.html`

### Endpoints that need verification before you rely on them

**These change. Verify each one in Phase 1 and report back what actually works — do not assume my URL shape is current.**

| Data | Starting point | Note |
|---|---|---|
| Daily Treasury yield curve | `home.treasury.gov` daily treasury rates, CSV/XML export | Query-string format has changed before. Confirm the current one and pin it. |
| Treasury auction schedule | `treasurydirect.gov` TA_WS securities endpoint, JSON | Verify the announced-vs-auctioned parameter. |
| Fed speaker calendar | `federalreserve.gov` calendar JSON feed | If unavailable, parse the press-release RSS instead. |
| Crypto | `api.coingecko.com/api/v3/simple/price` | Stable; `include_24hr_change=true`. |
| Earnings calendar | Finnhub `/calendar/earnings` | Confirm it's on the free tier. |
| Economic calendar | Finnhub `/calendar/economic` — **likely premium** | If gated, fall back to FMP's free tier (250 calls/day). Report which one works. |

If any of these can't be sourced free, say so rather than substituting a paid tier — I'll decide whether it's worth paying for.

---

## Appendix B — API integration details

### Anthropic (verified against current docs)

```python
# Pass 1 — research, with web search
response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=8000,
    messages=[{"role": "user", "content": research_prompt}],
    tools=[{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 15,                      # hard cost cap per run
        "user_location": {"type": "approximate", "city": "New York",
                          "region": "New York", "country": "US",
                          "timezone": "America/New_York"},
    }],
)

# Pass 2 — format transform, no tools
client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=4000, ...)
```

Web search is billed separately from tokens at **$10 per 1,000 searches** — `max_uses: 15` caps that at $0.15/run, ~$3.30/month. That's your main lever if costs run hot.

Available model strings: `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`. Use Sonnet for Pass 1; Opus is not worth the cost delta here. Note the response contains `server_tool_use` and `web_search_tool_result` blocks interleaved with text — extract text blocks by **type**, never by index position.

### OpenAI TTS

```python
client.audio.speech.create(
    model="gpt-4o-mini-tts",
    voice="ash",                     # A/B test ash / sage / cedar / marin first
    input=chunk,                     # ≤2,000 tokens — chunking is mandatory
    instructions=DELIVERY_INSTRUCTIONS,
    response_format="mp3",
)
```

### Cost logging

Every run appends one line to `data/costs.jsonl`: date, input tokens, output tokens, search count, TTS characters, computed dollar total. Print a rolling 30-day sum at the end of each run. Without this you won't know you've blown the budget until the invoice.

---

## Appendix C — Fixture mode (build this in Phase 1, not later)

Claude Code will run this pipeline dozens of times while building. At full cost that's real money, and market APIs will rate-limit you.

Required flags on every entry point:

- `--fixture` — load `tests/fixtures/market_data_sample.json` instead of hitting live APIs
- `--dry-run` — run generation and validation, skip TTS and publish entirely
- `--cache` — write each successful live fetch to `data/last_fetch.json`; reuse it if a fetch fails within the same run

**Default behavior during development is `--fixture --dry-run`.** Live fetches only when explicitly requested.

---

## Appendix D — Manual setup (my job, not Claude Code's)

Claude Code cannot do these. I'll do them; ask me to confirm each is done before the phase that needs it.

**Before Phase 1**
1. Create private GitHub repo, add `SPEC.md` and `CLAUDE.md`.
2. Get API keys: `console.anthropic.com` · `platform.openai.com` · `finnhub.io/register` · `fred.stlouisfed.org/docs/api/api_key.html`
3. **Set hard spend limits on both the Anthropic and OpenAI accounts.** A runaway loop with web search enabled is the one way this project gets genuinely expensive. Do this before the first live run, not after.
4. Local `.env` with all four keys. `.env` in `.gitignore` from the first commit.

**Before Phase 5**
5. Add the four keys as GitHub repo secrets (Settings → Secrets and variables → Actions).
6. Create an empty `gh-pages` branch; enable Pages on it (Settings → Pages).
7. Confirm Actions failure notifications are on (Settings → Notifications) — a silently dead cron is the likeliest failure mode of this whole system.

**After first successful publish**
8. Apple Podcasts → Library → Add a Show by URL → paste the feed URL.

---

## Appendix E — Session phase

The job only ever runs at ~6:15am ET on NYSE trading days, so `session_phase` is always `"pre-market"`. The original concept had the model detect pre-market vs intraday vs post-close; that's dead weight here. Write the prompts for pre-market and nothing else — no hedging about what session it might be, no branching logic.

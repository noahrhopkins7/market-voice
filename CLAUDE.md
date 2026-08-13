# CLAUDE.md

## What this is

A daily automated pre-market audio briefing, published as a private podcast feed. Runs at ~6:15am ET on NYSE trading days, produces a 10–12 minute MP3 plus a text version, and lands in Apple Podcasts before 7am.

**Read `SPEC.md` in full before writing code.** It is the authoritative design. This file is operational context only.

## The one invariant

**The language model never states a number it was not handed.**

All figures originate in `market_data.json`, fetched deterministically. The model synthesizes, ranks, and explains — it does not recall, calculate, derive, or estimate any quantity. `validate.py` enforces this mechanically.

If you find yourself writing code that lets a model-produced figure reach the output unchecked, stop and flag it. This constraint is the entire reason the project is worth building; a briefing with plausible invented numbers is worse than no briefing, because it gets acted on.

## Commands

```bash
# Development default — no API spend, no live calls
python -m src.fetch_data --fixture
python -m src.generate --fixture --dry-run
python -m src.validate data/script.txt data/market_data.json
pytest tests/

# Live single-stage runs
python -m src.fetch_data --cache          # real fetch, caches result
python -m src.generate                    # real API calls — costs money
python -m src.tts data/script.txt         # real API calls — costs money

# Full pipeline, local, no publish
python -m src.run --local

# Trigger the real workflow manually
gh workflow run brief.yml
```

## Conventions

- Python 3.11+. Standard library plus: `yfinance`, `requests`, `anthropic`, `openai`, `pydub`, `pandas_market_calendars`, `python-dotenv`. No framework, no ORM, no async unless a fetch genuinely needs it.
- Every data source is its own module in `src/sources/`, each with the same signature and each independently failable. One dead API degrades the briefing; it never kills the run.
- A failed source appends to `fetch_errors` in the JSON. Never let a failure pass silently, and never substitute a default value for missing market data.
- Every numeric field uses the `{"value": float, "display": str}` pattern from SPEC.md §2. `display` is pre-rounded at fetch time and is what the model reproduces verbatim.
- Prompts live in `src/prompts/*.txt`, loaded at runtime. Never inline a prompt in Python — I edit these frequently and want them diffable on their own.
- No market figure is ever hardcoded anywhere outside `tests/fixtures/`.
- Log every API call's token/search/character usage to `data/costs.jsonl`.

## Build order

Phases are in SPEC.md §9. Two rules about sequencing:

1. **`validate.py` and its tests come before `generate.py`.** Building generation first means weeks of reading fluent output while assuming it's correct.
2. **Stop at each phase checkpoint and show me real output.** Don't chain phases. I want to see the actual JSON, the actual script, and hear the actual audio before you build on top of them.

## Gotchas

- **GitHub Actions cron is UTC and ignores DST.** Two schedule entries (10:15 and 11:15 UTC), plus a guard inside the script that exits unless `America/New_York` local time is 06:00–06:59. Without the guard you publish twice daily for half the year.
- **Skip NYSE holidays** via `pandas_market_calendars`, not a hardcoded list.
- **`gpt-4o-mini-tts` caps at 2,000 input tokens.** A 1,750-word script is ~2,750. Chunk on paragraph-then-sentence boundaries — splitting mid-sentence produces an audible prosody break at the seam.
- **`yfinance` is unofficial** and breaks when Yahoo changes endpoints. Wrap it, alert on failure, keep Finnhub as the quote fallback.
- **Anthropic responses with web search enabled** contain `server_tool_use` and `web_search_tool_result` blocks interleaved with text. Extract text blocks by `type`, never by index.
- **Validator false positives** are expected in week one, from legitimate phrasing like "three developments stand out." Log every rejection rather than loosening the rules preemptively.

## Decide alone vs. ask me

**Decide alone:** file layout, function decomposition, error-handling style, test structure, logging format, retry/backoff details, anything about how the code is organized.

**Ask me:**
- Any change that would let an unvalidated figure reach the output.
- Any paid tier or new paid dependency — the ceiling is $25/month all-in.
- Any change to the prompts in `src/prompts/`. Propose the diff and the reasoning; don't just edit them.
- Any endpoint from SPEC.md Appendix A that turns out to be dead or gated. Report what you found rather than silently substituting a source.
- Anything in SPEC.md that turns out to be wrong once you're in the code. Tell me — don't work around it silently.

## Style

Direct and technical. Tell me when something won't work rather than building a workaround for a bad idea. If a design decision here is wrong, say so plainly and explain why.

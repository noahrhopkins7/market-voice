"""Pass 1 (research) and Pass 2 (spoken script).

    python -m src.generate --fixture --dry-run    # fixture data, still costs API money
    python -m src.generate                        # real API calls

Both prompts live in src/prompts/*.txt and are loaded at runtime — CLAUDE.md
forbids inlining them, because they get edited constantly and should diff on
their own.

Every call appends one line to data/costs.jsonl. Without that you find out you
blew the $25/month ceiling when the invoice arrives.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("generate")

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = Path(__file__).resolve().parent / "prompts"
DATA_DIR = ROOT / "data"
COSTS_PATH = DATA_DIR / "costs.jsonl"

# Verified against current model docs 2026-08-12.
#   Pass 1 needs judgment and web search -> Sonnet 5.
#   Pass 2 is a format transform, not analysis -> Haiku.
RESEARCH_MODEL = "claude-sonnet-5"
SCRIPT_MODEL = "claude-haiku-4-5"

# SPEC.md Appendix B pins web_search_20250305. The current variant for Sonnet 5
# is web_search_20260209, which filters results before they reach the context
# window — same price per search, fewer tokens carried. See notes in README of
# this change when reviewing.
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
MAX_SEARCHES = 15  # hard cost cap: 15 x $10/1000 = $0.15/run

# $ per million tokens. Sonnet 5 is on introductory pricing through 2026-08-31
# ($2/$10); it reverts to $3/$15 after that.
PRICING = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
SEARCH_COST_PER_1K = 10.00

# Raised from SPEC.md's $25 on 2026-08-12: a measured run costs ~$0.88 at
# Sonnet 5's introductory rate and ~$1.32 once that ends on 2026-08-31, which
# is ~$28/month for generation plus ~$5 for TTS.
BUDGET_CEILING = 30.00
# ~21 trading days of generation inside that ceiling, leaving room for TTS.
DAILY_ALERT = 1.20

# Pass 1 must hold thinking + prose. Sonnet 5 runs adaptive thinking by default
# and max_tokens caps the two together, so this is deliberately generous.
RESEARCH_MAX_TOKENS = 16000

# Pass 1 runs web search server-side. When that loop hits its iteration limit
# the API returns stop_reason "pause_turn" with the turn unfinished — often with
# no text block at all — and expects the conversation to be re-sent to continue.
# Not handling this is what killed every CI run between 2026-08-14 and 08-18:
# ~250s of searching, then "Pass 1 returned no text blocks" and exit 1.
#
# Bounded because each continuation may search again: a runaway loop is the one
# way this gets expensive.
MAX_CONTINUATIONS = 3
SCRIPT_MAX_TOKENS = 4000


@dataclass
class Usage:
    """What one API call cost."""
    model: str
    pass_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    searches: int = 0

    def add(self, response, model: str) -> None:
        """Fold another response's usage in, for multi-round turns."""
        usage = getattr(response, "usage", None)
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.searches += _count_searches(response)

    def dollars(self) -> float:
        rates = PRICING.get(self.model)
        if rates is None:
            return 0.0
        return (
            self.input_tokens / 1_000_000 * rates["input"]
            + self.output_tokens / 1_000_000 * rates["output"]
            + self.searches / 1_000 * SEARCH_COST_PER_1K
        )


@dataclass
class Result:
    report: str = ""
    script: str = ""
    usages: list[Usage] = field(default_factory=list)

    def total(self) -> float:
        return sum(u.dollars() for u in self.usages)


# --------------------------------------------------------------------------

def load_prompt(name: str) -> str:
    path = PROMPTS / f"{name}.txt"
    if not path.exists():
        raise SystemExit(f"prompt not found: {path}")
    return path.read_text()


def _text_of(response) -> str:
    """Join the text blocks of a response.

    CLAUDE.md gotcha: with web search enabled the response interleaves
    server_tool_use and web_search_tool_result blocks with the text. Select by
    block .type, never by index — content[0] is frequently a tool block.
    """
    return "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


def _count_searches(response) -> int:
    return sum(
        1 for block in response.content
        if getattr(block, "type", None) == "server_tool_use"
        and getattr(block, "name", None) == "web_search"
    )


def _usage_of(response, model: str, pass_name: str) -> Usage:
    usage = getattr(response, "usage", None)
    return Usage(
        model=model,
        pass_name=pass_name,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        searches=_count_searches(response),
    )


# --------------------------------------------------------------------------

def run_research(client, market_data: dict) -> tuple[str, Usage]:
    """Pass 1 — analysis with web search. The expensive call.

    Loops on stop_reason "pause_turn": the server-side search loop pauses when
    it hits its iteration cap, and the turn only completes once the
    conversation is re-sent. See MAX_CONTINUATIONS above.
    """
    prompt = load_prompt("research").replace(
        "{market_data_json}", json.dumps(market_data, indent=2)
    )
    tools = [{
        "type": WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": MAX_SEARCHES,
        "user_location": {
            "type": "approximate",
            "city": "New York",
            "region": "New York",
            "country": "US",
            "timezone": "America/New_York",
        },
    }]

    messages = [{"role": "user", "content": prompt}]
    usage = Usage(model=RESEARCH_MODEL, pass_name="research")
    parts: list[str] = []
    response = None

    for round_number in range(MAX_CONTINUATIONS + 1):
        response = client.messages.create(
            model=RESEARCH_MODEL,
            max_tokens=RESEARCH_MAX_TOKENS,
            messages=messages,
            tools=tools,
        )
        usage.add(response, RESEARCH_MODEL)

        if response.stop_reason == "refusal":
            raise SystemExit(
                "Pass 1 was refused by safety classifiers; nothing published.")

        chunk = _text_of(response)
        if chunk:
            parts.append(chunk)

        if response.stop_reason != "pause_turn":
            break

        # Re-send with the paused turn appended; the server resumes from there.
        messages = messages + [{"role": "assistant", "content": response.content}]
        log.info("Pass 1 paused after search round %d — continuing (%d searches so far)",
                 round_number + 1, usage.searches)
    else:
        raise SystemExit(
            f"Pass 1 still paused after {MAX_CONTINUATIONS} continuations "
            f"and {usage.searches} searches; nothing published.")

    report = "\n".join(parts).strip()
    if not report:
        raise SystemExit(
            f"Pass 1 produced no text (stop_reason={response.stop_reason!r}, "
            f"{usage.searches} searches, {usage.output_tokens} output tokens).")
    return report, usage


def run_script(client, report: str) -> tuple[str, Usage]:
    """Pass 2 — format transform. No tools, cheap model."""
    prompt = load_prompt("script").replace("{report_markdown}", report)

    response = client.messages.create(
        model=SCRIPT_MODEL,
        max_tokens=SCRIPT_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    script = _text_of(response)
    if not script:
        raise SystemExit("Pass 2 returned no text blocks.")
    return script, _usage_of(response, SCRIPT_MODEL, "script")


# --------------------------------------------------------------------------

def log_costs(result: Result, trading_day: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with COSTS_PATH.open("a") as handle:
        for usage in result.usages:
            handle.write(json.dumps({
                "logged_at_utc": stamp,
                "trading_day": trading_day,
                "pass": usage.pass_name,
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "searches": usage.searches,
                "dollars": round(usage.dollars(), 4),
            }) + "\n")


def rolling_30d_total() -> float:
    if not COSTS_PATH.exists():
        return 0.0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    total = 0.0
    for line in COSTS_PATH.read_text().splitlines():
        try:
            row = json.loads(line)
            when = dt.datetime.strptime(
                row["logged_at_utc"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
        except (ValueError, KeyError):
            continue
        if when >= cutoff:
            total += row.get("dollars", 0.0)
    return total


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.generate",
        description="Generate report.md and script.txt from market_data.json",
    )
    parser.add_argument("--fixture", action="store_true",
                        help="use tests/fixtures/market_data_sample.json as input")
    parser.add_argument("--dry-run", action="store_true",
                        help="print outputs instead of writing them")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "API_KEYS.env")

    from .fetch_data import load_fixture, OUTPUT_PATH

    if args.fixture:
        market_data = load_fixture()
    elif OUTPUT_PATH.exists():
        market_data = json.loads(OUTPUT_PATH.read_text())
    else:
        raise SystemExit(
            f"no market data at {OUTPUT_PATH}\n"
            "Run: python -m src.fetch_data --cache"
        )

    import anthropic

    client = anthropic.Anthropic()
    result = Result()

    log.info("Pass 1 (%s, web search up to %d)...", RESEARCH_MODEL, MAX_SEARCHES)
    result.report, research_usage = run_research(client, market_data)
    result.usages.append(research_usage)

    log.info("Pass 2 (%s)...", SCRIPT_MODEL)
    result.script, script_usage = run_script(client, result.report)
    result.usages.append(script_usage)

    trading_day = market_data.get("meta", {}).get("trading_day", "unknown")
    log_costs(result, trading_day)

    if args.dry_run:
        print("=" * 70 + "\nREPORT\n" + "=" * 70)
        print(result.report)
        print("\n" + "=" * 70 + "\nSCRIPT\n" + "=" * 70)
        print(result.script)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "report.md").write_text(result.report + "\n")
        (DATA_DIR / "script.txt").write_text(result.script + "\n")
        print(f"wrote {DATA_DIR / 'report.md'} and {DATA_DIR / 'script.txt'}",
              file=sys.stderr)

    spent, rolling = result.total(), rolling_30d_total()
    print(
        f"\nthis run ${spent:.4f}  ({research_usage.searches} searches)  |  "
        f"rolling 30d ${rolling:.2f} of ${BUDGET_CEILING:.2f}",
        file=sys.stderr,
    )
    if spent > DAILY_ALERT:
        print(f"ALERT: run cost ${spent:.2f} exceeds the ${DAILY_ALERT:.2f}/day budget",
              file=sys.stderr)
    if rolling > BUDGET_CEILING:
        print(f"ALERT: 30-day spend ${rolling:.2f} is over the ${BUDGET_CEILING:.2f} ceiling",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

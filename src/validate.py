"""Reject any script figure that did not come from market_data.json.

    python -m src.validate data/script.txt data/market_data.json

This is the mechanical enforcement of the one invariant: the language model
never states a number it was not handed. Prompt instructions are not
enforcement — this is.

How it works (SPEC.md §5)
-------------------------
1. Build ``allowed`` from every ``display`` string in market_data.json, plus
   calendar dates and times, plus a whitelist of years, small list-counts and
   number words.
2. Extract every figure-shaped token from the script.
3. Any extracted token not in ``allowed`` is a violation.

Normalisation, and why it stops short of being clever
-----------------------------------------------------
Tokens and display strings are both normalised before comparison: commas and
spaces dropped, a leading ``+`` dropped, ``basis points``/``bps`` unified to
``bp``, and trailing zeros canonicalised so ``6,390.50`` and ``6,390.5`` agree.

Normalisation never changes a *value*. ``4.32%`` and ``4.3%`` stay different,
``6,412.25`` and ``6,412`` stay different. Rounding is exactly what the model is
forbidden to do, so the validator must keep catching it. What normalisation
removes is cosmetic-only variation, which would otherwise generate false
failures on correct output.

Pass 2 (SPEC.md §4) rewrites ``bps`` as ``basis points`` for the ear, so the
validator has to understand both spellings or it fails every correct script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REJECTION_LOG = ROOT / "data" / "validation_rejections.jsonl"

# SPEC.md §5 whitelist.
WHITELIST_YEARS = range(1900, 2101)
WHITELIST_MAX_COUNT = 20  # "three developments stand out", list ordinals
NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty",
}

TOKEN_RE = re.compile(
    r"""
      (?P<time>\b\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)
    | (?P<currency_mag>\$\s?\d[\d,]*(?:\.\d+)?\s*(?:trillion|billion|million))
    | (?P<currency>\$\s?\d[\d,]*(?:\.\d+)?)
    | (?P<percent>[+-]?\d[\d,]*(?:\.\d+)?\s*%)
    | (?P<bps>[+-]?\d[\d,]*(?:\.\d+)?\s*(?:bps|bp|basis\ points?)\b)
    | (?P<multiple>[+-]?\d[\d,]*(?:\.\d+)?\s*x\b)
    | (?P<decimal>[+-]?\d[\d,]*\.\d+)
    | (?P<integer>[+-]?\d[\d,]*)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_NUMBER_IN_TEXT = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Figure:
    """One figure-shaped token found in the script."""
    raw: str
    normalized: str
    kind: str
    line: int
    context: str


@dataclass
class Result:
    ok: bool
    violations: list[Figure] = field(default_factory=list)
    checked: int = 0
    allowed_count: int = 0

    def report(self) -> str:
        if self.ok:
            return f"PASS — {self.checked} figures checked, all traceable to market_data.json"
        lines = [
            f"FAIL — {len(self.violations)} of {self.checked} figures not found in market_data.json",
            "",
        ]
        for violation in self.violations:
            lines.append(f"  line {violation.line}: {violation.raw!r}  ({violation.kind})")
            lines.append(f"    {violation.context}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def _canonical_numbers(text: str) -> str:
    """Canonicalise trailing zeros: ``6390.50`` -> ``6390.5``, ``4.0`` -> ``4``."""
    def replace(match: re.Match) -> str:
        try:
            return format(Decimal(match.group()).normalize(), "f")
        except InvalidOperation:
            return match.group()
    return _NUMBER_IN_TEXT.sub(replace, text)


def normalize(raw: str) -> str:
    """Fold cosmetic variation. Never changes a numeric value."""
    text = raw.strip().lower()
    text = re.sub(r"\bbasis\s+points?\b", "bp", text)
    text = re.sub(r"\bbps\b", "bp", text)
    text = text.replace(",", "").replace(" ", "")
    text = text.lstrip("+")
    return _canonical_numbers(text)


def numeric_core(raw: str) -> str | None:
    """The bare number inside a display string: ``+46 bps`` -> ``46``.

    Lets the script mention a figure in a different frame than the data stored
    it in (``3.2x average`` spoken as ``3.2 times average``) without inventing
    a value — the number itself must still exist in market_data.json.
    """
    match = _NUMBER_IN_TEXT.search(raw.replace(",", ""))
    if not match:
        return None
    try:
        return format(Decimal(match.group()).normalize(), "f").lstrip("+")
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------
# allowed set
# --------------------------------------------------------------------------

def collect_allowed(market_data: dict) -> set[str]:
    """Every value the script is permitted to state."""
    allowed: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            display = node.get("display")
            if isinstance(display, str):
                allowed.add(normalize(display))
                core = numeric_core(display)
                if core:
                    allowed.add(core)
                    # Also admit the unsigned magnitude. The data stores
                    # "-4.8%"; a script written for the ear says "down 4.8%",
                    # carrying the sign as a word. Requiring a literal "-"
                    # would reject almost every correct script.
                    #
                    # The tradeoff, stated plainly: this validator checks
                    # magnitudes, not directions. A model that says "up 4.8%"
                    # when the tape was down 4.8% is not caught here. That is a
                    # wrong *word*, not an invented *number* — but it is a real
                    # gap, and the Pass 1 prompt is what has to close it.
                    allowed.add(core.lstrip("-"))
            for key, value in node.items():
                if key in ("date", "time_et", "as_of", "next_release",
                           "curve_date", "trading_day", "prior_session"):
                    if isinstance(value, str):
                        _add_date_or_time(allowed, value)
                # Numbers the data carries inside strings rather than as
                # figures: tenor "30Y" spoken as "30-year", topic "Meeting of
                # July 28-29" spoken as "July 28 through 29". These are real
                # data the script may state; without them the validator
                # rejects correct output every single day.
                if key in ("tenor", "topic", "event", "security_type",
                           "series", "session") and isinstance(value, str):
                    allowed.update(re.findall(r"\d+", value))
                # Numbers carried in field *names*: sma200 -> "200-day moving
                # average", week52_high -> "52-week high", change_pct_24h ->
                # "over 24 hours".
                allowed.update(re.findall(r"\d+", key))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(market_data)

    for year in WHITELIST_YEARS:
        allowed.add(str(year))
    for count in range(0, WHITELIST_MAX_COUNT + 1):
        allowed.add(str(count))
    allowed |= NUMBER_WORDS
    return allowed


def _add_date_or_time(allowed: set[str], value: str) -> None:
    """Admit calendar dates and times in the forms a narrator would speak."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        year, month, day = value.split("-")
        allowed.update({value, str(int(day)), str(int(year))})
    elif re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = value.split(":")
        allowed.update({value, f"{int(hour)}:{minute}"})


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def entity_names(market_data: dict) -> list[str]:
    """Proper nouns containing digits: "S&P 500", "Russell 2000", "Nikkei 225".

    These are names, not figures. Without masking them the validator reads the
    500 in "S&P 500" as an unsourced number and rejects every correct script.
    Collected from the data itself rather than hardcoded, so a new index picked
    up by a source is handled automatically.
    """
    names: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("name", "series", "event", "speaker", "topic") and isinstance(value, str):
                    if any(char.isdigit() for char in value):
                        names.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(market_data)

    # A script rarely quotes a name in full. The data says "Nasdaq 100 futures";
    # a narrator says "the Nasdaq 100". Without the shorter form the 100 is
    # extracted as an unsourced figure — which is exactly what broke the
    # 2026-08-24 briefing. So also emit every prefix ending on a digit-bearing
    # token: "S&P 500 futures" -> "S&P 500", "Russell 2000 futures" ->
    # "Russell 2000".
    expanded: set[str] = set()
    for name in names:
        expanded.add(name)
        tokens = name.split()
        for index, token in enumerate(tokens):
            if any(character.isdigit() for character in token):
                expanded.add(" ".join(tokens[:index + 1]))

    # Longest first, so "S&P 500 futures" masks before "S&P 500".
    return sorted(expanded, key=len, reverse=True)


@lru_cache(maxsize=512)
def _name_pattern(name: str) -> re.Pattern:
    """Match a name tolerating spacing and hyphenation: "Nasdaq-100" too."""
    parts = [re.escape(token) for token in name.split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)", re.IGNORECASE)


def mask_entities(line: str, names: list[str]) -> str:
    """Blank the digits inside entity names, preserving length and offsets."""
    for name in names:
        if not name:
            continue
        line = _name_pattern(name).sub(
            lambda match: re.sub(r"\d", "#", match.group()), line)
    return line


def extract_figures(script: str, names: list[str] | None = None) -> list[Figure]:
    figures = []
    for line_number, line in enumerate(script.splitlines(), start=1):
        if names:
            line = mask_entities(line, names)
        for match in TOKEN_RE.finditer(line):
            kind = match.lastgroup or "unknown"
            raw = match.group().strip()
            figures.append(Figure(
                raw=raw,
                normalized=normalize(raw),
                kind=kind,
                line=line_number,
                context=_context(line, match),
            ))
    return figures


def _context(line: str, match: re.Match) -> str:
    start, end = max(0, match.start() - 40), min(len(line), match.end() + 40)
    return ("…" if start else "") + line[start:end].strip() + ("…" if end < len(line) else "")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(script: str, market_data: dict) -> Result:
    allowed = collect_allowed(market_data)
    figures = extract_figures(script, entity_names(market_data))

    violations = []
    for figure in figures:
        if _is_allowed(figure, allowed):
            continue
        violations.append(figure)

    return Result(
        ok=not violations,
        violations=violations,
        checked=len(figures),
        allowed_count=len(allowed),
    )


def _is_allowed(figure: Figure, allowed: set[str]) -> bool:
    # SPEC.md §3 rule 4 permits clock times outright.
    if figure.kind == "time":
        return True

    if figure.normalized in allowed:
        return True

    core = numeric_core(figure.raw)
    if core is None:
        return True  # nothing numeric to verify

    # Small integers are list counts ("three developments"), not market figures.
    if figure.kind == "integer":
        try:
            magnitude = abs(Decimal(core))
        except InvalidOperation:
            magnitude = None
        if magnitude is not None and magnitude <= WHITELIST_MAX_COUNT:
            return True

    return core in allowed


# --------------------------------------------------------------------------
# rejection log — CLAUDE.md: log every rejection rather than loosening rules
# --------------------------------------------------------------------------

def log_rejections(result: Result, script_path: Path) -> None:
    if result.ok:
        return
    REJECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with REJECTION_LOG.open("a") as handle:
        for violation in result.violations:
            handle.write(json.dumps({
                "logged_at_utc": stamp,
                "script": str(script_path),
                "token": violation.raw,
                "normalized": violation.normalized,
                "kind": violation.kind,
                "line": violation.line,
                "context": violation.context,
            }) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.validate",
        description="Verify every figure in a script exists in market_data.json",
    )
    parser.add_argument("script", type=Path)
    parser.add_argument("market_data", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    script = args.script.read_text()
    market_data = json.loads(args.market_data.read_text())

    result = validate(script, market_data)
    log_rejections(result, args.script)

    if not args.quiet:
        print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

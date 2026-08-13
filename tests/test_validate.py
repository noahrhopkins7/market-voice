"""Tests for the figure validator.

The four cases SPEC.md §9 Phase 2 asks for:

  * a clean script                          -> passes
  * a script with one fabricated percentage -> fails, and names that figure
  * a script with a differently-rounded figure -> fails (rounding is forbidden)
  * a script with legitimate list-count integers -> passes (no false positive)

Every market figure used here lives in tests/fixtures/, never in this file —
CLAUDE.md forbids hardcoded figures outside the fixtures directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.validate import (
    entity_names,
    collect_allowed,
    extract_figures,
    normalize,
    numeric_core,
    validate,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def market_data() -> dict:
    return json.loads((FIXTURES / "market_data_sample.json").read_text())


def script(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


def raw_tokens(result) -> list[str]:
    return [violation.raw for violation in result.violations]


# --------------------------------------------------------------------------
# the four required cases
# --------------------------------------------------------------------------

def test_clean_script_passes(market_data):
    result = validate(script("script_clean"), market_data)
    assert result.ok, f"clean script rejected: {raw_tokens(result)}"
    assert result.checked > 15, "suspiciously few figures extracted"


def test_fabricated_percentage_is_caught(market_data):
    result = validate(script("script_fabricated_pct"), market_data)
    assert not result.ok
    assert any("42.7" in token for token in raw_tokens(result)), raw_tokens(result)


def test_fabricated_script_flags_only_the_invention(market_data):
    """The real figures around the fabrication must not be collateral damage."""
    result = validate(script("script_fabricated_pct"), market_data)
    assert len(result.violations) == 1, raw_tokens(result)


def test_differently_rounded_figures_are_caught(market_data):
    result = validate(script("script_rounded"), market_data)
    assert not result.ok
    flagged = " ".join(raw_tokens(result))
    # 7,749 for 7,748.50 / 4.7% for 4.68% / 14.6 for 14.55 / 50 bps for 48 bps
    for rounded in ("7,749", "4.7%", "14.6"):
        assert rounded in flagged, f"{rounded} not flagged; got {raw_tokens(result)}"


def test_list_counts_do_not_trip_the_validator(market_data):
    """"Three developments", ordinals, years and clock times are legitimate."""
    result = validate(script("script_list_counts"), market_data)
    assert result.ok, f"false positives: {raw_tokens(result)}"


# --------------------------------------------------------------------------
# normalisation: folds format, never value
# --------------------------------------------------------------------------

@pytest.mark.parametrize("left,right", [
    ("+0.26%", "0.26%"),        # leading plus is cosmetic
    ("6,412.25", "6412.25"),    # comma grouping is cosmetic
    ("6,390.50", "6390.5"),     # trailing zero is cosmetic
    ("-3 bps", "-3 basis points"),   # Pass 2 rewrites bps for the ear
    ("+46 bps", "46bp"),
])
def test_normalization_folds_cosmetic_variation(left, right):
    assert normalize(left) == normalize(right)


@pytest.mark.parametrize("left,right", [
    ("4.32%", "4.3%"),          # rounding changes the value
    ("6,412.25", "6,412"),
    ("+3.1%", "-3.1%"),         # sign is meaning, not decoration
    ("14.55", "14.6"),
])
def test_normalization_preserves_value_differences(left, right):
    assert normalize(left) != normalize(right)


def test_numeric_core_extracts_the_bare_number():
    assert numeric_core("+46 bps") == "46"
    assert numeric_core("$182.40") == "182.4"
    assert numeric_core("3.2x average") == "3.2"
    assert numeric_core("unchanged") is None


# --------------------------------------------------------------------------
# allowed set and extraction
# --------------------------------------------------------------------------

def test_allowed_set_includes_every_display_string(market_data):
    allowed = collect_allowed(market_data)
    displays = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("display"), str):
                displays.append(node["display"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(market_data)
    assert displays, "fixture contains no display strings"
    missing = [d for d in displays if normalize(d) not in allowed]
    assert not missing, f"display strings absent from allowed set: {missing[:5]}"


def test_extraction_finds_each_figure_shape():
    found = extract_figures(
        "The ten-year is 4.68%, down 2 basis points, with $25 billion at 1:00 p.m. "
        "and 3.3x average volume on 7,748.50."
    )
    kinds = {figure.kind for figure in found}
    assert {"percent", "bps", "currency_mag", "time", "multiple", "decimal"} <= kinds, kinds


def test_index_names_are_not_read_as_figures(market_data):
    """"S&P 500" is a name. The 500 must not be treated as an unsourced number."""
    names = entity_names(market_data)
    assert any("500" in name for name in names), "no digit-bearing index name found"
    result = validate(
        "The S&P 500 and the Russell 2000 both closed higher.", market_data
    )
    assert result.ok, raw_tokens(result)


def test_direction_expressed_as_a_word_is_accepted(market_data):
    """Data holds "-4.8%"; a spoken script says "down 4.8%"."""
    allowed = collect_allowed(market_data)
    assert normalize("4.8%") in allowed or "4.8" in allowed


def test_magnitude_still_has_to_match(market_data):
    """Accepting an unsigned magnitude must not accept a *different* magnitude.

    Note the probe value: an earlier draft of this test used 4.9%, which is a
    real move in the fixture (Micron), so it passed validation correctly and
    the test was wrong. That is worth remembering — see
    test_membership_is_existence_not_correctness below.
    """
    result = validate("Volatility fell 87.6% on the session.", market_data)
    assert not result.ok


def test_membership_is_existence_not_correctness(market_data):
    """The known ceiling on what this validator can prove.

    ``allowed`` is a flat set of every figure in the data — around 570 entries
    on a normal day. Membership therefore proves a number appears *somewhere*
    in market_data.json, not that it is the right number for the sentence it
    sits in. Attaching a real figure to the wrong instrument passes.

    This is inherent to set-membership checking and is not a bug to fix here;
    catching it would need claim-level binding of figure to subject. Recorded
    as a test so the limitation stays visible rather than being rediscovered
    as a surprise in production.
    """
    # Read gold's price out of the fixture rather than hardcoding it, so
    # regenerating market_data_sample.json cannot break this test.
    gold = next(c for c in market_data["commodities"] if c["symbol"] == "GC=F")
    borrowed = validate(f"The VIX closed at {gold['last']['display']}.", market_data)
    assert borrowed.ok, "a figure borrowed from another instrument was expected to pass"


def test_numbers_carried_in_strings_and_field_names_are_allowed(market_data):
    """Real week-one false positives from the first generated script.

    Each of these is genuine data the script may state, but the number lives
    inside a string ("30Y", "Meeting of July 28-29") or a field name
    (change_pct_24h, week52_high, sma200) rather than in a display figure.
    Before the fix these four rejected an otherwise-perfect 133-figure script.
    """
    result = validate(
        "Treasury sells 30-year paper. The July 28 through 29 meeting minutes "
        "land soon. Bitcoin fell over 24 hours. The index sits above its "
        "200-day moving average, below the 52-week high.",
        market_data,
    )
    assert result.ok, raw_tokens(result)


def test_string_number_allowance_does_not_admit_fabrications(market_data):
    """The widening above must not become a general amnesty for integers."""
    result = validate("Some 87 percent of constituents advanced.", market_data)
    assert not result.ok


def test_empty_script_passes_vacuously(market_data):
    result = validate("", market_data)
    assert result.ok and result.checked == 0


def test_bare_ticker_prices_are_still_checked(market_data):
    """A figure-shaped token in prose is checked even without a % or $ marker."""
    result = validate("The index printed 9,999.99 overnight.", market_data)
    assert not result.ok

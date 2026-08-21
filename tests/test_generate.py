"""Tests for Pass 1's handling of a paused server-side search loop.

Every scheduled run between 2026-08-14 and 2026-08-18 failed here: web search
paused the turn, the response carried no text block, and run_research raised
"Pass 1 returned no text blocks" after ~250 seconds of searching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import generate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def market_data() -> dict:
    return json.loads((FIXTURES / "market_data_sample.json").read_text())


class _Block:
    def __init__(self, type_, text=None, name=None):
        self.type, self.text, self.name = type_, text, name


class _Usage:
    input_tokens = 1000
    output_tokens = 200
    cache_read_input_tokens = 0


class _Response:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content, self.usage = stop_reason, content, _Usage()


class _Client:
    """Replays a scripted list of responses and records how often it was called."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        self.last_messages = kwargs["messages"]
        return self._responses.pop(0)


SEARCH = _Block("server_tool_use", name="web_search")


def test_paused_search_loop_is_continued(market_data):
    """The exact production failure: a pause with no text, then completion."""
    client = _Client([
        _Response("pause_turn", [SEARCH, SEARCH]),          # no text at all
        _Response("end_turn", [SEARCH, _Block("text", "THE BRIEFING")]),
    ])
    report, usage = generate.run_research(client, market_data)
    assert report == "THE BRIEFING"
    assert client.calls == 2, "should have re-sent the paused turn"
    assert usage.searches == 3, "searches accumulate across rounds"


def test_continuation_resends_the_paused_assistant_turn(market_data):
    """The server resumes only if the paused turn is echoed back."""
    paused = _Response("pause_turn", [SEARCH])
    client = _Client([paused, _Response("end_turn", [_Block("text", "ok")])])
    generate.run_research(client, market_data)
    assert client.last_messages[-1]["role"] == "assistant"
    assert client.last_messages[-1]["content"] is paused.content


def test_partial_text_across_rounds_is_joined(market_data):
    client = _Client([
        _Response("pause_turn", [_Block("text", "first half")]),
        _Response("end_turn", [_Block("text", "second half")]),
    ])
    report, _ = generate.run_research(client, market_data)
    assert report == "first half\nsecond half"


def test_endless_pausing_gives_up_rather_than_looping(market_data):
    """A runaway continuation loop is the one way this gets expensive."""
    client = _Client([_Response("pause_turn", [SEARCH])] * 10)
    with pytest.raises(SystemExit) as excinfo:
        generate.run_research(client, market_data)
    assert "still paused" in str(excinfo.value)
    assert client.calls == generate.MAX_CONTINUATIONS + 1


def test_empty_response_reports_the_stop_reason(market_data):
    """The old message named no cause, which is why this took a week to find."""
    client = _Client([_Response("end_turn", [SEARCH])])
    with pytest.raises(SystemExit) as excinfo:
        generate.run_research(client, market_data)
    assert "end_turn" in str(excinfo.value)


def test_refusal_still_aborts(market_data):
    client = _Client([_Response("refusal", [])])
    with pytest.raises(SystemExit) as excinfo:
        generate.run_research(client, market_data)
    assert "refused" in str(excinfo.value)

"""Fed calendar — federalreserve.gov/json/calendar.json.

Verified live 2026-08-12. Free, no key. 538KB, 2,579 events.

READ THIS BEFORE TRUSTING ``fed_speakers``
------------------------------------------
The endpoint works, but it does not contain what SPEC.md §2 assumes. Filtering
to Speeches/Testimony/FOMC on 2026-08-12 gave 774 events, of which only 10 were
today-or-future — and all 10 were FOMC meetings and minutes running out to
December. There were **zero forward-dated speeches**; the most recent speech
was four days in the past. The Fed adds individual speeches to this feed with
little or no lead time.

So this source is reliable as an FOMC calendar and as a retrospective record of
what officials just said. It is not a forward speaker calendar, and
``fed_speakers`` will often contain only FOMC events. That is reported honestly
rather than padded — an empty list is a true statement about the world.

Parsing gotchas, all confirmed live:
  * UTF-8 BOM on the response  -> decode as utf-8-sig
  * date split across ``month`` ("2026-08") and ``days`` ("8", sometimes multi)
  * ``time`` is a display string ("12:45 p.m."), not 24-hour
  * fields carry HTML entities ("CEO &amp; Senior Management")
  * ``type: "Stat"`` is 1,059 statistical releases (H.15, H.4.1, commercial
    paper) — excluded as noise
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re

from .common import SourceError, get_text

NAME = "fed_calendar"
IMPACT = "Fed speaker and FOMC calendar unavailable"

URL = "https://www.federalreserve.gov/json/calendar.json"

WANTED_TYPES = {"Speeches", "Testimony", "FOMC"}
FORWARD_DAYS = 14


def fetch(ctx) -> dict:
    """Return ``{"fed_speakers": [...]}`` covering the next two weeks."""
    raw = get_text(URL, impact=IMPACT)

    try:
        # utf-8-sig strips the BOM; requests may have already decoded it as
        # a leading ﻿, so handle both.
        data = json.loads(raw.lstrip("﻿"))
    except ValueError as exc:
        raise SourceError(f"could not parse Fed calendar JSON: {exc}", IMPACT) from exc

    events = data.get("events")
    if not isinstance(events, list):
        raise SourceError("Fed calendar JSON had no 'events' array", IMPACT)

    horizon = ctx.trading_day + dt.timedelta(days=FORWARD_DAYS)
    seen: set[tuple] = set()
    out = []

    for event in events:
        if event.get("type") not in WANTED_TYPES:
            continue
        when = _event_date(event)
        if when is None or not (ctx.trading_day <= when <= horizon):
            continue

        speaker, topic = _speaker_and_topic(event)
        key = (when, speaker, topic)
        if key in seen:
            continue
        seen.add(key)

        entry = {"date": when.isoformat(), "speaker": speaker, "topic": topic}
        time_et = _time_24h(event.get("time"))
        if time_et:
            entry["time_et"] = time_et
        out.append(entry)

    out.sort(key=lambda e: (e["date"], e.get("time_et", "")))
    return {"fed_speakers": out}


def _event_date(event: dict) -> dt.date | None:
    """Recombine ``month`` ("2026-08") and ``days`` ("8" / "29,30") into a date."""
    month = event.get("month") or ""
    match = re.match(r"(\d{4})-(\d{2})", month)
    if not match:
        return None
    days = re.findall(r"\d+", str(event.get("days", "")))
    if not days:
        return None
    try:
        # Multi-day events (conferences) are dated from their first day.
        return dt.date(int(match.group(1)), int(match.group(2)), int(days[0]))
    except ValueError:
        return None


def _speaker_and_topic(event: dict) -> tuple[str, str]:
    """Split ``"Speech - Governor Lisa D. Cook"`` into speaker and topic."""
    title = _clean(event.get("title"))
    description = _clean(event.get("description"))

    if " - " in title:
        kind, _, who = title.partition(" - ")
        return who.strip(), (description or kind.strip())
    # FOMC entries have no dash: "FOMC Meeting", "FOMC Minutes".
    return (title or "Federal Reserve"), (description or title)


def _clean(value: str | None) -> str:
    """Unescape *before* stripping tags.

    The feed double-encodes: descriptions arrive as "&lt;p&gt;Meeting of
    July 28-29&lt;/p&gt;". Stripping tags first finds nothing to strip, and the
    later unescape then resurrects literal <p> markup into the output.
    """
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _time_24h(raw: str | None) -> str:
    """``"1:00 p.m."`` -> ``"13:00"``. Returns "" if unparseable."""
    if not raw:
        return ""
    match = re.match(r"\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", raw.strip(), re.I)
    if not match:
        return ""
    hour, minute, meridiem = int(match.group(1)), match.group(2), match.group(3).lower()
    if meridiem == "p" and hour != 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"

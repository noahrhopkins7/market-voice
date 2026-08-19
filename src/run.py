"""Full pipeline: fetch -> generate -> validate -> tts -> publish.

    python -m src.run --local        # everything except publish
    python -m src.run                # the real thing, publishes

THE GUARDS, AND WHY EACH ONE EXISTS
-----------------------------------
brief.yml fires every 30 minutes across a wide UTC band rather than at one
nominal time, because GitHub cron drift was measured at 105 minutes on
2026-08-13 — far past SPEC.md §7's assumed 5-20. Four guards decide whether a
given firing does any work:

  within_window     is it still meaningfully pre-market (05:00-08:59 ET)?
  is_trading_day    NYSE session, via pandas_market_calendars
  already_published does today's episode exist in the live feed? This, not the
                    time window, is what prevents double-publishing
  attempts_today    have 2 attempts already failed today? Between 2026-08-14
                    and 08-18 a failing run re-ran the paid pipeline once per
                    cron slot, 3-6 times a morning, for nothing

The first three exit 0 — a skipped firing is the normal case and must not send
a failure email. Exhausting the attempt cap exits 1, because that is a real
problem someone needs to look at.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("run")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ET = ZoneInfo("America/New_York")

# Measured 2026-08-13: the 10:15 UTC entry fired at 12:00 UTC — 105 minutes
# late, against SPEC.md §7's assumed 5-20. Both runs that day woke outside a
# 06:00-06:59 window and published nothing.
#
# So the window is no longer the double-publish guard; `already_published` is.
# The window only answers "is it still meaningfully pre-market?" — a briefing
# written after the 09:30 open is not a pre-market briefing, so 09:00 ET is the
# latest a run may start.
WINDOW_START_HOUR = 5   # inclusive
WINDOW_END_HOUR = 9     # exclusive -> 05:00-08:59 ET


def within_window(now_et: dt.datetime) -> bool:
    return WINDOW_START_HOUR <= now_et.hour < WINDOW_END_HOUR


# A deterministic failure must not re-run the paid pipeline once per cron slot.
# Measured 2026-08-14..18: 3-6 full Pass 1 runs every morning, all failing,
# roughly $2-4/day for nothing.
MAX_ATTEMPTS_PER_DAY = 2


def _feed_dir_url() -> str | None:
    import os

    base, token = os.getenv("FEED_BASE_URL"), os.getenv("FEED_TOKEN")
    return f"{base.rstrip('/')}/f/{token}" if base and token else None


def attempts_today(trading_day: str) -> int:
    """How many generation attempts gh-pages has already recorded today."""
    import json as _json
    import urllib.request

    feed_dir = _feed_dir_url()
    if not feed_dir:
        return 0
    try:
        with urllib.request.urlopen(f"{feed_dir}/state.json", timeout=15) as r:
            return _json.loads(r.read().decode()).get("attempts", {}).get(trading_day, 0)
    except Exception:  # noqa: BLE001 - absent state.json is the normal first run
        return 0


def already_published(trading_day: str) -> bool:
    """True if the live feed already carries this trading day's episode.

    This is what makes frequent cron entries safe: the first run of the day
    that lands inside the window publishes, and every later one exits here
    before spending a cent on APIs.

    On any failure, return False. Re-running costs ~$1 and overwrites the
    episode in place (same filename, same guid); skipping means no briefing at
    all. The cheaper mistake is to run twice.
    """
    import os
    import urllib.request

    base = os.getenv("FEED_BASE_URL")
    token = os.getenv("FEED_TOKEN")
    if not base or not token:
        return False
    url = f"{base.rstrip('/')}/f/{token}/feed.xml"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return f"market-voice-{trading_day}" in response.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - never let this check block a briefing
        log.info("could not read the published feed; assuming not yet published")
        return False


def is_trading_day(day: dt.date) -> bool:
    """NYSE session check. On calendar failure, assume yes and let the run
    proceed — a spurious extra briefing beats a silently skipped one."""
    try:
        import pandas_market_calendars as mcal

        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=day.isoformat(), end_date=day.isoformat()
        )
        return not schedule.empty
    except Exception:  # noqa: BLE001 - calendar must never hard-fail the run
        log.warning("NYSE calendar unavailable; proceeding without the holiday check")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.run")
    parser.add_argument("--local", action="store_true",
                        help="run everything but publish")
    parser.add_argument("--fixture", action="store_true",
                        help="use the sample market data instead of live APIs")
    parser.add_argument("--voice", default=None, help="override the TTS voice")
    parser.add_argument("--force", action="store_true",
                        help="skip the time-window and holiday guards")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stderr,
    )

    now_et = dt.datetime.now(ET)
    log.info("local time %s", now_et.strftime("%Y-%m-%d %H:%M:%S %Z"))

    if not args.force:
        if not within_window(now_et):
            log.info("outside the %02d:00-%02d:59 ET window — exiting cleanly",
                     WINDOW_START_HOUR, WINDOW_END_HOUR - 1)
            return 0
        if not is_trading_day(now_et.date()):
            log.info("%s is not an NYSE session — exiting cleanly", now_et.date())
            return 0
        today = now_et.date().isoformat()
        if already_published(today):
            log.info("today's episode is already in the feed — exiting cleanly")
            return 0
        tried = attempts_today(today)
        if tried >= MAX_ATTEMPTS_PER_DAY:
            log.error("%d attempts already failed today; not spending more. "
                      "Investigate, then re-run with --force.", tried)
            return 1

    started = dt.datetime.now()

    from . import fetch_data, generate, tts, validate
    from . import publish as publish_mod

    # Recorded before generation so a crash still counts against the cap.
    if not args.force and not args.local:
        try:
            publish_mod.record_attempt(
                publish_mod.build_config(now_et.date().isoformat()),
                now_et.date().isoformat())
        except SystemExit:
            log.warning("feed config missing; attempt not recorded")

    # 1. Fetch -----------------------------------------------------------
    log.info("fetch")
    if args.fixture:
        market_data = fetch_data.load_fixture()
    else:
        market_data = fetch_data.fetch_all(fetch_data.build_context(), use_cache=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "market_data.json").write_text(json.dumps(market_data, indent=2) + "\n")
    if market_data.get("fetch_errors"):
        log.warning("%d source(s) degraded", len(market_data["fetch_errors"]))

    # 2. Generate --------------------------------------------------------
    import anthropic

    client = anthropic.Anthropic()
    log.info("pass 1 — research")
    report, research_usage = generate.run_research(client, market_data)
    log.info("pass 2 — script")
    script, script_usage = generate.run_script(client, report)

    result = generate.Result(report=report, script=script,
                             usages=[research_usage, script_usage])
    (DATA_DIR / "report.md").write_text(report + "\n")
    (DATA_DIR / "script.txt").write_text(script + "\n")
    generate.log_costs(result, market_data["meta"]["trading_day"])

    # 3. Validate — one retry, then fail loudly (SPEC.md §5) --------------
    outcome = validate.validate(script, market_data)
    validate.log_rejections(outcome, DATA_DIR / "script.txt")

    if not outcome.ok:
        log.warning("validation failed: %s",
                    [v.raw for v in outcome.violations])
        log.info("regenerating once with the failures appended")
        correction = (
            report
            + "\n\nCORRECTION REQUIRED. These figures do not appear in the market "
              "data and must be removed or replaced with figures that do: "
            + ", ".join(sorted({v.raw for v in outcome.violations}))
        )
        script, retry_usage = generate.run_script(client, correction)
        result.usages.append(retry_usage)
        (DATA_DIR / "script.txt").write_text(script + "\n")
        generate.log_costs(generate.Result(usages=[retry_usage]),
                           market_data["meta"]["trading_day"])

        outcome = validate.validate(script, market_data)
        validate.log_rejections(outcome, DATA_DIR / "script.txt")
        if not outcome.ok:
            # SPEC.md §5: publish nothing. A missing episode is recoverable;
            # a wrong one you acted on is not.
            offenders = [v.raw for v in outcome.violations]
            log.error("validation failed twice — publishing nothing")
            log.error("offending figures: %s", offenders)
            if not args.local:
                try:
                    publish_mod.record_outcome(
                        publish_mod.build_config(market_data["meta"]["trading_day"]),
                        market_data["meta"]["trading_day"],
                        "validation failed twice; rejected figures: "
                        + ", ".join(offenders))
                except Exception:  # noqa: BLE001 - reporting must not mask the failure
                    log.warning("could not record the failure reason")
            return 1

    log.info("validation passed — %d figures checked", outcome.checked)

    # 4. Audio -----------------------------------------------------------
    log.info("tts")
    voice = args.voice or tts.DEFAULT_VOICE
    seconds, tts_dollars = tts.build_episode(
        client=__import__("openai").OpenAI(),
        script=script, voice=voice, out_path=DATA_DIR / "episode.mp3",
    )
    log.info("episode %.1f min", seconds / 60)

    # 5. Publish ---------------------------------------------------------
    if args.local:
        log.info("--local: skipping publish")
    else:
        config = publish_mod.build_config(market_data["meta"]["trading_day"])
        publish_mod.publish_to_branch(config)

    elapsed = (dt.datetime.now() - started).total_seconds()
    total = result.total() + tts_dollars
    log.info("done in %d:%02d — $%.3f this run, 30-day $%.2f of $%.2f",
             int(elapsed // 60), int(elapsed % 60), total,
             generate.rolling_30d_total(), generate.BUDGET_CEILING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

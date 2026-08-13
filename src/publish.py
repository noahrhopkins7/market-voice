"""Build the private podcast feed and prune old episodes.

    python -m src.publish --local          # build ./public, touch no git
    python -m src.publish --push           # also commit to the gh-pages branch

Privacy is by unguessable URL (SPEC.md §7): the feed lives at
/f/<FEED_TOKEN>/feed.xml. That is obscurity, not security — anyone with the URL
has the feed forever, so don't treat it as access control.

Episodes older than PRUNE_DAYS are dropped from both the manifest and disk, and
the feed is rewritten from scratch each run. Without that the repo grows without
bound.

The report is converted to HTML by a small converter below rather than a
markdown library, because no markdown package is on CLAUDE.md's dependency list
and this needs to handle exactly one document shape.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

log = logging.getLogger("publish")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"

ET = ZoneInfo("America/New_York")
PRUNE_DAYS = 14
GH_PAGES_BRANCH = "gh-pages"


# --------------------------------------------------------------------------
# minimal markdown -> HTML
# --------------------------------------------------------------------------

def markdown_to_html(text: str) -> str:
    """Convert the subset of markdown Pass 1 emits: headings, tables, lists,
    bold/italic, paragraphs. Anything unrecognised degrades to a paragraph."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)) + 1, 6)  # h1 -> h2, feed already has a title
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # Table: a header row followed by a |---|---| separator.
        if stripped.startswith("|") and index + 1 < len(lines) \
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[index + 1]):
            header = _table_cells(stripped)
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_table_cells(lines[index].strip()))
                index += 1
            out.append(_render_table(header, rows))
            continue

        if re.match(r"^[-*+]\s+", stripped):
            items = []
            while index < len(lines) and re.match(r"^[-*+]\s+", lines[index].strip()):
                items.append(_inline(re.sub(r"^[-*+]\s+", "", lines[index].strip())))
                index += 1
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            continue

        # Paragraph: consume until a blank line or a block-level marker.
        paragraph = []
        while index < len(lines) and lines[index].strip() \
                and not lines[index].strip().startswith(("#", "|", "-", "*", "+")):
            paragraph.append(lines[index].strip())
            index += 1
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
        else:
            index += 1

    return "\n".join(out)


def _table_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_inline(c)}</th>" for c in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _inline(text: str) -> str:
    """Escape, then re-apply bold/italic/code."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------

def _seconds_to_hhmmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def build_feed(episodes: list[dict], config: dict) -> str:
    """RSS 2.0 with the itunes namespace (SPEC.md §7)."""
    base = config["base_url"].rstrip("/")
    feed_dir = f"{base}/f/{config['token']}"
    now = dt.datetime.now(dt.timezone.utc)

    items = []
    for episode in sorted(episodes, key=lambda e: e["published_utc"], reverse=True):
        published = dt.datetime.strptime(
            episode["published_utc"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        url = f"{feed_dir}/episodes/{episode['filename']}"
        items.append(f"""    <item>
      <title>{xml_escape(episode['title'])}</title>
      <pubDate>{format_datetime(published)}</pubDate>
      <guid isPermaLink="false">{xml_escape(episode['guid'])}</guid>
      <enclosure url="{xml_escape(url)}" length="{episode['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{_seconds_to_hhmmss(episode['seconds'])}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <description><![CDATA[{episode.get('description_html', '')}]]></description>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{xml_escape(config['title'])}</title>
    <link>{xml_escape(feed_dir)}/feed.xml</link>
    <description>{xml_escape(config['description'])}</description>
    <language>en-us</language>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
    <itunes:author>{xml_escape(config['author'])}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Business"/>
    <itunes:type>episodic</itunes:type>
{chr(10).join(items)}
  </channel>
</rss>
"""


# --------------------------------------------------------------------------
# manifest + pruning
# --------------------------------------------------------------------------

def load_manifest(public_dir: Path) -> list[dict]:
    path = public_dir / "episodes.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except ValueError:
        log.warning("manifest unreadable; starting fresh")
        return []


def prune(episodes: list[dict], episodes_dir: Path, days: int = PRUNE_DAYS) -> list[dict]:
    """Drop episodes older than `days` from the manifest and from disk."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    keep, dropped = [], []
    for episode in episodes:
        try:
            published = dt.datetime.strptime(
                episode["published_utc"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            continue  # unparseable entry is not worth keeping
        (keep if published >= cutoff else dropped).append(episode)

    for episode in dropped:
        stale = episodes_dir / episode["filename"]
        if stale.exists():
            stale.unlink()
            log.info("pruned %s", stale.name)
    return keep


# --------------------------------------------------------------------------

def publish(config: dict, public_dir: Path = PUBLIC_DIR) -> Path:
    episode_src = DATA_DIR / "episode.mp3"
    if not episode_src.exists():
        raise SystemExit(f"no episode at {episode_src} — run src.tts first")

    feed_dir = public_dir / "f" / config["token"]
    episodes_dir = feed_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    trading_day = config["trading_day"]
    filename = f"brief-{trading_day}.mp3"
    shutil.copy2(episode_src, episodes_dir / filename)

    report_path = DATA_DIR / "report.md"
    description = markdown_to_html(report_path.read_text()) if report_path.exists() else ""

    from .tts import probe_duration

    episodes = [e for e in load_manifest(public_dir) if e.get("filename") != filename]
    episodes.append({
        "guid": f"market-voice-{trading_day}",
        "title": f"Pre-market briefing — {trading_day}",
        "filename": filename,
        "bytes": (episodes_dir / filename).stat().st_size,
        "seconds": probe_duration(episodes_dir / filename),
        "published_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description_html": description,
    })

    episodes = prune(episodes, episodes_dir)
    (public_dir / "episodes.json").write_text(json.dumps(episodes, indent=2))
    (feed_dir / "feed.xml").write_text(build_feed(episodes, config))

    log.info("%d episode(s) in the feed", len(episodes))
    return feed_dir / "feed.xml"


def publish_to_branch(config: dict) -> Path:
    """Build the site inside a gh-pages worktree, then commit and push it.

    A worktree rather than `git push HEAD:gh-pages`: that form would publish the
    whole main branch — src/, workflows, the lot — to a public Pages branch.
    gh-pages must contain the site and nothing else.

    Checking out the existing branch first also means prior episodes are present
    on disk, so pruning has something to prune and the manifest keeps its history.
    """
    import tempfile

    def git(*args: str, cwd: Path = ROOT) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "gh-pages"
        try:
            git("fetch", "origin", GH_PAGES_BRANCH)
            git("worktree", "add", str(worktree), f"origin/{GH_PAGES_BRANCH}")
        except subprocess.CalledProcessError:
            log.info("no %s branch yet — creating an orphan", GH_PAGES_BRANCH)
            git("worktree", "add", "--detach", str(worktree))
            git("checkout", "--orphan", GH_PAGES_BRANCH, cwd=worktree)
            git("rm", "-rf", "--quiet", ".", cwd=worktree)

        try:
            feed_path = publish(config, worktree)
            git("add", "-A", cwd=worktree)
            if not git("status", "--porcelain", cwd=worktree):
                log.info("nothing changed; not publishing")
                return feed_path
            git("commit", "-m", f"brief {config['trading_day']}", cwd=worktree)
            git("push", "origin", f"HEAD:{GH_PAGES_BRANCH}", cwd=worktree)
            log.info("pushed to %s", GH_PAGES_BRANCH)
            return feed_path
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=ROOT, capture_output=True)


def build_config(trading_day: str) -> dict:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "API_KEYS.env")
    token = os.getenv("FEED_TOKEN")
    base_url = os.getenv("FEED_BASE_URL")
    if not token or not base_url:
        raise SystemExit(
            "FEED_TOKEN and FEED_BASE_URL must be set in .env.\n"
            "  FEED_TOKEN     an unguessable path segment, e.g. "
            f"{os.urandom(5).hex()}\n"
            "  FEED_BASE_URL  your GitHub Pages root, e.g. "
            "https://<user>.github.io/<repo>"
        )
    return {
        "token": token,
        "base_url": base_url,
        "title": os.getenv("FEED_TITLE", "Morning Market Briefing"),
        "author": os.getenv("FEED_AUTHOR", "Market Voice"),
        "description": os.getenv(
            "FEED_DESCRIPTION",
            "Daily pre-market briefing. Every figure is fetched, not recalled.",
        ),
        "trading_day": trading_day,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.publish")
    parser.add_argument("--local", action="store_true",
                        help="build ./public without touching git (default)")
    parser.add_argument("--push", action="store_true",
                        help="commit and push the built feed to gh-pages")
    parser.add_argument("--out", type=Path, default=PUBLIC_DIR)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s", stream=sys.stderr,
    )

    market_data_path = DATA_DIR / "market_data.json"
    trading_day = dt.datetime.now(ET).date().isoformat()
    if market_data_path.exists():
        trading_day = json.loads(market_data_path.read_text()) \
            .get("meta", {}).get("trading_day", trading_day)

    config = build_config(trading_day)
    feed_path = publish_to_branch(config) if args.push else publish(config, args.out)
    print(f"wrote {feed_path}", file=sys.stderr)
    print(f"feed URL: {config['base_url'].rstrip('/')}/f/{config['token']}/feed.xml",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

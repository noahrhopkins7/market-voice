"""Script to audio — chunked TTS, concatenated into one MP3.

    python -m src.tts data/script.txt              # full episode
    python -m src.tts data/script.txt --sample     # 300-word A/B across voices

WHY FFMPEG AND NOT PYDUB
------------------------
SPEC.md §6 says "concatenate with pydub/ffmpeg". pydub is unusable here: it
imports the stdlib ``audioop`` module, which PEP 594 removed in Python 3.13,
so ``import pydub`` fails outright on this interpreter. Driving ffmpeg through
subprocess needs no extra Python dependency and does silence-prepend, concat,
and 64kbps mono encoding in a single pass.

CHUNKING IS THE PART THAT MATTERS
---------------------------------
gpt-4o-mini-tts caps at 2,000 input tokens and a full script is roughly 3,000,
so splitting is mandatory. Split on paragraph boundaries first, sentence
boundaries only when a single paragraph is too long, and never mid-sentence —
a seam inside a sentence produces an audible prosody break.

Sentence splitting has to survive this specific text: "4.32%" and "$182.40"
must not split at the decimal point, and "1:00 p.m." and "U.S." must not split
at the abbreviation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("tts")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
COSTS_PATH = DATA_DIR / "costs.jsonl"

MODEL = "gpt-4o-mini-tts"
VOICES = ["ash", "sage", "cedar", "marin"]  # SPEC.md §6: pick by ear, not by adjective
# Chosen 2026-08-12 after A/B-ing all four on the same 300-word sample.
# Re-run `python -m src.tts data/script.txt --sample` to revisit.
DEFAULT_VOICE = "ash"

# SPEC.md §6 — the "non-robotic" lever. Iterate on this before touching voice.
DELIVERY_INSTRUCTIONS = (
    "Measured and conversational, like a colleague reading you a desk note over "
    "coffee. Unhurried. Slight downward inflection at sentence ends. Do not "
    "sound like a newsreader or an advertisement."
)

# The cap is 2,000 tokens. English averages ~4 characters per token, but this
# script is dense with figures and punctuation, which tokenize worse — SPEC.md
# measures 1,750 words at ~2,750 tokens (~1.57 tokens/word). 4,000 characters
# is roughly 1,000-1,300 tokens here, leaving generous headroom.
MAX_CHUNK_CHARS = 4000

LEAD_SILENCE_SECONDS = 0.5  # podcast apps clip the first word without this
BITRATE = "64k"             # plenty for speech; ~5.5MB for 12 minutes
COST_PER_MINUTE = 0.015

# Abbreviations that end in a period and are followed by a capitalised word.
# Without these, "U.S. Treasury" and "1:00 p.m. The" split mid-sentence.
_ABBREVIATIONS = (
    "a.m", "p.m", "U.S", "U.K", "e.g", "i.e", "vs", "Mr", "Mrs", "Ms", "Dr",
    "Inc", "Corp", "Co", "Ltd", "St", "Jr", "Sr", "No", "approx", "est",
)
_ABBREV_SET = frozenset(_ABBREVIATIONS)

# Candidate sentence boundary: . ! or ? then whitespace then something that can
# open a sentence. Decimals ("4.32", "$182.40") are excluded for free, because
# their period is followed by a digit rather than whitespace.
#
# Abbreviations cannot be excluded here — Python requires a fixed-width
# lookbehind, and these vary in length — so candidates are filtered afterwards
# in _ends_with_abbreviation.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(]?[A-Z0-9])")


def _ends_with_abbreviation(text: str) -> bool:
    """True if `text` ends on an abbreviation's period rather than a sentence."""
    stripped = text.rstrip()
    if not stripped.endswith("."):
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    token = tokens[-1][:-1]  # drop the trailing period
    # Single capitals cover initials: "Philip N. Jefferson".
    return token in _ABBREV_SET or (len(token) == 1 and token.isupper())


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

def split_sentences(paragraph: str) -> list[str]:
    """Sentence-split without breaking decimals or abbreviations."""
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(paragraph):
        head = paragraph[start:match.start()]
        if _ends_with_abbreviation(head):
            continue  # "U.S." / "1:00 p.m." — not a sentence end
        if head.strip():
            parts.append(head.strip())
            start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [paragraph.strip()]


def chunk_script(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Paragraph-first chunking, falling back to sentences. Never mid-sentence."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # Expand any paragraph that is itself over the limit into sentences.
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        buffer = ""
        for sentence in split_sentences(paragraph):
            if buffer and len(buffer) + 1 + len(sentence) > max_chars:
                units.append(buffer)
                buffer = sentence
            else:
                buffer = f"{buffer} {sentence}".strip()
        if buffer:
            units.append(buffer)

    # Pack units into chunks, keeping paragraph breaks where they fall.
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 2 + len(unit) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = f"{current}\n\n{unit}".strip() if current else unit
    if current:
        chunks.append(current)

    oversized = [c for c in chunks if len(c) > max_chars]
    if oversized:
        # A single sentence longer than the cap. Nothing safe to do but say so.
        log.warning("%d chunk(s) exceed %d chars and may be rejected",
                    len(oversized), max_chars)
    return chunks


# --------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------

def synthesize(client, text: str, voice: str, out_path: Path) -> None:
    response = client.audio.speech.create(
        model=MODEL,
        voice=voice,
        input=text,
        instructions=DELIVERY_INSTRUCTIONS,
        response_format="mp3",
    )
    out_path.write_bytes(response.content)


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit(
            "ffmpeg not found. Install it with:  brew install ffmpeg\n"
            "(pydub cannot substitute — it needs the stdlib audioop module, "
            "removed in Python 3.13.)"
        )
    return ffmpeg


def concat_with_lead_silence(parts: list[Path], out_path: Path) -> float:
    """Concatenate chunks behind a lead silence. Returns duration in seconds."""
    ffmpeg = _require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        silence = tmpdir / "silence.mp3"
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i",
             f"anullsrc=r=24000:cl=mono:d={LEAD_SILENCE_SECONDS}",
             "-b:a", BITRATE, "-ac", "1", str(silence)],
            check=True, capture_output=True,
        )

        listing = tmpdir / "parts.txt"
        listing.write_text(
            "\n".join(f"file '{p}'" for p in [silence, *parts]) + "\n"
        )

        # Re-encode rather than stream-copy: the chunks come back from the API
        # independently encoded, and copying can leave gaps at the seams.
        subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-b:a", BITRATE, "-ac", "1", str(out_path)],
            check=True, capture_output=True,
        )

    return probe_duration(out_path)


def probe_duration(path: Path) -> float:
    """Seconds of audio, via ffprobe (falls back to 0.0)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


# --------------------------------------------------------------------------

def log_cost(characters: int, seconds: float, voice: str, chunks: int) -> float:
    dollars = seconds / 60 * COST_PER_MINUTE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with COSTS_PATH.open("a") as handle:
        handle.write(json.dumps({
            "logged_at_utc": dt.datetime.now(dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pass": "tts",
            "model": MODEL,
            "voice": voice,
            "characters": characters,
            "chunks": chunks,
            "audio_seconds": round(seconds, 1),
            "dollars": round(dollars, 4),
        }) + "\n")
    return dollars


def build_episode(client, script: str, voice: str, out_path: Path) -> tuple[float, float]:
    chunks = chunk_script(script)
    log.info("%d chunks (max %d chars)", len(chunks), max(len(c) for c in chunks))

    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for index, chunk in enumerate(chunks, start=1):
            part = Path(tmp) / f"chunk_{index:02d}.mp3"
            log.info("  chunk %d/%d — %d chars", index, len(chunks), len(chunk))
            synthesize(client, chunk, voice, part)
            parts.append(part)
        seconds = concat_with_lead_silence(parts, out_path)

    dollars = log_cost(len(script), seconds, voice, len(chunks))
    return seconds, dollars


def build_voice_samples(client, script: str, out_dir: Path, words: int = 300) -> None:
    """One ~300-word sample per voice, same text, for an A/B by ear."""
    sample = " ".join(script.split()[:words])
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Sample text ({len(sample.split())} words, {len(sample)} chars):\n",
          file=sys.stderr)
    for voice in VOICES:
        path = out_dir / f"sample_{voice}.mp3"
        log.info("rendering %s", voice)
        synthesize(client, sample, voice, path)
        seconds = probe_duration(path)
        log_cost(len(sample), seconds, voice, 1)
        print(f"  {voice:6} {path}  ({seconds:.0f}s)", file=sys.stderr)


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tts",
        description="Render a script to a single MP3",
    )
    parser.add_argument("script", type=Path, nargs="?",
                        default=DATA_DIR / "script.txt")
    parser.add_argument("--voice", default=DEFAULT_VOICE, choices=VOICES)
    parser.add_argument("--sample", action="store_true",
                        help="render a 300-word sample in every voice and exit")
    parser.add_argument("--out", type=Path, default=DATA_DIR / "episode.mp3")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the chunk plan without calling the API")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "API_KEYS.env")

    if not args.script.exists():
        raise SystemExit(f"script not found: {args.script}")
    script = args.script.read_text().strip()

    if args.dry_run:
        chunks = chunk_script(script)
        print(f"{len(script):,} chars -> {len(chunks)} chunks\n")
        for index, chunk in enumerate(chunks, start=1):
            head = chunk[:60].replace("\n", " ")
            tail = chunk[-60:].replace("\n", " ")
            print(f"  chunk {index}: {len(chunk):>5,} chars")
            print(f"    starts: {head}…")
            print(f"    ends:   …{tail}")
            ends_clean = chunk.rstrip().endswith((".", "!", "?", '"'))
            print(f"    ends on a sentence boundary: {ends_clean}")
        return 0

    from openai import OpenAI

    client = OpenAI()

    if args.sample:
        build_voice_samples(client, script, DATA_DIR / "voice_samples")
        return 0

    seconds, dollars = build_episode(client, script, args.voice, args.out)
    size_mb = args.out.stat().st_size / 1_048_576
    print(
        f"wrote {args.out}  —  {seconds/60:.1f} min, {size_mb:.1f} MB, "
        f"voice {args.voice}, ${dollars:.3f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

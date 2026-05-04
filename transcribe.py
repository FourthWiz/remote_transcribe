#!/usr/bin/env python3
# Auto-activate the local .venv if we're not already running inside it.
import os as _os, sys as _sys
_VENV = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".venv")
if _os.path.isdir(_VENV) and _sys.prefix != _VENV:
    _python = _os.path.join(_VENV, "bin", "python3")
    _os.execv(_python, [_python] + _sys.argv)
# ──────────────────────────────────────────────────────────────────────────────

# Disable huggingface_hub's "xet" chunked download protocol — it requires
# authentication and breaks on custom ffmpeg paths. Falls back to standard HTTPS.
import os as _os2
_os2.environ.setdefault("HF_HUB_DISABLE_XET", "1")

"""
transcribe.py — Batch-transcribe meeting recordings using local Whisper + optional speaker diarization.

Usage:
    python3 transcribe.py                          # transcribe today's meetings
    python3 transcribe.py --date 2026-04-14        # specific date
    python3 transcribe.py --all                    # all dates
    python3 transcribe.py --model medium           # use a smaller/faster model
    python3 transcribe.py --no-diarize             # skip speaker identification
    python3 transcribe.py --reprocess              # re-transcribe even good notes

Requirements (run setup.sh first):
    brew install ffmpeg
    pip3 install mlx-whisper tqdm
    pip3 install pyannote.audio   # optional, for speaker diarization
    export HF_TOKEN=hf_...        # required for diarization
"""

import argparse
import datetime
import os
import re
import shutil
import sys
import time
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = (".m4a", ".mp3")
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
SKIP_PATTERN = "_recovered"
STUB_MARKER = "AI Service Unavailable"
MIN_GOOD_NOTE_SIZE = 500  # bytes — notes below this are treated as stubs
DEFAULT_LANGUAGE = "en"

# mlx-whisper model repos on HuggingFace (Apple Silicon GPU)
MLX_MODEL_MAP = {
    "tiny":     "mlx-community/whisper-tiny-mlx",
    "base":     "mlx-community/whisper-base-mlx",
    "small":    "mlx-community/whisper-small-mlx",
    "medium":   "mlx-community/whisper-medium-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}


def _load_config() -> dict:
    """Read config.env from the script's directory. Returns key→value dict."""
    config_path = Path(__file__).parent / "config.env"
    cfg = {}
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                v = value.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                cfg[key.strip()] = v
    return cfg


_CFG = _load_config()

_meetings_default = _CFG.get("MEETINGS_DIR", "~/Desktop/Meetings")
DEFAULT_MEETINGS_DIR = Path(_meetings_default).expanduser()
_notes_default = _CFG.get("NOTES_DIR", "") or str(DEFAULT_MEETINGS_DIR / "Notes")
DEFAULT_NOTES_DIR = Path(_notes_default).expanduser()
_copy = _CFG.get("COPY_DIR", "").strip()
COPY_DIR = Path(_copy).expanduser() if _copy else None
DEFAULT_MODEL = _CFG.get("WHISPER_MODEL", "medium")


# ── Helpers ───────────────────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def format_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration string."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / 1_048_576


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe meeting recordings using local Whisper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Date to process (default: today)",
    )
    date_group.add_argument(
        "--all",
        action="store_true",
        help="Process all dates, not just one",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
        help=f"Whisper model size (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-diarize",
        action="store_true",
        help="Skip speaker diarization",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Re-transcribe files that already have good notes",
    )
    parser.add_argument(
        "--meetings-dir",
        type=Path,
        default=DEFAULT_MEETINGS_DIR,
        metavar="PATH",
        help=f"Input folder (default: {DEFAULT_MEETINGS_DIR})",
    )
    parser.add_argument(
        "--notes-dir",
        type=Path,
        default=DEFAULT_NOTES_DIR,
        metavar="PATH",
        help=f"Output folder (default: {DEFAULT_NOTES_DIR})",
    )

    args = parser.parse_args()

    # Expand ~ in paths
    args.meetings_dir = args.meetings_dir.expanduser()
    args.notes_dir = args.notes_dir.expanduser()

    # Validate / default date
    if not args.all:
        if args.date is None:
            args.date = datetime.date.today().strftime("%Y-%m-%d")
        else:
            try:
                datetime.datetime.strptime(args.date, "%Y-%m-%d")
            except ValueError:
                parser.error(f"Invalid date format '{args.date}'. Use YYYY-MM-DD.")

    return args


# ── Pre-flight checks ─────────────────────────────────────────────────────────

def preflight_checks(args: argparse.Namespace) -> dict:
    """
    Validate the environment. Returns a config dict with resolved settings.
    Hard-exits on critical missing dependencies; warns and degrades for optional ones.
    """
    config = {
        "diarize": not args.no_diarize,
        "diarize_available": False,
        "model": args.model,
        "meetings_dir": args.meetings_dir,
        "notes_dir": args.notes_dir,
    }

    # ffmpeg — hard requirement
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install it with:")
        print("       brew install ffmpeg")
        sys.exit(1)

    # mlx-whisper — hard requirement (Apple Silicon GPU)
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        print("ERROR: mlx-whisper not installed. Install it with:")
        print("       pip3 install mlx-whisper")
        sys.exit(1)

    # pyannote.audio — optional (only needed when diarization requested)
    if config["diarize"]:
        try:
            import pyannote.audio  # noqa: F401
            if os.environ.get("HF_TOKEN"):
                config["diarize_available"] = True
            else:
                print("WARNING: HF_TOKEN not set. Continuing without speaker diarization.")
                print("         Get a free token at: https://huggingface.co/settings/tokens")
                print("         Then: export HF_TOKEN=hf_YOUR_TOKEN")
                print("")
        except ImportError:
            print("WARNING: pyannote.audio not installed. Continuing without speaker diarization.")
            print("         Install with: pip3 install pyannote.audio")
            print("")

    # Directories
    if not config["meetings_dir"].exists():
        print(f"ERROR: Meetings folder not found: {config['meetings_dir']}")
        sys.exit(1)

    config["notes_dir"].mkdir(parents=True, exist_ok=True)

    return config


# ── File discovery ────────────────────────────────────────────────────────────

def discover_files(meetings_dir: Path, target_date: str | None, process_all: bool) -> list[Path]:
    """
    Find audio files whose modification date matches target_date (or all dates).
    Uses file mtime, not filename. Returns a list sorted by mtime.
    """
    candidates = []
    skipped_recovered = 0

    for ext in SUPPORTED_EXTENSIONS:
        candidates.extend(meetings_dir.glob(f"*{ext}"))

    matched = []
    for path in candidates:
        if SKIP_PATTERN in path.name:
            skipped_recovered += 1
            continue

        mtime = path.stat().st_mtime
        file_date = datetime.date.fromtimestamp(mtime).strftime("%Y-%m-%d")

        if process_all or file_date == target_date:
            matched.append((mtime, path))

    # Sort by modification time
    matched.sort(key=lambda t: t[0])
    result = [p for _, p in matched]

    label = "all dates" if process_all else target_date
    print(f"Found {len(result)} recording(s) modified on {label}:")
    for p in result:
        mtime_str = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M")
        print(f"  {p.name} ({file_size_mb(p):.1f} MB, modified {mtime_str})")
    if skipped_recovered:
        print(f"\nSkipped {skipped_recovered} _recovered file(s)")
    print()

    return result


# ── Skip logic ────────────────────────────────────────────────────────────────

def should_skip(audio_path: Path, notes_dir: Path, reprocess: bool) -> bool:
    """
    Returns True if this file should be skipped (already has a good note).
    """
    stem = audio_path.stem
    note_path = notes_dir / f"{stem}.md"

    if not note_path.exists():
        return False  # no note — needs transcription

    content = note_path.read_text(encoding="utf-8", errors="replace")

    # Check for stub marker before size (some stubs are large with bad transcripts)
    if STUB_MARKER in content:
        print(f"  Will re-transcribe (stub note detected): {note_path.name}")
        return False

    if note_path.stat().st_size <= MIN_GOOD_NOTE_SIZE:
        return False  # tiny file — probably an empty stub

    if reprocess:
        print(f"  Will re-transcribe (--reprocess): {note_path.name}")
        return False

    print(f"  Skipping (good note exists, {note_path.stat().st_size} bytes): {note_path.name}")
    return True


# ── Transcription ─────────────────────────────────────────────────────────────

def load_whisper_model(model_size: str) -> str:
    """
    Return the mlx-community HuggingFace repo for the requested model size.
    mlx-whisper downloads and caches the model on first use; no separate load step.
    """
    import mlx_whisper  # noqa: F401 — validates install

    repo = MLX_MODEL_MAP[model_size]
    print(f"Whisper model: {model_size} ({repo})")
    print("(Model downloads on first use, then cached)\n")
    return repo


def transcribe_audio(audio_path: Path, mlx_repo: str) -> tuple[list[dict], object]:
    """
    Transcribe audio using mlx-whisper (Apple Silicon GPU).
    Returns (segments, info) where info is a SimpleNamespace with .duration.
    """
    import mlx_whisper
    from types import SimpleNamespace

    print(f"  Transcribing with GPU (mlx-whisper {mlx_repo.split('/')[-1]})...")
    t0 = time.time()

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=mlx_repo,
        language=DEFAULT_LANGUAGE,
        word_timestamps=True,
        verbose=False,
    )

    transcript_segments = []
    for seg in result["segments"]:
        transcript_segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
            "words": [
                {"word": w["word"], "start": w["start"], "end": w["end"]}
                for w in seg.get("words", [])
            ],
            "speaker": None,
        })

    duration = transcript_segments[-1]["end"] if transcript_segments else 0.0
    elapsed = time.time() - t0
    print(f"  Transcribed {format_timestamp(duration)} of audio in {format_duration(elapsed)}")

    return transcript_segments, SimpleNamespace(duration=duration)


# ── Diarization ───────────────────────────────────────────────────────────────

def load_diarization_pipeline():
    """Load the pyannote speaker diarization pipeline once."""
    import torch
    from pyannote.audio import Pipeline

    print("Loading pyannote diarization pipeline...")
    t0 = time.time()
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=os.environ["HF_TOKEN"],
    )
    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
        print("  Using MPS (Apple Silicon) acceleration")
    elif torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
        print("  Using CUDA acceleration")

    print(f"Diarization pipeline loaded ({time.time() - t0:.1f}s)\n")
    return pipeline


def diarize_audio(audio_path: Path, pipeline) -> tuple[list[dict], dict]:
    """
    Run pyannote diarization on audio_path.
    Returns (speaker_segments, speaker_stats).
    speaker_segments: list of {start, end, speaker}
    speaker_stats: dict of speaker -> {duration, percentage}
    """
    diarization = pipeline(str(audio_path))

    speaker_segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        speaker_segments.append({
            "start": turn.start,
            "end": turn.end,
            "speaker": speaker,
        })

    # Compute per-speaker statistics
    durations: dict[str, float] = {}
    for seg in speaker_segments:
        dur = seg["end"] - seg["start"]
        durations[seg["speaker"]] = durations.get(seg["speaker"], 0.0) + dur

    total = sum(durations.values()) or 1.0
    speaker_stats = {
        spk: {
            "duration": dur,
            "percentage": dur / total * 100,
        }
        for spk, dur in sorted(durations.items())
    }

    # Print summary
    n = len(speaker_stats)
    print(f"  Diarization complete: {n} speaker(s) detected")
    for spk, stats in speaker_stats.items():
        print(f"    {spk}: {stats['percentage']:.0f}% ({format_duration(stats['duration'])})")

    return speaker_segments, speaker_stats


def align_speakers(transcript_segments: list[dict], speaker_segments: list[dict]) -> list[dict]:
    """
    Assign speaker labels to transcript segments using midpoint matching.
    Modifies segments in-place, returns the same list.
    O(n+m) algorithm — both lists are assumed sorted by start time.
    """
    spk_idx = 0
    n_spk = len(speaker_segments)

    for seg in transcript_segments:
        midpoint = (seg["start"] + seg["end"]) / 2

        # Advance past speaker segments that end before our midpoint
        while spk_idx < n_spk and speaker_segments[spk_idx]["end"] < midpoint:
            spk_idx += 1

        if spk_idx < n_spk and speaker_segments[spk_idx]["start"] <= midpoint:
            seg["speaker"] = speaker_segments[spk_idx]["speaker"]
        else:
            seg["speaker"] = "UNKNOWN"

    return transcript_segments


# ── Markdown writer ───────────────────────────────────────────────────────────

def write_transcript(
    audio_path: Path,
    segments: list[dict],
    info,
    notes_dir: Path,
    model_name: str,
    speaker_stats: dict | None = None,
) -> Path:
    """
    Write a Markdown transcript file to notes_dir.
    Uses atomic write (temp file + rename) to prevent partial files.
    """
    note_path = notes_dir / f"{audio_path.stem}.md"
    tmp_path = note_path.with_suffix(".md.tmp")

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    diarized = speaker_stats is not None

    lines = []

    # Header
    lines.append(f"# Transcript: {audio_path.name}")
    lines.append("")
    if diarized:
        lines.append(f"_Transcribed {now_str} — Whisper {model_name} + pyannote diarization_")
    else:
        lines.append(f"_Transcribed {now_str} — Whisper {model_name}_")
    lines.append("")

    # Speaker summary (Stage 2)
    if diarized:
        lines.append("## Speakers")
        for spk, stats in speaker_stats.items():
            lines.append(f"- {spk} ({stats['percentage']:.0f}% of meeting)")
        lines.append("")

    # Transcript body
    lines.append("## Transcript")
    lines.append("")
    for seg in segments:
        ts = format_timestamp(seg["start"])
        text = seg["text"]
        speaker = seg.get("speaker")

        if diarized and speaker and speaker != "UNKNOWN":
            lines.append(f"**[{ts}]** [{speaker}] {text}")
        else:
            lines.append(f"**[{ts}]** {text}")

    content = "\n".join(lines) + "\n"

    # Atomic write
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.rename(note_path)

    print(f"  Saved: Notes/{note_path.name} ({len(segments)} segments, {format_timestamp(info.duration)})")

    # Mirror to secondary notes folder
    if COPY_DIR and COPY_DIR.exists():
        import shutil as _shutil
        _shutil.copy2(note_path, COPY_DIR / note_path.name)
        print(f"  Copied: {COPY_DIR}/{note_path.name}")

    return note_path


def sync_copy_dir(notes_dir: Path, copy_dir: Path | None) -> int:
    """Copy every .md in notes_dir to copy_dir if absent or older there."""
    if copy_dir is None:
        return 0
    copy_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(notes_dir.glob("*.md")):
        dst = copy_dir / src.name
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copy2(src, dst)
            copied += 1
    return copied


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    config = preflight_checks(args)

    target_date = None if args.all else args.date

    print("=== Meeting Transcriber ===")
    print(f"Date:          {target_date or 'all'}")
    print(f"Model:         {config['model']} (int8)")
    print(f"Diarization:   {'enabled' if config['diarize_available'] else 'disabled'}")
    print(f"Meetings dir:  {config['meetings_dir']}")
    print(f"Notes dir:     {config['notes_dir']}")
    print()

    # Discover files
    all_files = discover_files(config["meetings_dir"], target_date, process_all=args.all)
    if not all_files:
        print("No recordings found. Nothing to do.")
        synced = sync_copy_dir(config["notes_dir"], COPY_DIR)
        print(f"Synced:     {synced} file(s) to COPY_DIR")
        return

    # Apply skip logic
    to_process = []
    for f in all_files:
        if not should_skip(f, config["notes_dir"], args.reprocess):
            to_process.append(f)
    print()

    if not to_process:
        print("All recordings already have good transcripts. Use --reprocess to re-transcribe.")
        synced = sync_copy_dir(config["notes_dir"], COPY_DIR)
        print(f"Synced:     {synced} file(s) to COPY_DIR")
        return

    print(f"Will process {len(to_process)} file(s):")
    for i, f in enumerate(to_process, 1):
        print(f"  {i}. {f.name} ({file_size_mb(f):.1f} MB)")
    print()

    # Load models (once)
    whisper_model = load_whisper_model(config["model"])

    diarize_pipeline = None
    if config["diarize_available"]:
        try:
            diarize_pipeline = load_diarization_pipeline()
        except Exception as e:
            print(f"WARNING: Could not load diarization pipeline: {e}")
            print("         Continuing without speaker diarization.\n")
            diarize_pipeline = None

    # Process files
    processed = 0
    skipped = len(all_files) - len(to_process)
    failed = 0
    total_start = time.time()

    for i, audio_path in enumerate(to_process, 1):
        print(f"[{i}/{len(to_process)}] {audio_path.name}")
        file_start = time.time()

        try:
            # Transcribe
            segments, info = transcribe_audio(audio_path, whisper_model)

            # Diarize + align
            speaker_stats = None
            if diarize_pipeline is not None:
                try:
                    speaker_segs, speaker_stats = diarize_audio(audio_path, diarize_pipeline)
                    segments = align_speakers(segments, speaker_segs)
                except Exception as e:
                    print(f"  WARNING: Diarization failed: {e}")
                    print("           Writing transcript without speaker labels.")
                    speaker_stats = None

            # Write output
            write_transcript(
                audio_path,
                segments,
                info,
                config["notes_dir"],
                config["model"],
                speaker_stats=speaker_stats,
            )

            elapsed = time.time() - file_start
            print(f"  Completed in {format_duration(elapsed)}\n")
            processed += 1

        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting cleanly.")
            break
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
            print()

    # Summary
    total_elapsed = time.time() - total_start
    print("=== Done ===")
    print(f"Processed:  {processed} file(s)")
    print(f"Skipped:    {skipped} file(s) (good notes exist)")
    print(f"Failed:     {failed} file(s)")
    print(f"Total time: {format_duration(total_elapsed)}")
    print(f"Output:     {config['notes_dir']}")

    synced = sync_copy_dir(config["notes_dir"], COPY_DIR)
    print(f"Synced:     {synced} file(s) to COPY_DIR")


if __name__ == "__main__":
    main()

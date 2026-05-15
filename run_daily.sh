#!/usr/bin/env bash
# run_daily.sh — called by cron to transcribe all pending meetings.
# Logs to <script_dir>/logs/transcribe.log by default.
# Pass --log_std to print output to the terminal instead.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/transcribe.log"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
LOG_STD=0

for arg in "$@"; do
    [ "$arg" = "--log_std" ] && LOG_STD=1
done

mkdir -p "$LOG_DIR"

# Load config.env (sets MEETINGS_DIR, NOTES_DIR, etc.)
# shellcheck source=config.env
source "$SCRIPT_DIR/config.env" 2>/dev/null || true

# Load HF_TOKEN from shell profile if not already set (cron has no shell env)
if [ -z "$HF_TOKEN" ] && [ -f "$HOME/.zshrc" ]; then
    export HF_TOKEN
    HF_TOKEN=$(grep 'export HF_TOKEN=' "$HOME/.zshrc" | tail -1 | sed 's/export HF_TOKEN=//' | tr -d '"'"'" 2>/dev/null)
fi

HEADER="=== $(date '+%Y-%m-%d %H:%M:%S') — transcribing all pending ==="

if [ "$LOG_STD" -eq 1 ]; then
    echo "$HEADER"
    "$PYTHON" "$SCRIPT_DIR/transcribe.py" --all
    EXIT=$?
    [ $EXIT -eq 0 ] && echo "Done." || echo "ERROR: script exited with code $EXIT"
else
    echo "" >> "$LOG_FILE"
    echo "$HEADER" >> "$LOG_FILE"
    "$PYTHON" "$SCRIPT_DIR/transcribe.py" --all >> "$LOG_FILE" 2>&1
    EXIT=$?
    [ $EXIT -eq 0 ] && echo "Done." >> "$LOG_FILE" || echo "ERROR: script exited with code $EXIT" >> "$LOG_FILE"
fi

#!/usr/bin/env bash
# run_daily.sh — called by cron to transcribe yesterday's meetings.
# Logs to <script_dir>/logs/transcribe.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/transcribe.log"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

mkdir -p "$LOG_DIR"

# Load config.env (sets MEETINGS_DIR, NOTES_DIR, etc.)
# shellcheck source=config.env
source "$SCRIPT_DIR/config.env" 2>/dev/null || true

# Load HF_TOKEN from shell profile if not already set (cron has no shell env)
if [ -z "$HF_TOKEN" ] && [ -f "$HOME/.zshrc" ]; then
    export HF_TOKEN
    HF_TOKEN=$(grep 'export HF_TOKEN=' "$HOME/.zshrc" | tail -1 | sed 's/export HF_TOKEN=//' | tr -d '"'"'" 2>/dev/null)
fi

YESTERDAY=$(date -v-1d +%Y-%m-%d)

echo "" >> "$LOG_FILE"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — transcribing $YESTERDAY ===" >> "$LOG_FILE"

"$PYTHON" "$SCRIPT_DIR/transcribe.py" --date "$YESTERDAY" >> "$LOG_FILE" 2>&1
EXIT=$?

if [ $EXIT -eq 0 ]; then
    echo "Done." >> "$LOG_FILE"
else
    echo "ERROR: script exited with code $EXIT" >> "$LOG_FILE"
fi

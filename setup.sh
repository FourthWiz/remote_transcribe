#!/usr/bin/env bash
# setup.sh — Interactive installer for Meeting Transcriber.
# Run once after cloning, or again to reconfigure.
# Safe to run multiple times.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="$SCRIPT_DIR/config.env"
CRON_MARKER="meet_transcribe/run_daily.sh"

# ── Colour helpers ────────────────────────────────────────────────────────────
bold=$'\033[1m'; reset=$'\033[0m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'
ok()   { echo "  ${green}✓${reset} $*"; }
warn() { echo "  ${yellow}⚠${reset}  $*"; }
err()  { echo "  ${red}✗${reset}  $*"; }
ask()  { printf "  %s " "$*"; }  # no newline — read follows

# ── Prompt helper: ask with a default ────────────────────────────────────────
# Usage: prompt_default VAR "Question" "default value"
prompt_default() {
    local var="$1" question="$2" default="$3"
    ask "${question} [${bold}${default}${reset}]:"
    local answer
    read -r answer
    printf -v "$var" '%s' "${answer:-$default}"
}

# ── Prompt helper: yes/no ─────────────────────────────────────────────────────
# Returns 0 for yes, 1 for no
prompt_yn() {
    local question="$1" default="${2:-n}"
    local prompt
    if [[ "$default" == "y" ]]; then prompt="[Y/n]"; else prompt="[y/N]"; fi
    ask "${question} ${prompt}:"
    local answer
    read -r -n 1 answer; echo
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

echo ""
echo "${bold}=== Meeting Transcriber — Setup ===${reset}"
echo ""

# ── Check if already configured ───────────────────────────────────────────────
if [ -f "$CONFIG_FILE" ]; then
    echo "  Existing configuration found at config.env."
    if ! prompt_yn "Reconfigure?" "n"; then
        echo ""
        echo "Keeping existing config. Re-running dependency install..."
        # shellcheck source=config.env
        source "$CONFIG_FILE"
        SKIP_PROMPTS=true
    fi
fi

# ── Step 1: Meetings input directory ─────────────────────────────────────────
if [ -z "${SKIP_PROMPTS}" ]; then
    echo "${bold}Step 1: Input folder${reset}"
    echo "  Where are your meeting recordings? (.m4a / .mp3 files)"
    while true; do
        prompt_default MEETINGS_DIR "Meetings folder" "~/Desktop/Meetings"
        MEETINGS_DIR_EXPANDED="${MEETINGS_DIR/#\~/$HOME}"
        if [ -d "$MEETINGS_DIR_EXPANDED" ]; then
            ok "Found: $MEETINGS_DIR_EXPANDED"
            break
        else
            err "Folder not found: $MEETINGS_DIR_EXPANDED"
            echo "  (Create it first, or enter a different path)"
        fi
    done
    echo ""

    # ── Step 2: Notes output directory ───────────────────────────────────────
    echo "${bold}Step 2: Output folder${reset}"
    echo "  Where should Markdown transcripts be saved?"
    DEFAULT_NOTES="${MEETINGS_DIR}/Notes"
    prompt_default NOTES_DIR "Notes folder" "$DEFAULT_NOTES"
    NOTES_DIR_EXPANDED="${NOTES_DIR/#\~/$HOME}"
    mkdir -p "$NOTES_DIR_EXPANDED"
    ok "Will save to: $NOTES_DIR_EXPANDED"
    echo ""

    # ── Step 3: Optional copy destination ────────────────────────────────────
    echo "${bold}Step 3: Secondary copy (optional)${reset}"
    echo "  Copy each transcript to a second folder? (e.g. for Claude Desktop access)"
    COPY_DIR=""
    if prompt_yn "Enable secondary copy?" "n"; then
        prompt_default COPY_DIR "Copy-to folder" ""
        COPY_DIR_EXPANDED="${COPY_DIR/#\~/$HOME}"
        if [ -n "$COPY_DIR_EXPANDED" ] && [ ! -d "$COPY_DIR_EXPANDED" ]; then
            mkdir -p "$COPY_DIR_EXPANDED"
            ok "Created: $COPY_DIR_EXPANDED"
        elif [ -n "$COPY_DIR_EXPANDED" ]; then
            ok "Found: $COPY_DIR_EXPANDED"
        fi
    else
        ok "Skipped"
    fi
    echo ""

    # ── Step 4: Whisper model ─────────────────────────────────────────────────
    echo "${bold}Step 4: Whisper model${reset}"
    echo "  Choose transcription quality vs. speed (Apple Silicon GPU):"
    echo ""
    echo "    tiny     — ~1 min/hr  — low accuracy, great for quick tests"
    echo "    base     — ~1 min/hr  — better, still fast"
    echo "    small    — ~2 min/hr  — decent accuracy"
    echo "    medium   — ~4 min/hr  — good accuracy  ${bold}(recommended)${reset}"
    echo "    large-v2 — ~10 min/hr — high accuracy"
    echo "    large-v3 — ~10 min/hr — best accuracy"
    echo ""
    while true; do
        prompt_default WHISPER_MODEL "Model" "medium"
        case "$WHISPER_MODEL" in
            tiny|base|small|medium|large-v2|large-v3) ok "Using: $WHISPER_MODEL"; break ;;
            *) err "Invalid choice. Pick: tiny, base, small, medium, large-v2, large-v3" ;;
        esac
    done
    echo ""

    # ── Step 5: Cron schedule ─────────────────────────────────────────────────
    echo "${bold}Step 5: Auto-transcription schedule${reset}"
    echo "  Set up a daily cron job to transcribe yesterday's meetings automatically?"
    CRON_ENABLED=false
    CRON_TIME="10:15"
    if prompt_yn "Enable daily auto-transcription?" "y"; then
        CRON_ENABLED=true
        while true; do
            prompt_default CRON_TIME "Run time (HH:MM, 24-hour)" "10:15"
            if [[ "$CRON_TIME" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
                ok "Will run daily at $CRON_TIME"
                break
            else
                err "Invalid format. Use HH:MM (e.g. 10:15 or 22:30)"
            fi
        done
    else
        ok "Skipped — run manually: python3 $SCRIPT_DIR/transcribe.py"
    fi
    echo ""

    # ── Write config.env ──────────────────────────────────────────────────────
    cat > "$CONFIG_FILE" <<EOF
# Meeting Transcriber — Configuration
# Generated by setup.sh on $(date '+%Y-%m-%d %H:%M')
# Edit values here or re-run: bash setup.sh

MEETINGS_DIR="${MEETINGS_DIR}"
NOTES_DIR="${NOTES_DIR}"
COPY_DIR="${COPY_DIR}"
WHISPER_MODEL=${WHISPER_MODEL}
CRON_TIME=${CRON_TIME}
CRON_ENABLED=${CRON_ENABLED}
EOF
    ok "Configuration saved to config.env"
    echo ""
fi  # end SKIP_PROMPTS

# ── Load final config ─────────────────────────────────────────────────────────
# shellcheck source=config.env
source "$CONFIG_FILE"

# ── Homebrew ──────────────────────────────────────────────────────────────────
echo "${bold}Installing dependencies...${reset}"
echo ""
echo "  Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    err "Homebrew not found. Install from https://brew.sh, then re-run setup.sh"
    exit 1
fi
ok "Homebrew found"

# ── ffmpeg ────────────────────────────────────────────────────────────────────
echo "  Checking ffmpeg..."
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg already installed ($(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3))"
else
    echo "  Installing ffmpeg via Homebrew..."
    brew install ffmpeg
    ok "ffmpeg installed"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
echo "  Setting up .venv..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok ".venv created"
else
    ok ".venv already exists"
fi
VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# ── Python packages ───────────────────────────────────────────────────────────
echo "  Installing Python packages..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet mlx-whisper tqdm
ok "mlx-whisper + tqdm installed"

# ── Verify imports ────────────────────────────────────────────────────────────
"$VENV_PY" -c "import mlx_whisper; print('  ✓ mlx_whisper import OK')"
"$VENV_PY" -c "import tqdm; print('  ✓ tqdm import OK')"
echo ""

# ── Speaker diarization (optional) ───────────────────────────────────────────
echo "${bold}Speaker diarization (optional)${reset}"
echo "  Identifies who said what — requires a free HuggingFace token."
echo ""
if prompt_yn "Install pyannote.audio for speaker diarization?" "n"; then
    echo "  Installing pyannote.audio..."
    "$VENV_PIP" install --quiet pyannote.audio
    ok "pyannote.audio installed"
    "$VENV_PY" -c "import pyannote.audio; print('  ✓ pyannote.audio import OK')"
    echo ""
    echo "  ${bold}One-time HuggingFace setup required:${reset}"
    echo "    1. Create a token:  https://huggingface.co/settings/tokens"
    echo "    2. Accept license:  https://huggingface.co/pyannote/speaker-diarization-3.1"
    echo "    3. Accept license:  https://huggingface.co/pyannote/segmentation-3.0"
    echo "    4. Add to ~/.zshrc: export HF_TOKEN=hf_YOUR_TOKEN_HERE"
    echo "    5. Then run:        source ~/.zshrc"
else
    ok "Skipped (run setup.sh again to install later)"
fi
echo ""

# ── Cron setup ────────────────────────────────────────────────────────────────
if [ "${CRON_ENABLED}" = "true" ]; then
    echo "${bold}Setting up cron job...${reset}"
    CRON_HOUR="${CRON_TIME%%:*}"
    CRON_MIN="${CRON_TIME##*:}"
    CRON_CMD="${CRON_MIN} ${CRON_HOUR} * * * bash ${SCRIPT_DIR}/run_daily.sh"

    # Remove any existing entry for this script, then add the new one
    ( crontab -l 2>/dev/null | grep -v "$CRON_MARKER"; echo "$CRON_CMD" ) | crontab -
    ok "Cron job set: daily at ${CRON_TIME}"
    echo "  Entry: ${CRON_CMD}"
    echo "  Logs:  ${SCRIPT_DIR}/logs/transcribe.log"
    echo ""
fi

# ── Dry-run test ──────────────────────────────────────────────────────────────
echo "${bold}Testing file discovery...${reset}"
YESTERDAY=$(date -v-1d +%Y-%m-%d)
echo ""
"$VENV_PY" "$SCRIPT_DIR/transcribe.py" --date "$YESTERDAY" --no-diarize 2>&1 \
    | grep -E "^(Found|Skipped|Will process|No recordings|===)" | head -10 || true
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "${bold}=== Setup complete ===${reset}"
echo ""
echo "  Configuration: ${SCRIPT_DIR}/config.env"
echo "  Meetings dir:  ${MEETINGS_DIR}"
echo "  Notes dir:     ${NOTES_DIR}"
[ -n "${COPY_DIR}" ] && echo "  Copy dir:      ${COPY_DIR}"
echo "  Model:         ${WHISPER_MODEL}"
[ "${CRON_ENABLED}" = "true" ] && echo "  Cron:          daily at ${CRON_TIME} (transcribes yesterday)"
echo ""
echo "  Run now:       python3 ${SCRIPT_DIR}/transcribe.py"
echo "  Help:          python3 ${SCRIPT_DIR}/transcribe.py --help"
echo ""

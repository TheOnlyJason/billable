#!/usr/bin/env bash
# billable note — macOS / Linux helper.
#
# Usage:
#   billable-note.sh                                # dialog prompt for text
#   billable-note.sh "what I just did"              # log directly, no dialog
#   billable-note.sh "..." -m 30 --matter acme      # with duration / matter
#
# Wiring up a hotkey:
#   macOS:  open Shortcuts.app -> new Shortcut -> "Run Shell Script" action ->
#           paste the absolute path to this script -> assign a Keyboard Shortcut.
#   Linux:  bind via your DE's keyboard settings (GNOME / KDE / sxhkd / etc.)
#           to: /absolute/path/to/scripts/billable-note.sh
#
# Resolves the repo location from the script's own location, so it works no
# matter where you cloned the repo.

set -euo pipefail

# Resolve script + repo dirs (handles symlinks).
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
    SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$SCRIPT_DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
BILLABLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pick a Python: prefer the local venv, fall back to whatever's on PATH.
if [ -x "$BILLABLE_DIR/.venv/bin/python" ]; then
    PY="$BILLABLE_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
else
    echo "billable-note: no python interpreter found." >&2
    exit 1
fi

# Parse args. Anything that isn't -m / --matter is treated as the note text.
TEXT=""
EXTRA_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -m|--minutes)
            EXTRA_ARGS+=("--minutes" "$2"); shift 2;;
        --matter)
            EXTRA_ARGS+=("--matter" "$2"); shift 2;;
        -h|--help)
            "$PY" -m billable.cli note --help
            exit 0;;
        *)
            TEXT="$1"; shift;;
    esac
done

# If no text given, prompt with a native dialog.
prompt_dialog() {
    if command -v osascript >/dev/null 2>&1; then
        # macOS — AppleScript dialog
        osascript \
            -e 'tell application "System Events" to display dialog "What did you just do?" default answer "" with title "billable note"' \
            -e 'text returned of result' 2>/dev/null
    elif command -v zenity >/dev/null 2>&1; then
        # Linux GNOME
        zenity --entry --title="billable note" --text="What did you just do?"
    elif command -v kdialog >/dev/null 2>&1; then
        # Linux KDE
        kdialog --title "billable note" --inputbox "What did you just do?"
    elif command -v rofi >/dev/null 2>&1; then
        # Linux power-user
        rofi -dmenu -p "billable note" -lines 0 < /dev/null
    else
        echo "billable-note: no GUI dialog available; pass text as the first argument." >&2
        return 1
    fi
}

if [ -z "$TEXT" ]; then
    TEXT="$(prompt_dialog || true)"
fi

# Trim and bail on empty (cancelled).
TEXT="$(printf '%s' "$TEXT" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
if [ -z "$TEXT" ]; then
    exit 0
fi

cd "$BILLABLE_DIR"
"$PY" -m billable.cli note "$TEXT" "${EXTRA_ARGS[@]}"

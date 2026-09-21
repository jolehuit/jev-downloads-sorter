#!/bin/bash
# Installs the launchd agent that runs sort_downloads.py whenever ~/Downloads changes.
# Usage: ./install.sh            (from the cloned repo)
#        ./install.sh --uninstall
set -euo pipefail

LABEL="com.jev-downloads-sorter"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$REPO/sort_downloads.py"
DOWNLOADS="${JEV_SORT_DIR:-$HOME/Downloads}"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Agent removed. Your files stay where they are."
  exit 0
fi

# Runner: uv if present (no install, stdlib only), else python3 from PATH.
# JEV_SORT_BACKEND: jev (default), laya (local model, needs uv: it pulls laya, torch and
# the weights) or rules (no model at all).
BACKEND="${JEV_SORT_BACKEND:-jev}"
if [ "$BACKEND" = "laya" ]; then
  command -v uv >/dev/null 2>&1 || { echo "Local mode needs uv (https://docs.astral.sh/uv/)"; exit 1; }
  RUNNER=("$(command -v uv)" run --no-project --quiet --with laya "$SCRIPT")
elif command -v uv >/dev/null 2>&1; then
  RUNNER=("$(command -v uv)" run --no-project --quiet "$SCRIPT")
else
  RUNNER=("$(command -v python3)" "$SCRIPT")
fi

# The folders must exist before the first run. Create the defaults unless a config is present.
CONFIG="${JEV_SORT_CONFIG:-$HOME/.config/jev-downloads-sorter/folders.json}"
if [ -f "$CONFIG" ]; then
  FOLDERS=$(python3 -c "import json,sys; print('\n'.join(json.load(open(sys.argv[1]))))" "$CONFIG")
else
  FOLDERS=$(python3 -c "import re,sys; s=open(sys.argv[1]).read(); print('\n'.join(re.findall(r'^    \"([^\"]+)\": \"', s[s.index('DEFAULT_FOLDERS'):s.index('EXTENSIONS =')], re.M)))" "$SCRIPT")
fi
while IFS= read -r f; do mkdir -p "$DOWNLOADS/$f"; done <<< "$FOLDERS"

ARGS=""
for a in "${RUNNER[@]}"; do ARGS+="        <string>$a</string>"$'\n'; done

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$ARGS    </array>
    <key>WatchPaths</key>
    <array>
        <string>$DOWNLOADS</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>3</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>JEV_SORT_DIR</key>
        <string>$DOWNLOADS</string>
        <key>JEV_SORT_CONFIG</key>
        <string>$CONFIG</string>
        <key>JEV_SORT_IGNORE</key>
        <string>${JEV_SORT_IGNORE:-}</string>
        <key>JEV_SORT_BACKEND</key>
        <string>$BACKEND</string>
    </dict>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/jev-downloads-sorter.err</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST" >/dev/null

if [ "$BACKEND" = "laya" ]; then
  echo "Downloading and loading Laya once (about 1 GB the first time)..."
  JEV_SORT_BACKEND=laya "${RUNNER[@]}" --warm || { echo "Laya failed to load; agent not installed."; exit 1; }
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Agent loaded. Watching $DOWNLOADS (backend: $BACKEND)"
echo "Folders: $(echo "$FOLDERS" | tr '\n' ',' | sed 's/,$//; s/,/, /g')"
echo "Log:     ~/Library/Logs/jev-downloads-sorter.log"

#!/usr/bin/env bash
# Pen Setup — install / restore into the current Omarchy session.
#
# Copies the bar plugin, the pen daemon, the CLI, the systemd unit and the
# default config, then wires the widget into the bar and the main menu.
# Safe to run again: existing config and bar layout entries are preserved.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="dbeaudry.pensetup"
SHELL_JSON="$HOME/.config/omarchy/shell.json"
MENU_FILE="$HOME/.config/omarchy/extensions/omarchy-menu.jsonc"

say() { printf '  %s\n' "$*"; }

echo "Pen Setup — installing from $HERE"

# --- requirements ---------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi
if ! python3 -c 'import evdev' >/dev/null 2>&1; then
  echo "ERROR: the python 'evdev' module is required." >&2
  echo "       Install it with:  sudo pacman -S python-evdev" >&2
  exit 1
fi

# --- files ----------------------------------------------------------------
install -d "$HOME/.local/bin" \
           "$HOME/.config/omarchy/plugins/$PLUGIN_ID" \
           "$HOME/.config/systemd/user" \
           "$HOME/.config/wacom-pen"

install -m 755 "$HERE/files/.local/bin/wacom-pen-daemon.py" "$HOME/.local/bin/wacom-pen-daemon.py"
install -m 755 "$HERE/files/.local/bin/wacom-pen-ctl"       "$HOME/.local/bin/wacom-pen-ctl"
install -m 644 "$HERE/files/.config/omarchy/plugins/$PLUGIN_ID/manifest.json" \
               "$HOME/.config/omarchy/plugins/$PLUGIN_ID/manifest.json"
install -m 644 "$HERE/files/.config/omarchy/plugins/$PLUGIN_ID/Panel.qml" \
               "$HOME/.config/omarchy/plugins/$PLUGIN_ID/Panel.qml"
install -m 644 "$HERE/files/.config/systemd/user/wacom-pen.service" \
               "$HOME/.config/systemd/user/wacom-pen.service"

if [ -f "$HOME/.config/wacom-pen/config.json" ]; then
  say "kept existing config: ~/.config/wacom-pen/config.json"
else
  install -m 644 "$HERE/files/.config/wacom-pen/config.json" "$HOME/.config/wacom-pen/config.json"
  say "installed default config: ~/.config/wacom-pen/config.json"
fi

# --- bar widget -----------------------------------------------------------
if [ -f "$SHELL_JSON" ]; then
  python3 - "$SHELL_JSON" "$PLUGIN_ID" <<'PY'
import json, sys

path, plugin = sys.argv[1], sys.argv[2]
with open(path) as fh:
    data = json.load(fh)
layout = data.setdefault("bar", {}).setdefault("layout", {})
right = layout.setdefault("right", [])
if any(isinstance(e, dict) and e.get("id") == plugin for e in right):
    print(f"  bar entry already present ({plugin})")
else:
    right.append({"id": plugin})
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"  added {plugin} to the bar (right side)")
PY
else
  say "no shell.json found — add { \"id\": \"$PLUGIN_ID\" } to bar.layout.right yourself"
fi

# --- main menu entry ------------------------------------------------------
if [ -f "$MENU_FILE" ]; then
  if grep -q '"pensetup"' "$MENU_FILE"; then
    say "menu entry already present (pensetup)"
  else
    cp -p "$MENU_FILE" "$MENU_FILE.bak.$(date +%s)"
    python3 - "$MENU_FILE" <<'PY'
import sys

path = sys.argv[1]
row = ('  "pensetup": {"icon":"\u270e","label":"Pen Setup",'
       '"aliases":["pen","pensetup","zenpen","wacom","tablet","stylus"],'
       '"description":"Mouse / pressure modes and per-app pen rules",'
       '"action":"omarchy-shell dbeaudry.pensetup toggle"},\n')
with open(path) as fh:
    text = fh.read()
idx = text.rfind("}")
if idx == -1:
    raise SystemExit("could not find the end of the menu file")
text = text[:idx] + row + text[idx:]
with open(path, "w") as fh:
    fh.write(text)
PY
    say "added the Pen Setup row to the main menu"
  fi
else
  say "no menu file found — skip the Super+Space entry"
fi

# --- service --------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now wacom-pen.service
sleep 1
if systemctl --user is-active --quiet wacom-pen.service; then
  say "wacom-pen.service is running"
else
  echo "WARNING: wacom-pen.service did not start; check: journalctl --user -u wacom-pen.service" >&2
fi

# --- shell reload ---------------------------------------------------------
if command -v omarchy >/dev/null 2>&1; then
  say "restarting the Omarchy shell"
  omarchy restart shell >/dev/null 2>&1 || true
fi

echo
echo "Done. Look for the pen icon on the right side of the bar,"
echo "or press Super+Space and type 'pen'."
echo "If the widget is missing, run: omarchy restart shell"

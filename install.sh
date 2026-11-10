#!/usr/bin/env bash
# Pen Setup — install / restore into the current Omarchy session.
#
# Copies the bar plugin, the pen daemon, the CLI, the systemd unit and the
# default config, then wires the widget into the bar and the main menu.
# Safe to run again: existing config and bar layout entries are preserved.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="pen-setup"
LEGACY_ID="dbeaudry.pensetup"   # pre-1.2 plugin id, migrated on install
SHELL_JSON="$HOME/.config/omarchy/shell.json"
MENU_FILE="$HOME/.config/omarchy/extensions/omarchy-menu.jsonc"
UDEV_RULE_SRC="$HERE/files/etc/udev/rules.d/70-wacom-pen.rules"
UDEV_RULE_DST="/etc/udev/rules.d/70-wacom-pen.rules"
MODULE_CONF_SRC="$HERE/files/etc/modules-load.d/wacom-pen.conf"
MODULE_CONF_DST="/etc/modules-load.d/wacom-pen.conf"

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

# --- device permissions ---------------------------------------------------
# Arch keeps /dev/input/event* in the input group and /dev/uinput root-only,
# so the user service can neither see a tablet nor create its virtual devices.
# A udev rule granting uaccess to the active seat user fixes both without a
# logout (and without adding the user to the input group). The uinput module
# is not loaded by default, so it is placed in modules-load.d too.
SUDO=()
if command -v sudo >/dev/null 2>&1; then
  if [ -t 0 ]; then
    SUDO=(sudo)
  elif sudo -n true >/dev/null 2>&1; then
    SUDO=(sudo -n)
  fi
fi

install_root_file() {
  local src="$1"
  local dst="$2"
  local label="$3"
  [ -f "$src" ] || return 0
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    say "$label already installed: $dst"
    return 0
  fi
  if [ ${#SUDO[@]} -eq 0 ]; then
    say "SKIPPED $label: sudo is needed to install $dst"
    say "run: sudo install -m 644 '$src' '$dst'"
    return 0
  fi
  say "installing $label"
  if ! "${SUDO[@]}" install -m 644 "$src" "$dst"; then
    echo "WARNING: could not install $dst" >&2
  fi
}

install_device_access() {
  install_root_file "$UDEV_RULE_SRC" "$UDEV_RULE_DST" "the tablet udev rule"
  install_root_file "$MODULE_CONF_SRC" "$MODULE_CONF_DST" "the uinput module-load rule"
  if [ ${#SUDO[@]} -eq 0 ]; then
    say "then: sudo modprobe uinput"
    say "and:  sudo udevadm control --reload-rules && sudo udevadm trigger --action=change --subsystem-match=input"
    return 0
  fi
  say "loading the uinput module (sudo password may be required once)"
  "${SUDO[@]}" modprobe uinput >/dev/null 2>&1 || true
  "${SUDO[@]}" udevadm control --reload-rules >/dev/null 2>&1 || true
  "${SUDO[@]}" udevadm trigger --action=change --subsystem-match=input >/dev/null 2>&1 || true
  "${SUDO[@]}" udevadm trigger --action=change --name-match=/dev/uinput >/dev/null 2>&1 || true
}

install_device_access

# --- migrate from the pre-1.2 plugin id -----------------------------------
if [ -d "$HOME/.config/omarchy/plugins/$LEGACY_ID" ]; then
  rm -rf "$HOME/.config/omarchy/plugins/$LEGACY_ID"
  say "removed legacy plugin folder ($LEGACY_ID)"
fi

# --- bar widget -----------------------------------------------------------
if [ -f "$SHELL_JSON" ]; then
  python3 - "$SHELL_JSON" "$PLUGIN_ID" "$LEGACY_ID" <<'PY'
import json, sys

path, plugin, legacy = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)
layout = data.setdefault("bar", {}).setdefault("layout", {})
changed = False
for side in ("left", "center", "centerBegin", "centerEnd", "right"):
    entries = layout.get(side) or []
    rebuilt = []
    side_changed = False
    for e in entries:
        if isinstance(e, dict) and e.get("id") == legacy:
            # Drop the legacy id; keep the widget's position on the right side.
            if side == "right":
                rebuilt.append({"id": plugin})
            side_changed = True
            continue
        rebuilt.append(e)
    if side_changed:
        layout[side] = rebuilt
        changed = True
        print(f"  migrated legacy bar entry ({legacy}) on {side}")
right = layout.setdefault("right", [])
if not any(isinstance(e, dict) and e.get("id") == plugin for e in right):
    right.append({"id": plugin})
    changed = True
    print(f"  added {plugin} to the bar (right side)")
if changed:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
PY
else
  say "no shell.json found — add { \"id\": \"$PLUGIN_ID\" } to bar.layout.right yourself"
fi

# --- main menu entry ------------------------------------------------------
if [ -f "$MENU_FILE" ]; then
  python3 - "$MENU_FILE" "$PLUGIN_ID" "$LEGACY_ID" <<'PY'
import sys, time, shutil

path, plugin, legacy = sys.argv[1], sys.argv[2], sys.argv[3]
row = ('  "pensetup": {"icon":"\u270e","label":"Pen Setup",'
       '"aliases":["pen","pensetup","zenpen","wacom","tablet","stylus"],'
       '"description":"Mouse / pressure modes and per-app pen rules",'
       '"action":"omarchy-shell ' + plugin + ' toggle"},\n')
with open(path) as fh:
    text = fh.read()
if '"pensetup"' in text:
    old = '"action":"omarchy-shell %s toggle"' % legacy
    new = '"action":"omarchy-shell %s toggle"' % plugin
    if old in text:
        text = text.replace(old, new)
        with open(path, "w") as fh:
            fh.write(text)
        print("  updated the Pen Setup menu row action")
    else:
        print("  menu entry already present (pensetup)")
else:
    shutil.copy2(path, path + ".bak." + str(int(time.time())))
    idx = text.rfind("}")
    if idx == -1:
        raise SystemExit("could not find the end of the menu file")
    text = text[:idx] + row + text[idx:]
    with open(path, "w") as fh:
        fh.write(text)
    print("  added the Pen Setup row to the main menu")
PY
else
  say "no menu file found — skip the Super+Space entry"
fi

# --- service --------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable wacom-pen.service
# Restart (not just enable --now) so an update actually picks up the new daemon.
systemctl --user restart wacom-pen.service
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

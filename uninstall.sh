#!/usr/bin/env bash
# Pen Setup — remove the plugin, daemon, CLI and service.
#
# The saved config (~/.config/wacom-pen/config.json) is kept unless you pass
# --purge, so a reinstall keeps your button maps, app rules and active area.
set -euo pipefail

PLUGIN_ID="pen-setup"
LEGACY_ID="dbeaudry.pensetup"
SHELL_JSON="$HOME/.config/omarchy/shell.json"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

echo "Pen Setup — uninstalling"

systemctl --user disable --now wacom-pen.service >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/wacom-pen.service"
systemctl --user daemon-reload

rm -rf "$HOME/.config/omarchy/plugins/$PLUGIN_ID"
rm -rf "$HOME/.config/omarchy/plugins/$LEGACY_ID"
rm -f "$HOME/.local/bin/wacom-pen-daemon.py" "$HOME/.local/bin/wacom-pen-ctl"

UDEV_RULE_DST="/etc/udev/rules.d/70-wacom-pen.rules"
MODULE_CONF_DST="/etc/modules-load.d/wacom-pen.conf"
if [ -f "$UDEV_RULE_DST" ] || [ -f "$MODULE_CONF_DST" ]; then
  if [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
    sudo rm -f "$UDEV_RULE_DST" "$MODULE_CONF_DST"
    sudo udevadm control --reload-rules >/dev/null 2>&1 || true
    sudo udevadm trigger --action=change --subsystem-match=input >/dev/null 2>&1 || true
    echo "  removed the tablet udev and module-load rules"
  else
    echo "  kept the tablet udev/module-load rules; remove them with:"
    echo "    sudo rm -f $UDEV_RULE_DST $MODULE_CONF_DST"
  fi
fi

if [ "$PURGE" = 1 ]; then
  rm -rf "$HOME/.config/wacom-pen"
  echo "  removed saved config"
else
  echo "  kept saved config: ~/.config/wacom-pen/config.json"
fi

if [ -f "$SHELL_JSON" ]; then
  python3 - "$SHELL_JSON" "$PLUGIN_ID" "$LEGACY_ID" <<'PY'
import json, sys

path, plugin, legacy = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)
layout = data.get("bar", {}).get("layout", {})
removed = False
for side in ("left", "center", "centerBegin", "centerEnd", "right"):
    entries = layout.get(side) or []
    kept = [e for e in entries
            if not (isinstance(e, dict) and e.get("id") in (plugin, legacy))]
    if len(kept) != len(entries):
        layout[side] = kept
        removed = True
if removed:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print("  removed the bar entry")
else:
    print("  no bar entry to remove")
PY
fi

echo "  (the 'pensetup' row in omarchy-menu.jsonc was left in place — delete it by hand if you want it gone)"

if command -v omarchy >/dev/null 2>&1; then
  omarchy restart shell >/dev/null 2>&1 || true
fi

echo "Done."

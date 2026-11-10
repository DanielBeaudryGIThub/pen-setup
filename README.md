# Pen Setup — graphics-tablet control for Omarchy

A bar widget + user daemon that turns any graphics tablet into two switchable
personalities on [Omarchy](https://omarchy.org/) (Hyprland/Wayland):

- **Mouse mode** — absolute pointer, no pressure. Pen tip = left click, lower
  barrel button = right click, upper barrel button = middle click. Super+drag
  move and Super+middle-drag resize work like a normal mouse.
- **Pressure mode** — virtual tablet with pressure and tilt for Krita, GIMP,
  Inkscape, Xournal++ and anything else that wants a stylus.

Plus per-application rules, remappable pen/pad buttons (mouse, keyboard or
scroll), a draggable tablet→screen active-area crop, and both touch strips
mapped to smooth high-resolution scrolling with flick acceleration.

Free and open source under the [MIT license](LICENSE). No account, no
telemetry, no network access.

![Pen Setup panel](docs/panel.png)

## Downloads

Grab the latest package from **[Releases](https://github.com/DanielBeaudryGIThub/pen-setup/releases/latest)**
(`Pen-Setup-<version>.zip`), or clone this repo. Both contain the same files.

## Features

| | |
|---|---|
| **Mouse / pressure mode** | One toggle switches the pen between a plain absolute pointer and a pressure+tilting virtual tablet. |
| **Pressure sensitivity** | Four levels (Firm / Normal / Soft / Very soft) scale how quickly pen pressure builds up in pressure mode. |
| **Stable tip clicks** | While the tip is down, up to 14 px of hand shake is ignored in mouse mode (2 px in tablet mode) so a tap stays a clean click instead of drifting into a drag. |
| **Per-application rules** | Switch modes automatically by window class (`*krita*`, `*gimp*`, `*inkscape*`, `*xournal*` are pre-seeded). Add the focused app from the panel with one click. |
| **Button mapping** | Pen tip, both barrel buttons, the pad's express keys and touch strips — map each to a mouse action, a scroll action or a keyboard shortcut. |
| **Tablet active area** | Drag the corners in the panel to crop the tablet to the part of the screen you want. |
| **Touch strips** | Continuous high-resolution wheel events: a slow stroke scrolls ~24 notches, a quick flick accelerates up to ~2.2×. Sensitivity: Gentle / Normal / Fast / Very fast. |
| **Bar widget + menu row** | Lives on the right side of the Omarchy bar, and opens from Super+Space (search "pen"). |
| **CLI** | `wacom-pen-ctl` for scripts: mode, buttons, strips, area, status. |

## Requirements

- Omarchy 4.x on Hyprland (Wayland). Other Quickshell/Hyprland setups may work
  with small path changes.
- `python` with [`python-evdev`](https://python-evdev.readthedocs.io/):

  ```bash
  sudo pacman -S python-evdev
  ```

- Any graphics tablet (Wacom, Huion, XP-Pen, …). The daemon auto-detects a
  tablet purely by its input capabilities — a stylus pen and a pad with
  express keys or touch strips — so there are no vendor ids or model names to
  edit. To limit detection to specific vendors or name patterns, set
  `devices` in `~/.config/wacom-pen/config.json`.
- Your user must be able to read the tablet's `/dev/input/event*` nodes and
  create a `uinput` device. The installer adds
  `/etc/udev/rules.d/70-wacom-pen.rules` (access through `uaccess`, no logout
  or group change needed) and `/etc/modules-load.d/wacom-pen.conf` (loads the
  `uinput` module). Run `./install.sh` from a terminal so it can ask for sudo
  once; if you copy the files by hand, install those files yourself as well.

## Install

From a release zip:

```bash
cd ~/Downloads
unzip Pen-Setup-<version>.zip
cd Pen-Setup-<version>
./install.sh
```

Or from a clone:

```bash
git clone https://github.com/DanielBeaudryGIThub/pen-setup.git
cd pen-setup
./install.sh
```

The installer:

1. copies the plugin, daemon, CLI, systemd unit and default config into
   `~/.config` / `~/.local`;
2. installs `/etc/udev/rules.d/70-wacom-pen.rules` and
   `/etc/modules-load.d/wacom-pen.conf` (asks for sudo once) and loads `uinput`
   so the daemon can read the tablet and create virtual input devices;
3. adds `pen-setup` to the right side of the Omarchy bar (once — it
   will not duplicate the entry; a `dbeaudry.pensetup` entry from an older
   release is migrated automatically);
4. adds a **Pen Setup** row to the Super+Space menu (a `.bak` copy of the menu
   file is made first);
5. enables and starts `wacom-pen.service`;
6. restarts the Omarchy shell so the widget appears.

The installer is idempotent: re-running it keeps your existing
`~/.config/wacom-pen/config.json` and bar layout.

## Use

- Click the pen icon in the bar, or run
  `omarchy-shell pen-setup toggle`.
- **Mouse mode / Pressure mode** — the toggle at the top.
- **Per-application** — rules switch modes by window class when you focus an
  app. *Add "…"* uses the currently focused window class.
- **Tablet · Active area** — drag the corner handles to crop.
- **Button mapping** — pen tip, barrel buttons, express keys, touch strips.
- **Scroll sensitivity** — Gentle / Normal / Fast / Very fast.
- **Pressure sensitivity** — Firm / Normal / Soft / Very soft.
- **Tip click stabilisation** — movement under 14 px (mouse mode) / 2 px
  (tablet mode) while the tip is down is
  treated as shake, so a tap does not turn into a drag.
- **Switch modes by app** — turn the per-app rules on or off.

### Touch strips

Both strips ship mapped identically (`Scroll up / down`, Normal) and behave the
same way: a stroke of the same length and speed scrolls the same distance on
either side. They emit high-resolution wheel events, so scrolling is smooth
rather than stepped; lifting the finger stops instantly. Either strip can be
set to vertical/horizontal, normal or inverted, or `none` from the panel,
independently of the other.

## CLI

```bash
wacom-pen-ctl status
wacom-pen-ctl set-mode mouse|tablet|native
wacom-pen-ctl set-button stylus "key:ctrl+z"
wacom-pen-ctl set-strip-sensitivity 1.75
wacom-pen-ctl set-pressure-sensitivity 2
wacom-pen-ctl reset-area
```

`wacom-pen-ctl status --json` prints the whole state for scripting.
Your settings live in `~/.config/wacom-pen/config.json`.

## Troubleshooting

- **Widget missing from the bar** — `omarchy restart shell` (a full shell
  restart is required after plugin changes; `rescanPlugins` alone is not
  enough).
- **Tablet is plugged in but the panel says "input access denied"** — the
  daemon cannot open `/dev/input/event*` or `/dev/uinput`. Re-run `./install.sh`
  from a terminal to install the udev rule, then
  `systemctl --user restart wacom-pen.service`. (If you installed by hand,
  install `files/etc/udev/rules.d/70-wacom-pen.rules` and
  `files/etc/modules-load.d/wacom-pen.conf` under `/etc`, then run
  `sudo modprobe uinput && sudo udevadm control --reload-rules && sudo udevadm trigger --action=change --subsystem-match=input`.)
- **Daemon exits with "virtual pointer not registered"** — another process may
  hold the pen; stop old helper scripts and restart
  `systemctl --user restart wacom-pen.service`.
- **Pressure apps see no pressure** — add a per-app rule for that app (the
  panel's *Add "…"* button uses the focused window class).
- **Golden cursor / no pointer** — make sure `python-evdev` is installed and
  the user can access the tablet's event devices.
- **A strip scrolls the opposite way** — set that strip to `Scroll down / up`
  (or `Scroll left / right`) in the panel; the two strips are configured
  independently.
- **Logs** — `journalctl --user -u wacom-pen.service -n 40`.

## Uninstall

```bash
./uninstall.sh            # keeps ~/.config/wacom-pen/config.json
./uninstall.sh --purge    # also removes the saved config
```

The `pensetup` row in `~/.config/omarchy/extensions/omarchy-menu.jsonc` is left
in place; delete it by hand if you want it gone.

## Project layout

```
install.sh                                  installer (bar + menu + service)
uninstall.sh                                remover (keeps config by default)
files/.local/bin/wacom-pen-daemon.py        the daemon (owns pen + pad)
files/.local/bin/wacom-pen-ctl              CLI used by the panel
files/.config/omarchy/plugins/pen-setup/
    manifest.json                           plugin manifest (author, version)
    Panel.qml                               bar widget / panel UI
files/.config/systemd/user/wacom-pen.service
files/.config/wacom-pen/config.json         default settings
files/etc/udev/rules.d/70-wacom-pen.rules   tablet + uinput access for the user
files/etc/modules-load.d/wacom-pen.conf     loads the uinput module
scripts/package.sh                          builds the release zip in dist/
docs/panel.png                              screenshot used above
```

## License

MIT — see [LICENSE](LICENSE).

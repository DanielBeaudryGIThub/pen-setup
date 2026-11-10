#!/usr/bin/env python3
"""Pen setup daemon for any graphics tablet.

Finds a tablet by its input capabilities (a stylus pen, and a pad with express
keys or touch strips — no vendor or model name required), owns its pen and pad,
and exposes two emulation modes plus per-application rules, per-button action
mappings, and a configurable active area. Everything is controlled over a small
JSON socket so the Omarchy bar plugin can change settings without restarting
anything.

Modes
    mouse   Absolute pointer (no pressure/tilt). Tip = left, lower barrel
            button = right, upper = middle by default. Window manipulation
            (Super+drag move, Super+MMB resize) works because Hyprland sees a
            mouse.
    tablet  Native tablet with pressure and tilt. The tip drives the virtual
            tablet; barrel buttons are remapped on a button-only virtual mouse
            so apps never see them as stylus clicks. Super+drag window
            manipulation does NOT work in this mode.
    native  Pen and pad released untouched (no emulation). Disabling the setup
            lands here.

Button actions (pen tip, both barrel buttons, all pad express keys, the ring's
press button, and any extra pad buttons the tablet reports)
    none
    mouse:left | mouse:right | mouse:middle | mouse:back | mouse:forward
    key:<combo>            e.g. key:ctrl+z, key:alt+tab, key:f5

Touch strip / ring ("roulette") actions
    none
    scroll:vertical        turn/slide -> wheel
    scroll:vertical-inv    same, direction reversed
    scroll:horizontal      turn/slide -> horizontal wheel
    scroll:horizontal-inv  same, direction reversed

Active area
    "area": {"x":0,"y":0,"w":1,"h":1} selects the part of the tablet surface
    that is stretched over the whole screen. Applies in both mouse and tablet
    mode, so cropping the tablet here crops it for every application.

Tip stabilisation
    While the pen tip is in contact, movement within a 2-pixel radius of the
    press point is treated as hand shake and is not forwarded. This keeps a tap
    a clean click instead of letting tiny movement turn it into a drag.

Control socket: $XDG_RUNTIME_DIR/wacom-pen.sock
Config:         ~/.config/wacom-pen/config.json
CLI:            wacom-pen-ctl
"""

import fnmatch
import glob
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

import evdev
from evdev import AbsInfo, UInput, ecodes as e

# Any graphics tablet, not just Wacom, and not any one model: devices are
# matched by their input capabilities (a stylus pen, and a pad with express
# keys / touch strips), with no vendor requirement by default. Override
# "devices" in ~/.config/wacom-pen/config.json to narrow the search by name or
# to limit it to specific USB vendors (see README).
# All uinput devices created by this daemon report this phys string, so they
# can never be mistaken for the physical tablet.
VIRTUAL_PHYS = "py-evdev-uinput"
VIRTUAL_SUFFIXES = (" Mouse", " (remapped)", " Buttons", " Keys")
# UINPUT_MAX_NAME_SIZE is 80 bytes; keep the derived names safely below it.
MAX_NAME_BASE = 60

# Tablet pads expose their express keys as BTN_0..BTN_9 (some models use fewer,
# some use all ten). The list is built from the pad's real capabilities.
PAD_KEY_CODES = tuple(range(e.BTN_0, e.BTN_9 + 1))
# A pad is only recognized as one when it exposes at least this many express
# keys (or touch strips), so keyboards/mice with a stray BTN_0 (e.g. a craft
# dial) are never mistaken for a tablet pad.
MIN_PAD_KEYS = 2
ABS_MAX = 65535
# A pen tap must stay a click. While the tip is down, pointer movement within
# this radius (in screen pixels) is treated as hand shake and not forwarded,
# so a micro-movement cannot turn the click into a drag before it is released.
CLICK_SHAKE_PX = 2.0
SCREEN_SPAN_TTL = 30.0

CONFIG_DIR = os.path.expanduser("~/.config/wacom-pen")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "wacom-pen.sock")

PEN_BUTTONS = ("tip", "stylus", "stylus2")
PAD_BUTTONS = tuple(f"pad:{i}" for i in range(len(PAD_KEY_CODES)))
STRIP_BUTTONS = ("strip:0", "strip:1")
RING_BUTTONS = ("ring:0",)

MOUSE_ACTIONS = {
    "left": e.BTN_LEFT,
    "right": e.BTN_RIGHT,
    "middle": e.BTN_MIDDLE,
    "back": e.BTN_SIDE,
    "forward": e.BTN_EXTRA,
}
ALL_MOUSE_BUTTONS = tuple(MOUSE_ACTIONS.values())

# Strip gestures. "vertical" turns finger travel into wheel movement; the
# "-inv" variants flip the direction. Movement is emitted as high-resolution
# wheel events (120 units = one notch) so scrolling is continuous rather than
# a burst of discrete notches.
SCROLL_ACTIONS = {
    "scroll:vertical": (e.REL_WHEEL_HI_RES, e.REL_WHEEL, 1),
    "scroll:vertical-inv": (e.REL_WHEEL_HI_RES, e.REL_WHEEL, -1),
    "scroll:horizontal": (e.REL_HWHEEL_HI_RES, e.REL_HWHEEL, 1),
    "scroll:horizontal-inv": (e.REL_HWHEEL_HI_RES, e.REL_HWHEEL, -1),
}
STRIP_SPAN = 4096.0
HI_RES_PER_NOTCH = 120
# Notches produced by sliding the whole length of a strip at Normal speed.
# The strips are short, so this is deliberately generous: one comfortable
# stroke should move a page, not ten lines.
NOTCHES_PER_SWEEP = 24.0
HI_RES_PER_UNIT = NOTCHES_PER_SWEEP * HI_RES_PER_NOTCH / STRIP_SPAN
# A full turn of the touch ring ("roulette") scrolls this many notches at
# Normal speed (one wheel rotation, broadly).
RING_NOTCHES_PER_TURN = 24.0
# Flick acceleration: a fast stroke covers more ground than a slow one, so a
# quick swipe scrolls a lot without making slow positioning twitchy. The
# factor ramps from 1x at STRIP_FAST_FROM to STRIP_ACCEL_MAX at STRIP_FAST_TO
# (units per second along the strip).
STRIP_FAST_FROM = 1500.0
STRIP_FAST_TO = 9000.0
STRIP_ACCEL_MAX = 2.2
# A position report that arrives after this long belongs to a new stroke, even
# if the touch flag was not seen: anchor it instead of scrolling the jump.
STRIP_STALE_GAP = 0.25

DEFAULT_BUTTONS = {
    "tip": "mouse:left",
    "stylus": "mouse:right",
    "stylus2": "mouse:middle",
    **{f"pad:{i}": "none" for i in range(8)},
    "strip:0": "scroll:vertical",
    "strip:1": "scroll:vertical",
    "ring:0": "scroll:vertical",
}

DEFAULT_CONFIG = {
    "version": 2,
    "enabled": True,
    "mode": "mouse",
    "auto": True,
    "rules": {
        "*krita*": "tablet",
        "*gimp*": "tablet",
        "*inkscape*": "tablet",
        "*xournal*": "tablet",
    },
    "devices": {"pen": "", "pad": "", "vendors": []},
    "buttons": dict(DEFAULT_BUTTONS),
    "stripSensitivity": 1.0,
    "pressureSensitivity": 1.0,
    "area": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
}

# Touch strips report absolute positions on ABS_RX (left) and ABS_RY (right);
# ABS_MISC flags touch start (15) and touch end (0). Both axes are only used
# when the pad actually has them (see attach_pad).
STRIP_AXES = {e.ABS_RX: 0, e.ABS_RY: 1}

STOP = False


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


DEBUG = os.environ.get("WACOM_DEBUG", "0") == "1"


def debug(msg):
    if DEBUG:
        log(f"debug: {msg}")


# --------------------------------------------------------------------------
# config


def clamp(value, low, high):
    return max(low, min(high, value))


def clean_area(raw):
    area = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    if not isinstance(raw, dict):
        return area
    try:
        area["x"] = clamp(float(raw.get("x", 0.0)), 0.0, 0.99)
        area["y"] = clamp(float(raw.get("y", 0.0)), 0.0, 0.99)
        area["w"] = clamp(float(raw.get("w", 1.0)), 0.05, 1.0)
        area["h"] = clamp(float(raw.get("h", 1.0)), 0.05, 1.0)
    except (TypeError, ValueError):
        return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    if area["x"] + area["w"] > 1.0:
        area["w"] = 1.0 - area["x"]
    if area["y"] + area["h"] > 1.0:
        area["h"] = 1.0 - area["y"]
    return area


def valid_action(action):
    if not isinstance(action, str):
        return False
    if action == "none":
        return True
    if action.startswith("mouse:"):
        return action[6:] in MOUSE_ACTIONS
    if action.startswith("key:"):
        return len(action) > 4
    if action in SCROLL_ACTIONS:
        return True
    return False


def clean_gain(raw, fallback=1.0):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value != value or value <= 0:  # NaN or nonsense
        return fallback
    return clamp(round(value, 2), 0.25, 4.0)


def clean_devices(raw):
    """Optional discovery overrides: name substrings and USB vendor ids.

    Empty hints mean "pick any matching device", so the daemon works out of
    the box on any graphics tablet. Vendors are kept as four-digit hex strings
    so the config file stays readable; an empty list disables the vendor filter
    (the default).
    """
    out = {"pen": "", "pad": "", "vendors": []}
    if isinstance(raw, dict):
        out["pen"] = str(raw.get("pen", "") or "").strip()
        out["pad"] = str(raw.get("pad", "") or "").strip()
        items = raw.get("vendors")
        if not isinstance(items, (list, tuple)):
            items = [items] if items else []
    else:
        items = []
    for item in items:
        try:
            value = int(str(item), 16) if isinstance(item, str) else int(item)
        except (TypeError, ValueError):
            continue
        if 0 < value <= 0xFFFF:
            out["vendors"].append(f"{value:04x}")
    return out


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config is not an object")
    except FileNotFoundError:
        data = json.loads(json.dumps(DEFAULT_CONFIG))
    except (OSError, ValueError) as exc:
        log(f"config unreadable ({exc}); using defaults")
        data = json.loads(json.dumps(DEFAULT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(data)
    if cfg.get("mode") not in ("mouse", "tablet"):
        cfg["mode"] = "mouse"

    rules = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else {}
    cfg["rules"] = {
        str(k): v for k, v in rules.items()
        if isinstance(k, str) and k.strip() and v in ("mouse", "tablet")
    }

    buttons = dict(DEFAULT_BUTTONS)
    raw_buttons = data.get("buttons") if isinstance(data.get("buttons"), dict) else {}
    for name, action in raw_buttons.items():
        if name in buttons or name in PAD_BUTTONS or name in STRIP_BUTTONS:
            if valid_action(action):
                buttons[name] = action
    cfg["buttons"] = buttons
    cfg["stripSensitivity"] = clean_gain(data.get("stripSensitivity"), 1.0)
    cfg["pressureSensitivity"] = clean_gain(data.get("pressureSensitivity"), 1.0)
    cfg["devices"] = clean_devices(data.get("devices"))
    cfg["area"] = clean_area(data.get("area"))
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["auto"] = bool(cfg.get("auto", True))
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------
# devices


def virtual_names(source_name):
    """Names for the uinput devices, derived from the real tablet's name.

    Keeping the source name (e.g. "Wacom Intuos Pro Pen") in the generated
    names means the same Hyprland device rules keep matching: Hyprland turns
    "… Pen Mouse" into "…-pen-mouse".
    """
    base = (source_name or "Pen Tablet").strip()
    if base.lower().endswith(" pen"):
        base = base[:-4]
    base = base[:MAX_NAME_BASE].rstrip()
    stem = base + " Pen" if base else "Pen Tablet"
    return {
        "pointer": stem + " Mouse",
        "tablet": stem + " (remapped)",
        "buttons": stem + " Buttons",
        "keys": stem + " Keys",
    }


def device_facts(dev):
    caps = dev.capabilities()
    keys = set(caps.get(e.EV_KEY, ()))
    abs_codes = {code for code, _ in caps.get(e.EV_ABS, ())}
    return keys, abs_codes


def is_virtual(dev):
    """True for the uinput devices this daemon creates itself."""
    if dev.phys == VIRTUAL_PHYS:
        return True
    name = dev.name or ""
    return any(name.endswith(suffix) for suffix in VIRTUAL_SUFFIXES)


def read_sysfs_bitmap(path):
    """Read a sysfs capabilities bitmap (most-significant word first)."""
    try:
        with open(path) as fh:
            words = fh.read().split()
    except OSError:
        return set()
    value = 0
    for word in words:
        try:
            value = (value << 64) | int(word, 16)
        except ValueError:
            return set()
    return {i for i in range(value.bit_length()) if value >> i & 1}


def inaccessible_tablets():
    """Tablet event nodes that exist but this user cannot open.

    evdev.list_devices() only reports nodes the process can read, so a tablet
    hidden by device permissions looks exactly like an empty machine. Ask the
    kernel through sysfs (world-readable) instead, so the panel can say the
    tablet is there but access is denied rather than pretending it is absent.
    """
    found = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if os.access(path, os.R_OK):
            continue
        sysdir = os.path.join(
            "/sys/class/input", os.path.basename(path), "device"
        )
        try:
            with open(os.path.join(sysdir, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        keys = read_sysfs_bitmap(os.path.join(sysdir, "capabilities", "key"))
        abs_codes = read_sysfs_bitmap(
            os.path.join(sysdir, "capabilities", "abs")
        )
        if e.BTN_TOOL_PEN in keys and e.ABS_X in abs_codes and e.ABS_Y in abs_codes:
            found.append({"path": path, "name": name})
    return found


def uinput_device_path(ui, timeout=2.0):
    """Find the event node created by a UInput without opening it.

    python-evdev's UInput.device is None when the new node is not readable yet
    (or when its sysfs lookup fails on newer kernels). The daemon only needs the
    path for udevadm, so locate it through sysfs instead of crashing on
    ``ui.device.path``.
    """
    if ui.device is not None:
        return ui.device.path
    deadline = time.time() + timeout
    while True:
        for sysdir in sorted(glob.glob("/sys/devices/virtual/input/input*")):
            try:
                with open(os.path.join(sysdir, "name")) as fh:
                    name = fh.read().strip()
                with open(os.path.join(sysdir, "phys")) as fh:
                    phys = fh.read().strip()
            except OSError:
                continue
            if name != ui.name or (phys and phys != VIRTUAL_PHYS):
                continue
            for entry in sorted(os.listdir(sysdir)):
                if entry.startswith("event") and entry[5:].isdigit():
                    path = os.path.join("/dev/input", entry)
                    if os.path.exists(path):
                        return path
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


def permission_state():
    """Describe whether this user can reach the tablet and /dev/uinput."""
    tablets = inaccessible_tablets()
    input_ok = not tablets
    uinput_ok = os.access("/dev/uinput", os.W_OK)
    if not input_ok and not uinput_ok:
        hint = (
            "A tablet is connected but Pen Setup cannot read its "
            "/dev/input nodes or create /dev/uinput devices. Re-run "
            "./install.sh to install the udev rule, then restart the daemon."
        )
    elif not input_ok:
        hint = (
            "A tablet is connected but Pen Setup cannot read its "
            "/dev/input nodes. Re-run ./install.sh to install the udev rule."
        )
    elif not uinput_ok:
        hint = (
            "Pen Setup cannot create /dev/uinput devices. Re-run ./install.sh "
            "to install the udev rule."
        )
    else:
        hint = ""
    return {
        "input": input_ok,
        "uinput": uinput_ok,
        "tablets": tablets,
        "denied": not (input_ok and uinput_ok),
        "hint": hint,
    }


PEN_TOOL_CODES = {
    e.BTN_TOUCH, e.BTN_STYLUS, e.BTN_STYLUS2,
    e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOOL_BRUSH, e.BTN_TOOL_PENCIL,
    e.BTN_TOOL_AIRBRUSH, e.BTN_TOOL_FINGER, e.BTN_TOOL_MOUSE, e.BTN_TOOL_LENS,
}
# Extra pad buttons are only surfaced in small numbers, so a device that also
# reports a few stray KEY codes cannot flood the mapping UI.
PAD_EXTRA_MAX = 32


def ev_key_name(code):
    """A stable code name (e.g. "KEY_A" or "BTN_0") for config ids."""
    for name in evdev.ecodes.bytype.get(e.EV_KEY, {}).get(code, []):
        if isinstance(name, str) and name:
            return name
    return f"KEY_0x{code:x}"


def pad_extra_buttons(keys):
    """Mappable pad buttons beyond the BTN_0..BTN_9 express keys.

    Some tablets report their pad/express keys as keyboard KEY_* codes rather
    than BTN codes, so those are surfaced as extra buttons. Pen tools, mouse
    buttons, and anything the virtual keyboard cannot emit are ignored.
    """
    return sorted(c for c in keys if c in KEY_CODES)


def extra_button_id(code):
    """The config id (e.g. "extra:KEY_A") for an extra pad button."""
    return f"extra:{ev_key_name(code)}"


def is_pen_device(dev, hint=""):
    keys, abs_codes = device_facts(dev)
    # A pen interface has a stylus tool and absolute X/Y. Touch digitizers
    # (ABS_MT_*) are excluded so a laptop touchscreen is not grabbed as a pen.
    if e.ABS_MT_POSITION_X in abs_codes:
        return False
    if not (e.BTN_TOOL_PEN in keys and e.ABS_X in abs_codes and e.ABS_Y in abs_codes):
        return False
    return not hint or hint in (dev.name or "").lower()


def is_pad_device(dev, hint=""):
    keys, abs_codes = device_facts(dev)
    # The pen interface and touch digitizers are not pads.
    if e.BTN_TOOL_PEN in keys or e.ABS_MT_POSITION_X in abs_codes:
        return False
    express_keys = sum(1 for code in keys if code in PAD_KEY_CODES)
    has_buttons = express_keys >= MIN_PAD_KEYS
    has_strips = e.ABS_RX in abs_codes and e.ABS_RY in abs_codes
    has_ring = e.ABS_WHEEL in abs_codes
    if not (has_buttons or has_strips or has_ring):
        return False
    return not hint or hint in (dev.name or "").lower()


def candidate_devices(devices):
    """Yield every readable input device that might be a tablet.

    `devices["vendors"]` is a set of four-digit hex vendor ids; when it is
    empty (the default) the vendor is not used as a filter and matching is done
    purely on capabilities.
    """
    try:
        vendors = {int(str(v), 16) for v in devices.get("vendors") or []}
    except (TypeError, ValueError):
        vendors = set()
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        try:
            if is_virtual(dev):
                dev.close()
                continue
            if vendors and dev.info.vendor not in vendors:
                dev.close()
                continue
        except OSError:
            dev.close()
            continue
        yield dev


def find_pen(devices):
    hint = (devices.get("pen") or "").lower()
    for dev in candidate_devices(devices):
        if is_pen_device(dev, hint):
            return dev
        dev.close()
    return None


def find_pad_devices(devices, pen):
    """Return the tablet's pad companion devices, each with its control roles.

    A tablet can expose its pad controls across several event nodes that share
    the pen's `phys`. Match on phys, and classify each node by capabilities so
    express keys, touch strips, a touch ring (ABS_WHEEL) and a physical dial
    (REL_WHEEL) are all found no matter how the vendor splits them.
    """
    if pen is None:
        return []
    hint = (devices.get("pad") or "").lower()
    out = []
    for dev in candidate_devices(devices):
        if dev is pen:
            dev.close()
            continue
        try:
            same_phys = dev.phys == pen.phys
        except OSError:
            dev.close()
            continue
        if not same_phys:
            dev.close()
            continue
        if is_virtual(dev) or is_pen_device(dev):
            dev.close()
            continue
        if hint and hint not in (dev.name or "").lower():
            dev.close()
            continue
        keys, abs_codes = device_facts(dev)
        rel_codes = {c for c in dev.capabilities().get(e.EV_REL, ())}
        roles = []
        if sum(1 for c in keys if c in PAD_KEY_CODES) >= MIN_PAD_KEYS:
            roles.append("keys")
        if e.ABS_RX in abs_codes and e.ABS_RY in abs_codes:
            roles.append("strip")
        if e.ABS_WHEEL in abs_codes:
            roles.append("ring_abs")
        if e.REL_WHEEL in rel_codes:
            roles.append("ring_rel")
        if pad_extra_buttons(keys):
            roles.append("extra")
        if not roles:
            dev.close()
            continue
        out.append((dev, roles))
    # Prefer the express-key node as the pad's identity; keep the rest too.
    prio = {"keys": 4, "strip": 3, "ring_abs": 2, "ring_rel": 2, "extra": 1}
    out.sort(key=lambda dr: -max(prio.get(r, 0) for r in dr[1]))
    return out


def wait_for_props(path, needles, timeout=4.0):
    """udev must classify a uinput device or libinput silently ignores it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            seen = subprocess.run(
                ["udevadm", "info", "--query=property", "--name", path],
                capture_output=True, text=True, timeout=2,
            ).stdout
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
            continue
        if all(n in seen for n in needles):
            return True
        time.sleep(0.15)
    return False


def build_pointer(name):
    return UInput(
        {
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
            ],
            e.EV_KEY: list(ALL_MOUSE_BUTTONS),
        },
        name=name,
        input_props=[e.INPUT_PROP_POINTER],
    )


def build_tablet(src, name):
    """Virtual tablet that keeps pressure/tilt but never emits barrel buttons.

    BTN_STYLUS/BTN_STYLUS2 are declared because udev only tags a device as a
    tablet when they are present, but they are never emitted: apps get their
    remapped clicks from a button-only virtual mouse instead.
    """
    caps = src.capabilities(absinfo=True)
    abs_caps = {code: ai for code, ai in caps.get(e.EV_ABS, [])}
    wanted = (
        e.ABS_X, e.ABS_Y, e.ABS_Z, e.ABS_RZ, e.ABS_THROTTLE, e.ABS_WHEEL,
        e.ABS_PRESSURE, e.ABS_DISTANCE, e.ABS_TILT_X, e.ABS_TILT_Y, e.ABS_MISC,
    )
    abs_list = []
    for code in wanted:
        ai = abs_caps.get(code)
        if ai is not None:
            abs_list.append(
                (code, AbsInfo(ai.value, ai.min, ai.max, ai.fuzz, ai.flat, ai.resolution))
            )
    keys = [
        e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH,
        e.BTN_STYLUS, e.BTN_STYLUS2,
    ]
    return UInput(
        {e.EV_ABS: abs_list, e.EV_KEY: keys},
        name=name,
        input_props=[e.INPUT_PROP_POINTER],
    )


def build_buttons(name):
    return UInput(
        {
            e.EV_KEY: list(ALL_MOUSE_BUTTONS),
            e.EV_REL: [
                e.REL_X, e.REL_Y,
                e.REL_WHEEL, e.REL_HWHEEL,
                e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES,
            ],
        },
        name=name,
        input_props=[e.INPUT_PROP_POINTER],
    )


# Keys the virtual keyboard can type. Declared up front; sending anything not
# declared is silently dropped by the kernel.
def key_codes():
    names = []
    names += [chr(c) for c in range(ord("a"), ord("z") + 1)]
    names += [str(d) for d in range(10)]
    names += [f"f{i}" for i in range(1, 25)]
    names += [
        "esc", "tab", "enter", "space", "backspace", "delete", "insert",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
        "minus", "equal", "bracketleft", "bracketright", "backslash",
        "semicolon", "apostrophe", "grave", "comma", "period", "slash",
        "leftctrl", "rightctrl", "leftshift", "rightshift",
        "leftalt", "rightalt", "leftmeta", "rightmeta",
        "capslock", "numlock", "scrolllock", "print", "pause",
        "kp0", "kp1", "kp2", "kp3", "kp4", "kp5", "kp6", "kp7", "kp8", "kp9",
    ]
    codes = set()
    for name in names:
        code = getattr(e, "KEY_" + name.upper(), None)
        if code is not None:
            codes.add(code)
    return sorted(codes)


KEY_CODES = key_codes()

MODIFIER_ALIASES = {
    "ctrl": e.KEY_LEFTCTRL,
    "control": e.KEY_LEFTCTRL,
    "shift": e.KEY_LEFTSHIFT,
    "alt": e.KEY_LEFTALT,
    "super": e.KEY_LEFTMETA,
    "meta": e.KEY_LEFTMETA,
    "win": e.KEY_LEFTMETA,
}

KEY_ALIASES = {
    "escape": e.KEY_ESC,
    "return": e.KEY_ENTER,
    "del": e.KEY_DELETE,
    "ins": e.KEY_INSERT,
    "pgup": e.KEY_PAGEUP,
    "pgdn": e.KEY_PAGEDOWN,
    "plus": e.KEY_EQUAL,
    "backtick": e.KEY_GRAVE,
    "bracketleft": e.KEY_LEFTBRACE,
    "bracketright": e.KEY_RIGHTBRACE,
}


def resolve_key(name):
    name = name.strip().lower()
    if not name:
        return None
    if name in MODIFIER_ALIASES:
        return MODIFIER_ALIASES[name]
    if name in KEY_ALIASES:
        return KEY_ALIASES[name]
    if len(name) == 1:
        if "a" <= name <= "z":
            return getattr(e, "KEY_" + name.upper())
        if "0" <= name <= "9":
            return getattr(e, "KEY_" + name)
    code = getattr(e, "KEY_" + name.upper(), None)
    if code is not None and code in KEY_CODES:
        return code
    return None


def build_keyboard(name):
    return UInput(
        {e.EV_KEY: KEY_CODES},
        name=name,
        input_props=[],
    )


# --------------------------------------------------------------------------
# Hyprland active window


def hypr_signature():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if sig:
        return sig
    base = os.path.join(RUNTIME_DIR, "hypr")
    try:
        entries = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return ""
    return entries[0] if entries else ""


def hypr_socket_path():
    sig = hypr_signature()
    if not sig:
        return ""
    return os.path.join(RUNTIME_DIR, "hypr", sig, ".socket2.sock")


def hyprctl_active_class():
    env = dict(os.environ)
    sig = hypr_signature()
    if sig:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    try:
        out = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True, text=True, timeout=2, env=env,
        ).stdout
        data = json.loads(out) if out.strip() else {}
        return str(data.get("class") or "")
    except Exception:  # noqa: BLE001
        return ""


def hyprctl_screen_span():
    """Logical size of the Hyprland monitor layout, for pixel-sized thresholds."""
    env = dict(os.environ)
    sig = hypr_signature()
    if sig:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True, text=True, timeout=2, env=env,
        ).stdout
        monitors = json.loads(out) if out.strip() else []
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(monitors, list) or not monitors:
        return None

    left = top = right = bottom = None
    for monitor in monitors:
        try:
            scale = float(monitor.get("scale") or 1.0)
            if scale <= 0:
                scale = 1.0
            x = float(monitor.get("x", 0))
            y = float(monitor.get("y", 0))
            w = float(monitor.get("width", 0)) / scale
            h = float(monitor.get("height", 0)) / scale
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        left = x if left is None else min(left, x)
        top = y if top is None else min(top, y)
        right = x + w if right is None else max(right, x + w)
        bottom = y + h if bottom is None else max(bottom, y + h)
    if left is None or right is None or right <= left or bottom <= top:
        return None
    return round(right - left), round(bottom - top)


class HyprWatcher:
    """Subscribes to Hyprland's event socket; falls back to polling."""

    def __init__(self):
        self.sock = None
        self.buf = b""
        self.active = ""
        self.next_connect = 0.0
        self.next_poll = 0.0
        self.want_poll = True

    def poll(self, force=False):
        now = time.time()
        if not force and now < self.next_poll:
            return False
        self.next_poll = now + 5.0
        cls = hyprctl_active_class()
        if cls != self.active:
            self.active = cls
            return True
        return False

    def ensure_connected(self):
        now = time.time()
        if self.sock is not None or now < self.next_connect:
            return
        self.next_connect = now + 3.0
        path = hypr_socket_path()
        if not path:
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.setblocking(False)
            s.connect(path)
        except OSError:
            close_quietly(s)
            return
        self.sock = s
        self.buf = b""
        log("listening to Hyprland event socket")

    def fileno(self):
        return self.sock.fileno() if self.sock is not None else -1

    def pump(self):
        changed = False
        try:
            chunk = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            chunk = b""
        if not chunk:
            close_quietly(self.sock)
            self.sock = None
            self.buf = b""
            self.next_connect = time.time() + 2.0
            return False
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace")
            if text.startswith("activewindow>>"):
                payload = text.split(">>", 1)[1]
                cls = payload.split(",", 1)[0]
                if cls != self.active:
                    self.active = cls
                    changed = True
            elif text.startswith(("openwindow>>", "closewindow>>", "workspace>>",
                                  "focusedmon>>", "fullscreen>>", "changefloatingmode>>")):
                self.want_poll = True
        return changed


def close_quietly(sock):
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


# --------------------------------------------------------------------------
# daemon


class PenDaemon:
    def __init__(self):
        self.cfg = load_config()
        self.active = ""
        self.effective = "native"

        self.pen = None
        self.ax = None
        self.ay = None
        self.press = None
        self.pos = [0, 0]
        # Pen proximity (None = not seen yet, True = hovering, False = lifted).
        # Mouse mode uses it to ignore the coordinates a tablet reports while
        # the pen is out of range, and to re-send the last good position on
        # lift-off so the cursor stays put instead of vanishing.
        self.proximity = None
        self.screen_pos = None
        self.frame_pos = None
        # Click shake stabilisation: while the tip is in contact, small
        # movements around the press point are held back until the pointer has
        # travelled CLICK_SHAKE_PX pixels.
        self.tip_down = False
        self.click_anchor = None
        self.click_unlocked = False
        # Which virtual button the current tip contact is holding. Tracking it
        # lets the daemon repair a missed press/release instead of leaving the
        # virtual mouse stuck or dropping a tap.
        self.tip_button = None
        self.tip_device = None
        # Set when release_all() already let the tip button go (mode switch,
        # disable, config reload). The physical release that follows must not
        # be turned into a synthesized click.
        self.tip_ignore_release = False
        self.screen_span_px = (1920, 1080)
        self.screen_span_at = 0.0
        self.proximity_out_at = 0.0
        self.pad = None
        self.grabbed = False
        self.pads_grabbed = False
        self.held = {}
        # The tablet pad is built from one or more companion event nodes that
        # share the pen's phys. A single device (e.g. an Intuos3 pad) can hold
        # several roles; a tablet like the HUION Q620M splits express keys,
        # touch strips and the dial across separate nodes. Roles:
        #   keys     -> express keys (BTN_0..BTN_9)
        #   strip    -> touch strips (ABS_RX / ABS_RY)
        #   ring_abs -> touch ring reported as ABS_WHEEL
        #   ring_rel -> physical dial reported as REL_WHEEL
        #   extra    -> extra keyboard-style buttons
        self.pads = []          # [(dev, [role, ...]), ...]
        self.pad_keys = []
        self.pad_key_index = {}
        self.strip_axes = {}
        self.ring_kind = None           # "abs" or "rel" or None
        self.ring_min = 0
        self.ring_max = 0
        self.ring_last = None
        self.ring_acc = 0.0
        self.ring_notch = 0.0
        self.ring_time = None
        self.ring_speed = 0.0
        self.ring_btn_ids = []          # ["ring-btn:0", ...] dial press buttons
        self.ring_btn_index = {}        # EV_KEY code -> index
        self.extra_keys = []
        self.extra_key_index = {}
        # uinput names for the current tablet (derived from its name once it
        # is found, so a different tablet gets its own correctly named devices).
        self.virtual_names = None
        # Touch strip gesture state: last absolute position per strip, the
        # high-resolution scroll accumulated so far, and the fraction of a
        # whole notch still owed to clients that only read REL_WHEEL.
        self.strip_last = [None, None]
        self.strip_acc = [0.0, 0.0]
        self.strip_notch = [0.0, 0.0]
        self.strip_time = [None, None]
        self.strip_speed = [0.0, 0.0]
        self.last_pen_search = 0.0
        self.last_pad_search = 0.0
        self.access_logged = False

        self.pointer = None
        self.tablet = None
        self.buttons = None
        self.keyboard = None
        self.pointer_ok = False
        self.tablet_ok = False
        self.buttons_ok = False
        self.keyboard_ok = False

        self.cfg_mtime = 0.0
        self.next_maintenance = 0.0

        self.hypr = HyprWatcher()
        self.server = None
        self.started_at = time.time()

    # -- setup ---------------------------------------------------------

    def listen(self):
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setblocking(False)
        s.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        s.listen(8)
        self.server = s
        log(f"control socket -> {SOCKET_PATH}")

    def attach_pen(self):
        now = time.time()
        if self.pen is not None or now < self.last_pen_search:
            return
        self.last_pen_search = now + 2.0
        dev = find_pen(self.cfg.get("devices") or {})
        if dev is None:
            perms = permission_state()
            if perms["denied"] and not self.access_logged:
                log("tablet access denied: " + perms["hint"])
                self.access_logged = True
            return
        self.access_logged = False
        self.pen = dev
        self.ax = dev.absinfo(e.ABS_X)
        self.ay = dev.absinfo(e.ABS_Y)
        try:
            self.press = dev.absinfo(e.ABS_PRESSURE)
        except (OSError, ValueError):
            self.press = None
        self.pos = [self.ax.value, self.ay.value]
        # Warm the monitor-size cache now so the first tip-down click never
        # has to wait on hyprctl in the input path.
        self.screen_span()

        names = virtual_names(dev.name)
        if self.virtual_names is not None and names != self.virtual_names:
            # A different tablet (or a renamed one) appeared: rebuild the
            # uinput devices so their names follow the new source name.
            log("tablet changed; rebuilding virtual devices")
            self.close_virtual_devices()
        self.virtual_names = names

        if not self.build_virtual_devices(dev):
            log("virtual devices failed; releasing pen (native tablet)")
            self.close_pen()
            return

        log(f"pen ready: {dev.name} ({dev.path})")
        self.apply_mode(log_change=True)

    def attach_pad(self):
        now = time.time()
        if self.pads or now < self.last_pad_search:
            return
        # A pad only belongs to a tablet that also has a pen attached: never
        # grab a keyboard/mouse (or a stray pad-like interface) while in
        # standby waiting for a tablet.
        if self.pen is None:
            return
        self.last_pad_search = now + 2.0
        found = find_pad_devices(self.cfg.get("devices") or {}, self.pen)
        if not found:
            return

        self.pads = found
        self.pad = found[0][0]

        # Reset per-tablet control state, then accumulate from each node.
        self.pad_keys = []
        self.pad_key_index = {}
        self.strip_axes = {}
        self.ring_kind = None
        self.ring_btn_ids = []
        self.ring_btn_index = {}
        self.extra_keys = []
        self.extra_key_index = {}

        ring_min = ring_max = 0
        for dev, roles in found:
            keys, abs_codes = device_facts(dev)
            if "keys" in roles:
                self.pad_keys = [c for c in PAD_KEY_CODES if c in keys]
                self.pad_key_index = {
                    code: index for index, code in enumerate(self.pad_keys)
                }
            if "strip" in roles and not self.strip_axes:
                self.strip_axes = dict(STRIP_AXES)
            if "ring_abs" in roles and self.ring_kind is None:
                info = dev.absinfo(e.ABS_WHEEL)
                self.ring_kind = "abs"
                ring_min, ring_max = info.min, info.max
            if "ring_rel" in roles and self.ring_kind is None:
                self.ring_kind = "rel"
                ring_min, ring_max = 0, 0
            if "extra" in roles:
                self.extra_keys = pad_extra_buttons(keys)[:PAD_EXTRA_MAX]
                self.extra_key_index = {
                    code: index for index, code in enumerate(self.extra_keys)
                }
            # A dedicated dial (a ring node with no express keys) also exposes
            # the dial's own press button(s) as mappable "ring button" controls.
            if ("ring_abs" in roles or "ring_rel" in roles) and "keys" not in roles:
                presses = sorted(
                    c for c in keys
                    if e.BTN_MISC <= c <= e.BTN_DPAD_RIGHT
                    and c not in PEN_TOOL_CODES and c not in ALL_MOUSE_BUTTONS
                )
                for i, code in enumerate(presses, start=len(self.ring_btn_ids)):
                    self.ring_btn_ids.append(f"ring-btn:{i}")
                    self.ring_btn_index[code] = i

        self.ring_min = ring_min
        self.ring_max = ring_max
        self.ring_last = None
        self.ring_acc = 0.0
        self.ring_notch = 0.0
        self.ring_time = None
        self.ring_speed = 0.0

        roles_desc = [
            f"{dev.name.split()[0] if dev.name else '?'}:{'/'.join(roles)}"
            for dev, roles in found
        ]
        log(
            f"pad ready: {self.pad.name} ({self.pad.path}) "
            f"keys={len(self.pad_keys)} strips={len(self.strip_axes)} "
            f"ring={'yes' if self.ring_kind else 'no'} "
            f"ringbtns={len(self.ring_btn_ids)} extra={len(self.extra_keys)} "
            f"[{', '.join(roles_desc)}]"
        )
        self.apply_mode()

    def build_virtual_devices(self, src):
        names = self.virtual_names or virtual_names(src.name)
        if self.pointer is None:
            try:
                self.pointer = build_pointer(names["pointer"])
            except Exception as exc:  # noqa: BLE001
                log(f"could not create pointer: {exc}")
                return False
            pointer_path = uinput_device_path(self.pointer)
            if pointer_path is None:
                log("ERROR: virtual pointer device node not found")
                return False
            self.pointer_ok = wait_for_props(pointer_path, ("ID_INPUT=1",))
            if not self.pointer_ok:
                log("ERROR: virtual pointer not registered; not grabbing pen")
                return False
            log(f"pointer -> {pointer_path}")

        if self.tablet is None:
            try:
                self.tablet = build_tablet(src, names["tablet"])
                self.buttons = build_buttons(names["buttons"])
            except Exception as exc:  # noqa: BLE001
                log(f"could not create tablet: {exc}")
                self.close_tablet()
                return False
            tablet_path = uinput_device_path(self.tablet)
            buttons_path = uinput_device_path(self.buttons)
            if tablet_path is None or buttons_path is None:
                log("ERROR: virtual tablet/buttons device node not found")
                self.close_tablet()
                return False
            self.tablet_ok = wait_for_props(
                tablet_path, ("ID_INPUT_TABLET=1",)
            )
            self.buttons_ok = wait_for_props(buttons_path, ("ID_INPUT=1",))
            if not (self.tablet_ok and self.buttons_ok):
                log("ERROR: virtual tablet/buttons not registered")
                self.close_tablet()
                return False
            log(
                f"tablet -> {tablet_path}, "
                f"buttons -> {buttons_path}"
            )
        return True

    def ensure_keyboard(self):
        if self.keyboard is not None:
            return self.keyboard_ok
        names = self.virtual_names or virtual_names("")
        try:
            self.keyboard = build_keyboard(names["keys"])
        except Exception as exc:  # noqa: BLE001
            log(f"could not create keyboard: {exc}")
            self.keyboard = None
            return False
        keyboard_path = uinput_device_path(self.keyboard)
        if keyboard_path is None:
            log("ERROR: virtual keyboard device node not found")
            self.close_keyboard()
            return False
        self.keyboard_ok = wait_for_props(keyboard_path, ("ID_INPUT=1",))
        if not self.keyboard_ok:
            log("ERROR: virtual keyboard not registered")
            self.close_keyboard()
            return False
        log(f"keyboard -> {keyboard_path}")
        return True

    def close_pointer(self):
        if self.pointer is not None:
            try:
                self.pointer.close()
            except OSError:
                pass
        self.pointer = None
        self.pointer_ok = False

    def close_virtual_devices(self):
        self.close_tablet()
        self.close_keyboard()
        self.close_pointer()

    def close_tablet(self):
        for dev in (self.tablet, self.buttons):
            if dev is not None:
                try:
                    dev.close()
                except OSError:
                    pass
        self.tablet = None
        self.buttons = None
        self.tablet_ok = False
        self.buttons_ok = False

    def close_keyboard(self):
        if self.keyboard is not None:
            try:
                self.keyboard.close()
            except OSError:
                pass
        self.keyboard = None
        self.keyboard_ok = False

    def close_pen(self):
        if self.pen is None:
            return
        try:
            if self.grabbed:
                self.pen.ungrab()
        except OSError:
            pass
        self.grabbed = False
        try:
            self.pen.close()
        except OSError:
            pass
        self.pen = None
        self.press = None
        self.tip_down = False
        self.click_anchor = None
        self.click_unlocked = False

    def close_pad(self):
        if not self.pads:
            return
        if self.pads_grabbed:
            for dev, _roles in self.pads:
                try:
                    dev.ungrab()
                except OSError:
                    pass
        self.pads_grabbed = False
        for dev, _roles in self.pads:
            try:
                dev.close()
            except OSError:
                pass
        self.pads = []
        self.pad = None
        self.pad_keys = []
        self.pad_key_index = {}
        self.strip_axes = {}
        self.ring_kind = None
        self.ring_last = None
        self.ring_acc = 0.0
        self.ring_notch = 0.0
        self.ring_time = None
        self.ring_speed = 0.0
        self.ring_btn_ids = []
        self.ring_btn_index = {}
        self.extra_keys = []
        self.extra_key_index = {}

    # -- mode ----------------------------------------------------------

    def desired_mode(self):
        if not self.cfg.get("enabled", True):
            return "native"
        base = self.cfg.get("mode", "mouse")
        if self.cfg.get("auto", True):
            cls = self.active.lower()
            if cls:
                for pattern, mode in self.cfg.get("rules", {}).items():
                    pat = pattern.lower()
                    if pat == cls or fnmatch.fnmatchcase(cls, pat):
                        return mode
        return base

    def apply_mode(self, log_change=False):
        want = self.desired_mode()
        wanted_grab = want != "native"

        if (
            want == self.effective
            and self.grabbed == (wanted_grab and self.pen is not None)
            and self.pads_grabbed == (wanted_grab and bool(self.pads))
        ):
            return

        if want == "native":
            self.release_all()
            self.ungrab()
            self.effective = "native"
            if log_change:
                log("mode: native (pen released)")
            return

        self.release_all()
        self.effective = want
        if not self.grab():
            self.effective = "native"
            return
        self.sync_position()
        self.replay_held()
        if log_change:
            log(f"mode: {want}")

    def grab(self):
        if self.pen is not None and not self.grabbed:
            if self.pointer_ok and self.tablet_ok:
                try:
                    self.pen.grab()
                    self.grabbed = True
                except OSError as exc:
                    log(f"pen grab failed: {exc}")
        if self.pads and not self.pads_grabbed:
            try:
                for dev, _roles in self.pads:
                    dev.grab()
                self.pads_grabbed = True
            except OSError as exc:
                log(f"pad grab failed: {exc}")
        return self.grabbed

    def ungrab(self):
        if self.pen is not None and self.grabbed:
            try:
                self.pen.ungrab()
            except OSError:
                pass
            self.grabbed = False
        if self.pads and self.pads_grabbed:
            for dev, _roles in self.pads:
                try:
                    dev.ungrab()
                except OSError:
                    pass
            self.pads_grabbed = False

    # -- active area ----------------------------------------------------

    def area_rect(self):
        area = self.cfg.get("area") or {}
        span_x = max(1, self.ax.max - self.ax.min)
        span_y = max(1, self.ay.max - self.ay.min)
        x0 = self.ax.min + area.get("x", 0.0) * span_x
        y0 = self.ay.min + area.get("y", 0.0) * span_y
        return x0, y0, max(1.0, area.get("w", 1.0) * span_x), max(1.0, area.get("h", 1.0) * span_y)

    def to_screen(self):
        x0, y0, w, h = self.area_rect()
        sx = round((self.pos[0] - x0) / w * ABS_MAX)
        sy = round((self.pos[1] - y0) / h * ABS_MAX)
        return clamp(sx, 0, ABS_MAX), clamp(sy, 0, ABS_MAX)

    def to_tablet(self, code, value):
        x0, y0, w, h = self.area_rect()
        if code == e.ABS_X:
            return clamp(round((value - x0) / w * (self.ax.max - self.ax.min) + self.ax.min),
                         self.ax.min, self.ax.max)
        return clamp(round((value - y0) / h * (self.ay.max - self.ay.min) + self.ay.min),
                     self.ay.min, self.ay.max)

    def scale_pressure(self, value):
        """Apply the configured pressure gain, keeping the tablet's own range."""
        gain = self.cfg.get("pressureSensitivity", 1.0)
        info = self.press
        if info is None or info.max <= info.min or gain == 1.0:
            return value
        scaled = info.min + (value - info.min) * gain
        return clamp(round(scaled), info.min, info.max)

    def screen_span(self):
        """Cached logical width/height of the monitor layout."""
        now = time.monotonic()
        if now - self.screen_span_at < SCREEN_SPAN_TTL:
            return self.screen_span_px
        self.screen_span_at = now
        span = hyprctl_screen_span()
        if span is not None:
            self.screen_span_px = span
        return self.screen_span_px

    def click_deadzone(self, sx, sy):
        """True while a tip-down movement is still just hand shake."""
        if not self.tip_down or self.click_unlocked:
            return False
        if self.click_anchor is None:
            self.click_anchor = self.screen_pos or (sx, sy)
        ax, ay = self.click_anchor
        width, height = self.screen_span()
        dx = (sx - ax) * width / ABS_MAX
        dy = (sy - ay) * height / ABS_MAX
        if dx * dx + dy * dy <= CLICK_SHAKE_PX * CLICK_SHAKE_PX:
            return True
        # Once the pen has really moved, let it draw/drag normally for the
        # rest of this contact; do not re-latch if it wanders back.
        self.click_unlocked = True
        return False

    def sync_position(self):
        if self.effective == "mouse" and self.pointer_ok:
            sx, sy = self.to_screen()
            self.pointer.write(e.EV_ABS, e.ABS_X, sx)
            self.pointer.write(e.EV_ABS, e.ABS_Y, sy)
            self.pointer.write(e.EV_SYN, e.SYN_REPORT, 0)
            self.screen_pos = (sx, sy)
        elif self.effective == "tablet" and self.tablet_ok:
            self.tablet.write(e.EV_ABS, e.ABS_X, self.to_tablet(e.ABS_X, self.pos[0]))
            self.tablet.write(e.EV_ABS, e.ABS_Y, self.to_tablet(e.ABS_Y, self.pos[1]))
            self.tablet.write(e.EV_SYN, e.SYN_REPORT, 0)

    # -- actions -------------------------------------------------------

    def release_tip_button(self):
        """Release whichever virtual button the pen tip is holding."""
        dev = self.tip_device
        code = self.tip_button
        self.tip_button = None
        self.tip_device = None
        if dev is None or code is None:
            return
        try:
            dev.write(e.EV_KEY, code, 0)
            dev.write(e.EV_SYN, e.SYN_REPORT, 0)
        except OSError:
            pass

    def set_tip_button(self, dev, code, value):
        """Drive the tip button as a paired press/release.

        A dropped press or a dropped release is repaired here: a new press
        closes any stale contact first, and a release without a press still
        emits a complete click. That way a physical tap always produces the
        same press/release pair the compositor needs to count a click.
        """
        if value:
            self.release_tip_button()
            try:
                dev.write(e.EV_KEY, code, 1)
                dev.write(e.EV_SYN, e.SYN_REPORT, 0)
            except OSError:
                return
            self.tip_button = code
            self.tip_device = dev
            return

        if self.tip_button == code and self.tip_device is dev:
            self.release_tip_button()
            return

        # Release without a matching press (or after a restart): synthesize a
        # full click so the tap is not lost.
        self.release_tip_button()
        try:
            dev.write(e.EV_KEY, code, 1)
            dev.write(e.EV_SYN, e.SYN_REPORT, 0)
            dev.write(e.EV_KEY, code, 0)
            dev.write(e.EV_SYN, e.SYN_REPORT, 0)
        except OSError:
            pass

    def run_action(self, action, value, source, is_tip=False):
        if not valid_action(action):
            return
        if action == "none":
            return
        if action.startswith("mouse:"):
            button = MOUSE_ACTIONS[action[6:]]
            if (
                source == "pen"
                and is_tip
                and self.effective == "tablet"
                and action == "mouse:left"
            ):
                # The tip must stay a real tablet contact or pressure and
                # drawing break. "mouse:left" on the tip means "normal tip".
                if value or self.tip_button is None:
                    self.sync_position()
                self.set_tip_button(self.tablet, e.BTN_TOUCH, value)
                return "tablet"
            target = (
                self.pointer
                if source == "pen" and self.effective == "mouse"
                else self.buttons
            )
            if target is None:
                return None
            if source == "pen" and is_tip:
                if value or self.tip_button is None:
                    # Park the pointer exactly on the pen before the click.
                    # Motion may have been held by the shake filter or the
                    # proximity guard, and clicking at an old position feels
                    # like the click was not registered.
                    self.sync_position()
                self.set_tip_button(target, button, value)
            else:
                target.write(e.EV_KEY, button, value)
                target.write(e.EV_SYN, e.SYN_REPORT, 0)
            return None
        if action.startswith("key:") and value:
            self.send_combo(action[4:])
        return None

    def send_combo(self, combo):
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        if not parts:
            return
        codes = []
        for part in parts:
            code = resolve_key(part)
            if code is None:
                log(f"unknown key in shortcut: {part!r} ({combo!r})")
                return
            codes.append(code)
        if not self.ensure_keyboard():
            return
        *mods, key = codes
        for code in mods:
            self.keyboard.write(e.EV_KEY, code, 1)
        self.keyboard.write(e.EV_KEY, key, 1)
        self.keyboard.write(e.EV_KEY, key, 0)
        for code in reversed(mods):
            self.keyboard.write(e.EV_KEY, code, 0)
        self.keyboard.write(e.EV_SYN, e.SYN_REPORT, 0)

    def mapped_action(self, button_id):
        return (self.cfg.get("buttons") or {}).get(button_id, "none")

    def release_all(self):
        self.held.clear()
        self.tip_down = False
        self.click_anchor = None
        self.click_unlocked = False
        # release_all() itself lets the tip button go below; forget the pairing
        # and swallow the matching physical release so it cannot be mistaken
        # for a tap that lost its press.
        self.tip_ignore_release = self.tip_button is not None
        self.tip_button = None
        self.tip_device = None
        targets = (
            (self.pointer, ALL_MOUSE_BUTTONS),
            (self.tablet, (e.BTN_TOUCH, e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER)),
            (self.buttons, ALL_MOUSE_BUTTONS),
        )
        for dev, codes in targets:
            if dev is None:
                continue
            try:
                for code in codes:
                    dev.write(e.EV_KEY, code, 0)
                dev.write(e.EV_SYN, e.SYN_REPORT, 0)
            except OSError:
                pass

    def replay_held(self):
        for code, value in list(self.held.items()):
            if not value:
                continue
            if code == e.BTN_TOUCH:
                self.run_action(self.mapped_action("tip"), 1, "pen", is_tip=True)
            elif code == e.BTN_STYLUS:
                self.run_action(self.mapped_action("stylus"), 1, "pen")
            elif code == e.BTN_STYLUS2:
                self.run_action(self.mapped_action("stylus2"), 1, "pen")

    # -- pen events -----------------------------------------------------

    def commit_pointer(self):
        """Flush queued pointer writes and remember where the cursor landed."""
        self.pointer.write(e.EV_SYN, e.SYN_REPORT, 0)
        if self.frame_pos is not None:
            if self.proximity is not False:
                self.screen_pos = self.frame_pos
            self.frame_pos = None

    def on_pen_events(self):
        try:
            events = list(self.pen.read())
        except OSError:
            log("pen disconnected; standby (waiting for a tablet)")
            self.close_pen()
            self.effective = "native"
            return

        dirty_tablet = False
        dirty_pointer = False

        for ev in events:
            if ev.type == e.EV_SYN:
                if ev.code == e.SYN_REPORT:
                    if self.effective == "mouse" and dirty_pointer:
                        self.commit_pointer()
                        dirty_pointer = False
                    elif self.effective == "tablet" and dirty_tablet:
                        self.tablet.write(e.EV_SYN, e.SYN_REPORT, 0)
                        dirty_tablet = False
                continue

            if ev.type == e.EV_ABS:
                if ev.code == e.ABS_X:
                    self.pos[0] = ev.value
                elif ev.code == e.ABS_Y:
                    self.pos[1] = ev.value

                if self.effective == "mouse" and ev.code in (e.ABS_X, e.ABS_Y):
                    # Out of range a tablet may still report (zeroed)
                    # coordinates for a moment. Ignoring those keeps the
                    # cursor parked; real motion after that means the pen is
                    # back, so pick the pen up again instead of getting stuck.
                    if self.proximity is False and (time.monotonic() - self.proximity_out_at) < 0.25:
                        continue
                    self.proximity = True
                    sx, sy = self.to_screen()
                    if self.click_deadzone(sx, sy):
                        continue
                    self.pointer.write(e.EV_ABS, e.ABS_X, sx)
                    self.pointer.write(e.EV_ABS, e.ABS_Y, sy)
                    self.frame_pos = (sx, sy)
                    dirty_pointer = True
                elif self.effective == "tablet":
                    if ev.code in (e.ABS_X, e.ABS_Y):
                        if self.click_deadzone(*self.to_screen()):
                            continue
                        self.tablet.write(e.EV_ABS, ev.code, self.to_tablet(ev.code, ev.value))
                    elif ev.code == e.ABS_PRESSURE:
                        self.tablet.write(e.EV_ABS, ev.code, self.scale_pressure(ev.value))
                    else:
                        self.tablet.write(e.EV_ABS, ev.code, ev.value)
                    dirty_tablet = True
                continue

            if ev.type == e.EV_KEY:
                if ev.value:
                    self.held[ev.code] = ev.value
                else:
                    self.held.pop(ev.code, None)

                if ev.code == e.BTN_TOUCH:
                    if ev.value:
                        self.tip_ignore_release = False
                        if not self.tip_down:
                            self.tip_down = True
                            self.click_anchor = self.to_screen()
                            self.click_unlocked = False
                    else:
                        self.tip_down = False
                        self.click_anchor = None
                        self.click_unlocked = False
                        if self.tip_ignore_release:
                            # release_all() already released this contact.
                            self.tip_ignore_release = False
                            continue
                    result = self.run_action(
                        self.mapped_action("tip"), ev.value, "pen", is_tip=True
                    )
                    if result == "tablet" and self.effective == "tablet":
                        dirty_tablet = True
                elif ev.code == e.BTN_STYLUS:
                    self.run_action(self.mapped_action("stylus"), ev.value, "pen")
                elif ev.code == e.BTN_STYLUS2:
                    self.run_action(self.mapped_action("stylus2"), ev.value, "pen")
                elif ev.code in (e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER):
                    if ev.value:
                        self.proximity = True
                    elif self.proximity is not False:
                        self.proximity = False
                        self.proximity_out_at = time.monotonic()
                        self.tip_down = False
                        self.click_anchor = None
                        self.click_unlocked = False
                        # Lifting the pen away must not make the cursor jump
                        # or disappear: put it back where the pen left off
                        # (a pointer event also makes Hyprland show it again).
                        if (
                            self.effective == "mouse"
                            and self.pointer is not None
                            and self.screen_pos is not None
                        ):
                            self.pointer.write(e.EV_ABS, e.ABS_X, self.screen_pos[0])
                            self.pointer.write(e.EV_ABS, e.ABS_Y, self.screen_pos[1])
                            dirty_pointer = True
                    if self.effective == "tablet":
                        self.tablet.write(e.EV_KEY, ev.code, ev.value)
                        dirty_tablet = True
                continue

        if self.effective == "mouse" and dirty_pointer:
            self.commit_pointer()
        if self.effective == "tablet" and dirty_tablet:
            self.tablet.write(e.EV_SYN, e.SYN_REPORT, 0)

    # -- pad events -----------------------------------------------------

    def roles_for(self, dev):
        for d, roles in self.pads:
            if d is dev:
                return roles
        return []

    def on_pad_events(self, dev):
        roles = self.roles_for(dev)
        try:
            events = list(dev.read())
        except OSError:
            log(f"pad node disappeared: {dev.name}")
            self.close_pad()
            return
        # Strips/dial deliver position and buttons in one frame, so the frame
        # is only interpreted once SYN_REPORT closes it.
        frame = []
        for ev in events:
            if ev.type != e.EV_SYN:
                frame.append(ev)
                continue
            if ev.code == e.SYN_REPORT and frame:
                self.handle_pad_frame(dev, roles, frame)
                frame = []

    def handle_pad_frame(self, dev, roles, frame):
        if "ring_rel" in roles:
            self.handle_ring_rel(frame)
        else:
            touch = None
            positions = []
            ring_value = None
            for ev in frame:
                if ev.type != e.EV_ABS:
                    continue
                if ev.code == e.ABS_MISC:
                    touch = ev.value
                elif ev.code in self.strip_axes:
                    positions.append((self.strip_axes[ev.code], ev.value))
                elif ev.code == e.ABS_WHEEL:
                    ring_value = ev.value

            if ring_value is not None and self.ring_kind == "abs":
                self.scroll_ring(ring_value)

            if "strip" in roles:
                if touch == 0:
                    # Finger lifted; the driver also snaps the position back to 0.
                    self.strip_last = [None, None]
                    self.strip_acc = [0.0, 0.0]
                    self.strip_notch = [0.0, 0.0]
                    self.strip_time = [None, None]
                    self.strip_speed = [0.0, 0.0]
                elif positions:
                    self.scroll_strip(positions, landing=touch == 15)

        for ev in frame:
            if ev.type != e.EV_KEY:
                continue
            value = ev.value
            index = self.pad_key_index.get(ev.code)
            if index is not None:
                self.run_action(self.mapped_action(f"pad:{index}"), value, "pad")
                continue
            rbtn = self.ring_btn_index.get(ev.code)
            if rbtn is not None:
                self.run_action(self.mapped_action(self.ring_btn_ids[rbtn]), value, "pad")
                continue
            eidx = self.extra_key_index.get(ev.code)
            if eidx is not None:
                self.run_action(self.mapped_action(extra_button_id(self.extra_keys[eidx])), value, "pad")

    def handle_ring_rel(self, frame):
        """A physical dial turns -> REL_WHEEL / REL_WHEEL_HI_RES. Re-emit as
        the mapped ring scroll."""
        if not self.pads_grabbed or self.buttons is None:
            return
        action = self.mapped_action("ring:0")
        scroll = SCROLL_ACTIONS.get(action)
        if scroll is None:
            return
        hi_axis, axis, sign = scroll
        hi = 0
        fallback = 0
        for ev in frame:
            if ev.type != e.EV_REL:
                continue
            if ev.code == e.REL_WHEEL_HI_RES:
                hi += ev.value
            elif ev.code == e.REL_WHEEL:
                fallback += ev.value
        if hi == 0 and fallback != 0:
            hi = fallback * HI_RES_PER_NOTCH
        if not hi:
            return
        gain = self.cfg.get("stripSensitivity", 1.0)
        hi = int(round(hi * gain * sign))
        if not hi:
            return
        self.buttons.write(e.EV_REL, hi_axis, hi)
        notches = int(hi / HI_RES_PER_NOTCH)
        if notches:
            self.buttons.write(e.EV_REL, axis, notches)
        self.buttons.write(e.EV_SYN, e.SYN_REPORT, 0)

    def scroll_ring(self, value):
        if not self.pads_grabbed or self.buttons is None or self.ring_kind != "abs":
            return
        now = time.monotonic()
        previous = self.ring_last
        if previous is None:
            self.ring_last = value
            self.ring_time = now
            self.ring_speed = 0.0
            return
        span = self.ring_max - self.ring_min + 1
        delta = float(value - previous)
        if span > 1:
            # The ring is an absolute 360°-ish axis: wrap the shortest way.
            if delta > span / 2:
                delta -= span
            elif delta < -span / 2:
                delta += span
        self.ring_last = value
        dt = now - self.ring_time if self.ring_time is not None else 0.0
        self.ring_time = now
        if delta == 0:
            return
        action = self.mapped_action("ring:0")
        scroll = SCROLL_ACTIONS.get(action)
        if scroll is None:
            self.ring_acc = 0.0
            self.ring_notch = 0.0
            self.ring_speed = 0.0
            return
        hi_axis, axis, sign = scroll
        gain = self.cfg.get("stripSensitivity", 1.0)
        if dt > 0.002:
            instant = abs(delta) / dt
            self.ring_speed = 0.55 * self.ring_speed + 0.45 * instant
        speed = self.ring_speed
        ramp = (speed - STRIP_FAST_FROM) / (STRIP_FAST_TO - STRIP_FAST_FROM)
        factor = 1.0 + (STRIP_ACCEL_MAX - 1.0) * clamp(ramp, 0.0, 1.0)
        scale = RING_NOTCHES_PER_TURN * HI_RES_PER_NOTCH / max(1.0, float(span))
        self.ring_acc += delta * scale * sign * gain * factor
        hi_ticks = int(self.ring_acc)
        if hi_ticks:
            self.ring_acc -= hi_ticks
            self.buttons.write(e.EV_REL, hi_axis, hi_ticks)
            self.ring_notch += hi_ticks
            notches = int(self.ring_notch / HI_RES_PER_NOTCH)
            if notches:
                self.ring_notch -= notches * HI_RES_PER_NOTCH
                self.buttons.write(e.EV_REL, axis, notches)
            self.buttons.write(e.EV_SYN, e.SYN_REPORT, 0)

    def scroll_strip(self, positions, landing=False):
        if not self.pads_grabbed or self.buttons is None:
            return
        emitted = False
        now = time.monotonic()
        for index, value in positions:
            previous = self.strip_last[index]
            last = self.strip_time[index]
            stale = last is not None and (now - last) > STRIP_STALE_GAP
            if landing or stale or previous is None:
                # Anchor only: the finger landed somewhere on the strip and
                # that jump must not scroll anything.
                self.strip_last[index] = value
                self.strip_acc[index] = 0.0
                self.strip_notch[index] = 0.0
                self.strip_time[index] = now
                self.strip_speed[index] = 0.0
                if DEBUG:
                    debug(f"strip{index} anchor at {value} (landing={landing} stale={stale})")
                continue
            delta = value - previous
            self.strip_last[index] = value
            dt = now - last if last is not None else 0.0
            self.strip_time[index] = now
            if delta == 0:
                continue
            action = self.mapped_action(f"strip:{index}")
            scroll = SCROLL_ACTIONS.get(action)
            if scroll is None:
                self.strip_acc[index] = 0.0
                self.strip_notch[index] = 0.0
                self.strip_speed[index] = 0.0
                continue
            hi_axis, axis, sign = scroll
            gain = self.cfg.get("stripSensitivity", 1.0)

            # Finger speed, smoothed so one jittery frame cannot spike the
            # gain. Short gaps are clamped so a pause between frames does not
            # read as a huge flick.
            if dt > 0.002:
                instant = abs(delta) / dt
                self.strip_speed[index] = 0.55 * self.strip_speed[index] + 0.45 * instant
            speed = self.strip_speed[index]
            ramp = (speed - STRIP_FAST_FROM) / (STRIP_FAST_TO - STRIP_FAST_FROM)
            factor = 1.0 + (STRIP_ACCEL_MAX - 1.0) * clamp(ramp, 0.0, 1.0)

            self.strip_acc[index] += delta * sign * HI_RES_PER_UNIT * gain * factor
            hi_ticks = int(self.strip_acc[index])
            if hi_ticks:
                self.strip_acc[index] -= hi_ticks
                self.buttons.write(e.EV_REL, hi_axis, hi_ticks)
                emitted = True
                # Keep the classic wheel in step for clients that ignore the
                # high-resolution axis; one notch per 120 hi-res units.
                self.strip_notch[index] += hi_ticks
                notches = int(self.strip_notch[index] / HI_RES_PER_NOTCH)
                if notches:
                    self.strip_notch[index] -= notches * HI_RES_PER_NOTCH
                    self.buttons.write(e.EV_REL, axis, notches)
        if emitted:
            self.buttons.write(e.EV_SYN, e.SYN_REPORT, 0)


    # -- config / status ------------------------------------------------

    def reload_config(self):
        before = json.dumps(self.cfg, sort_keys=True)
        self.cfg = load_config()
        try:
            self.cfg_mtime = os.stat(CONFIG_PATH).st_mtime
        except OSError:
            self.cfg_mtime = 0.0
        if json.dumps(self.cfg, sort_keys=True) != before:
            log("config reloaded")
            self.apply_mode(log_change=True)

    def status(self):
        perms = permission_state()
        return {
            "ok": True,
            "enabled": bool(self.cfg.get("enabled", True)),
            "mode": self.cfg.get("mode", "mouse"),
            "auto": bool(self.cfg.get("auto", True)),
            "rules": dict(self.cfg.get("rules", {})),
            "buttons": dict(self.cfg.get("buttons", {})),
            "stripSensitivity": self.cfg.get("stripSensitivity", 1.0),
            "pressureSensitivity": self.cfg.get("pressureSensitivity", 1.0),
            "area": dict(self.cfg.get("area", {})),
            "activeClass": self.active,
            "effectiveMode": self.effective,
            "grabbed": self.grabbed,
            "standby": bool(self.cfg.get("enabled", True)) and self.pen is None,
            "penPresent": self.pen is not None,
            "padPresent": self.pad is not None,
            "penName": self.pen.name if self.pen is not None else "",
            "padName": self.pad.name if self.pad is not None else "",
            "padButtons": len(self.pad_keys),
            "strips": len(self.strip_axes),
            "ring": 1 if self.ring_kind else 0,
            "ringButtons": list(self.ring_btn_ids),
            "extraButtons": [extra_button_id(c) for c in self.extra_keys],
            "devices": {
                "pointer": self.pointer_ok,
                "tablet": self.tablet_ok,
                "buttons": self.buttons_ok,
                "keyboard": self.keyboard_ok,
            },
            "inputAccess": perms["input"],
            "uinputAccess": perms["uinput"],
            "permissionDenied": perms["denied"],
            "permissionHint": perms["hint"],
            "inaccessibleTablets": perms["tablets"],
            "pid": os.getpid(),
            "uptime": round(time.time() - self.started_at, 1),
        }

    def handle_command(self, req):
        if not isinstance(req, dict):
            return {"ok": False, "error": "request must be an object"}
        cmd = req.get("cmd", "")
        if cmd == "status":
            return self.status()
        if cmd == "ping":
            return {"ok": True, "pong": True}

        if cmd in ("set-mode", "enable", "disable"):
            if cmd == "disable":
                self.cfg["enabled"] = False
            else:
                mode = req.get("mode", self.cfg.get("mode", "mouse"))
                if mode not in ("mouse", "tablet"):
                    return {"ok": False, "error": f"bad mode: {mode}"}
                self.cfg["enabled"] = True
                self.cfg["mode"] = mode
            save_config(self.cfg)
            self.apply_mode(log_change=True)

        elif cmd == "set-auto":
            self.cfg["auto"] = bool(req.get("enabled", True))
            save_config(self.cfg)
            self.apply_mode(log_change=True)

        elif cmd == "set-rule":
            pattern = str(req.get("class", "")).strip()
            mode = req.get("mode")
            if not pattern:
                return {"ok": False, "error": "empty class"}
            if mode not in ("mouse", "tablet"):
                return {"ok": False, "error": f"bad mode: {mode}"}
            self.cfg.setdefault("rules", {})[pattern] = mode
            save_config(self.cfg)
            self.apply_mode(log_change=True)

        elif cmd == "remove-rule":
            pattern = str(req.get("class", "")).strip()
            self.cfg.setdefault("rules", {}).pop(pattern, None)
            save_config(self.cfg)
            self.apply_mode(log_change=True)

        elif cmd == "set-button":
            name = str(req.get("button", "")).strip()
            action = req.get("action", "")
            known = (
                name in PEN_BUTTONS or name in PAD_BUTTONS
                or name in STRIP_BUTTONS or name in RING_BUTTONS
                or name.startswith("ring-btn:") or name.startswith("extra:")
            )
            if not known:
                return {"ok": False, "error": f"unknown button: {name}"}
            if not valid_action(action):
                return {"ok": False, "error": f"bad action: {action}"}
            if (name in STRIP_BUTTONS or name in RING_BUTTONS) and action.startswith(("mouse:", "key:")):
                return {"ok": False, "error": f"{name} only takes scroll actions"}
            self.cfg.setdefault("buttons", {})[name] = action
            save_config(self.cfg)

        elif cmd == "set-strip-sensitivity":
            self.cfg["stripSensitivity"] = clean_gain(
                req.get("value", 1.0), self.cfg.get("stripSensitivity", 1.0)
            )
            save_config(self.cfg)

        elif cmd == "set-pressure-sensitivity":
            self.cfg["pressureSensitivity"] = clean_gain(
                req.get("value", 1.0), self.cfg.get("pressureSensitivity", 1.0)
            )
            save_config(self.cfg)

        elif cmd == "set-area":
            area = clean_area(req)
            self.cfg["area"] = area
            save_config(self.cfg)

        elif cmd == "reset-area":
            self.cfg["area"] = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
            save_config(self.cfg)

        elif cmd == "test-button":
            name = str(req.get("button", "")).strip()
            known = (
                name in PEN_BUTTONS or name in PAD_BUTTONS
                or name in STRIP_BUTTONS or name in RING_BUTTONS
                or name.startswith("ring-btn:") or name.startswith("extra:")
            )
            if not known:
                return {"ok": False, "error": f"unknown button: {name}"}
            action = self.mapped_action(name)
            if name in STRIP_BUTTONS or name in RING_BUTTONS:
                scroll = SCROLL_ACTIONS.get(action)
                if scroll is not None and self.buttons is not None:
                    hi_axis, axis, sign = scroll
                    # Two notches as feedback, in both wheel encodings.
                    self.buttons.write(e.EV_REL, hi_axis, 240 * sign)
                    self.buttons.write(e.EV_REL, axis, 2 * sign)
                    self.buttons.write(e.EV_SYN, e.SYN_REPORT, 0)
                return {"ok": True, "button": name, "action": action}
            is_tip = name == "tip"
            source = "pen" if name in PEN_BUTTONS else "pad"
            self.run_action(action, 1, source, is_tip=is_tip)
            self.run_action(action, 0, source, is_tip=is_tip)
            return {"ok": True, "button": name, "action": action}

        elif cmd == "reload":
            self.reload_config()

        else:
            return {"ok": False, "error": f"unknown command: {cmd}"}

        try:
            self.cfg_mtime = os.stat(CONFIG_PATH).st_mtime
        except OSError:
            pass
        out = self.status()
        out["ok"] = True
        return out

    # -- socket ---------------------------------------------------------

    def accept(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        # A fresh connection may not have data queued yet; read with a timeout
        # instead of one non-blocking recv, which raced with the client.
        conn.settimeout(2.0)
        data = b""
        try:
            while b"\n" not in data and len(data) < (1 << 20):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        response = {"ok": False, "error": "empty request"}
        if data:
            line = data.split(b"\n", 1)[0]
            try:
                req = json.loads(line.decode("utf-8", "replace"))
                response = self.handle_command(req)
            except ValueError as exc:
                response = {"ok": False, "error": f"bad json: {exc}"}
            except Exception as exc:  # noqa: BLE001
                log(f"command failed: {exc}")
                response = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(response) + "\n").encode())
        except OSError:
            pass
        close_quietly(conn)

    # -- main loop -------------------------------------------------------

    def periodic(self):
        now = time.time()
        if now < self.next_maintenance:
            return
        self.next_maintenance = now + 0.5

        self.hypr.ensure_connected()
        self.attach_pen()
        self.attach_pad()

        if self.hypr.sock is None:
            self.hypr.poll()
        elif self.hypr.want_poll:
            self.hypr.want_poll = False
            self.hypr.poll(force=True)

        if self.hypr.active != self.active:
            self.active = self.hypr.active
            self.apply_mode(log_change=True)

        try:
            mtime = os.stat(CONFIG_PATH).st_mtime
        except OSError:
            mtime = 0.0
        if mtime and mtime != self.cfg_mtime:
            self.cfg_mtime = mtime
            self.reload_config()

    def run(self):
        self.listen()
        try:
            self.cfg_mtime = os.stat(CONFIG_PATH).st_mtime
        except OSError:
            self.cfg_mtime = 0.0
        self.hypr.poll(force=True)
        self.active = self.hypr.active
        self.attach_pen()
        self.attach_pad()

        while not STOP:
            fds = [self.server]
            pad_by_fd = {}
            if self.pen is not None:
                fds.append(self.pen.fd)
            for dev, _roles in self.pads:
                try:
                    fd = dev.fd
                except OSError:
                    continue
                fds.append(fd)
                pad_by_fd[fd] = dev
            hypr_fd = self.hypr.fileno()
            if hypr_fd >= 0:
                fds.append(hypr_fd)
            try:
                readable, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                if self.pen is not None:
                    self.close_pen()
                if self.pads:
                    self.close_pad()
                self.hypr.sock = None
                continue

            for fd in readable:
                if fd is self.server:
                    self.accept()
                elif self.pen is not None and fd == self.pen.fd:
                    self.on_pen_events()
                elif fd in pad_by_fd:
                    self.on_pad_events(pad_by_fd[fd])
                elif hypr_fd >= 0 and fd == hypr_fd:
                    changed = self.hypr.pump()
                    if changed or self.hypr.want_poll:
                        self.hypr.want_poll = False
                        self.active = self.hypr.active
                        self.apply_mode(log_change=True)

            self.periodic()

        self.shutdown()

    def shutdown(self):
        log("shutting down")
        self.release_all()
        self.ungrab()
        self.close_tablet()
        self.close_keyboard()
        if self.pointer is not None:
            try:
                self.pointer.close()
            except OSError:
                pass
        self.pointer = None
        self.close_pen()
        self.close_pad()
        close_quietly(self.server)
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


def main():
    def _stop(_sig, _frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    daemon = PenDaemon()
    daemon.run()


if __name__ == "__main__":
    main()

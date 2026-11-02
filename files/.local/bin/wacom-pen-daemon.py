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

Button actions (pen tip, both barrel buttons, all pad express keys)
    none
    mouse:left | mouse:right | mouse:middle | mouse:back | mouse:forward
    key:<combo>            e.g. key:ctrl+z, key:alt+tab, key:f5

Touch strip actions (the two strips beside the express keys)
    none
    scroll:vertical        slide up/down -> wheel
    scroll:vertical-inv    same, direction reversed
    scroll:horizontal      slide up/down -> horizontal wheel
    scroll:horizontal-inv  same, direction reversed

Active area
    "area": {"x":0,"y":0,"w":1,"h":1} selects the part of the tablet surface
    that is stretched over the whole screen. Applies in both mouse and tablet
    mode, so cropping the tablet here crops it for every application.

Control socket: $XDG_RUNTIME_DIR/wacom-pen.sock
Config:         ~/.config/wacom-pen/config.json
CLI:            wacom-pen-ctl
"""

import fnmatch
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

CONFIG_DIR = os.path.expanduser("~/.config/wacom-pen")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "wacom-pen.sock")

PEN_BUTTONS = ("tip", "stylus", "stylus2")
PAD_BUTTONS = tuple(f"pad:{i}" for i in range(len(PAD_KEY_CODES)))
STRIP_BUTTONS = ("strip:0", "strip:1")

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
    if not (has_buttons or has_strips):
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


def find_pad(devices, pen):
    """Pick the best pad: most buttons, strips, and the same USB interface."""
    hint = (devices.get("pad") or "").lower()
    best = None
    best_score = -1
    for dev in candidate_devices(devices):
        if not is_pad_device(dev, hint):
            dev.close()
            continue
        keys, abs_codes = device_facts(dev)
        score = sum(1 for code in keys if code in PAD_KEY_CODES)
        if e.ABS_RX in abs_codes and e.ABS_RY in abs_codes:
            score += 2
        if pen is not None and dev.phys == pen.phys:
            score += 100
        if score > best_score:
            if best is not None:
                best.close()
            best, best_score = dev, score
        else:
            dev.close()
    return best


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
        self.pos = [0, 0]
        # Pen proximity (None = not seen yet, True = hovering, False = lifted).
        # Mouse mode uses it to ignore the coordinates a tablet reports while
        # the pen is out of range, and to re-send the last good position on
        # lift-off so the cursor stays put instead of vanishing.
        self.proximity = None
        self.screen_pos = None
        self.frame_pos = None
        self.proximity_out_at = 0.0
        self.pad = None
        self.grabbed = False
        self.pad_grabbed = False
        self.held = {}
        # Pad layout of the tablet that is actually connected: the express
        # keys it exposes (BTN_0..BTN_9) and whether both touch strips exist.
        self.pad_keys = []
        self.pad_key_index = {}
        self.strip_axes = {}
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
            return
        self.pen = dev
        self.ax = dev.absinfo(e.ABS_X)
        self.ay = dev.absinfo(e.ABS_Y)
        self.pos = [self.ax.value, self.ay.value]

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
        if self.pad is not None or now < self.last_pad_search:
            return
        # A pad only belongs to a tablet that also has a pen attached: never
        # grab a keyboard/mouse (or a stray pad-like interface) while in
        # standby waiting for a tablet.
        if self.pen is None:
            return
        self.last_pad_search = now + 2.0
        dev = find_pad(self.cfg.get("devices") or {}, self.pen)
        if dev is None:
            return
        self.pad = dev
        keys, abs_codes = device_facts(dev)
        self.pad_keys = [code for code in PAD_KEY_CODES if code in keys]
        self.pad_key_index = {
            code: index for index, code in enumerate(self.pad_keys)
        }
        if e.ABS_RX in abs_codes and e.ABS_RY in abs_codes:
            self.strip_axes = dict(STRIP_AXES)
        else:
            self.strip_axes = {}
        log(
            f"pad ready: {dev.name} ({dev.path}) "
            f"keys={len(self.pad_keys)} strips={len(self.strip_axes)}"
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
            self.pointer_ok = wait_for_props(self.pointer.device.path, ("ID_INPUT=1",))
            if not self.pointer_ok:
                log("ERROR: virtual pointer not registered; not grabbing pen")
                return False
            log(f"pointer -> {self.pointer.device.path}")

        if self.tablet is None:
            try:
                self.tablet = build_tablet(src, names["tablet"])
                self.buttons = build_buttons(names["buttons"])
            except Exception as exc:  # noqa: BLE001
                log(f"could not create tablet: {exc}")
                self.close_tablet()
                return False
            self.tablet_ok = wait_for_props(
                self.tablet.device.path, ("ID_INPUT_TABLET=1",)
            )
            self.buttons_ok = wait_for_props(self.buttons.device.path, ("ID_INPUT=1",))
            if not (self.tablet_ok and self.buttons_ok):
                log("ERROR: virtual tablet/buttons not registered")
                self.close_tablet()
                return False
            log(
                f"tablet -> {self.tablet.device.path}, "
                f"buttons -> {self.buttons.device.path}"
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
        self.keyboard_ok = wait_for_props(self.keyboard.device.path, ("ID_INPUT=1",))
        if not self.keyboard_ok:
            log("ERROR: virtual keyboard not registered")
            self.close_keyboard()
            return False
        log(f"keyboard -> {self.keyboard.device.path}")
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

    def close_pad(self):
        if self.pad is None:
            return
        try:
            if self.pad_grabbed:
                self.pad.ungrab()
        except OSError:
            pass
        self.pad_grabbed = False
        try:
            self.pad.close()
        except OSError:
            pass
        self.pad = None
        self.pad_keys = []
        self.pad_key_index = {}
        self.strip_axes = {}

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
            and self.pad_grabbed == (wanted_grab and self.pad is not None)
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
        if self.pad is not None and not self.pad_grabbed:
            try:
                self.pad.grab()
                self.pad_grabbed = True
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
        if self.pad is not None and self.pad_grabbed:
            try:
                self.pad.ungrab()
            except OSError:
                pass
            self.pad_grabbed = False

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
                self.tablet.write(e.EV_KEY, e.BTN_TOUCH, value)
                self.tablet.write(e.EV_SYN, e.SYN_REPORT, 0)
                return "tablet"
            target = (
                self.pointer
                if source == "pen" and self.effective == "mouse"
                else self.buttons
            )
            if target is None:
                return None
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
                    self.pointer.write(e.EV_ABS, e.ABS_X, sx)
                    self.pointer.write(e.EV_ABS, e.ABS_Y, sy)
                    self.frame_pos = (sx, sy)
                    dirty_pointer = True
                elif self.effective == "tablet":
                    if ev.code in (e.ABS_X, e.ABS_Y):
                        self.tablet.write(e.EV_ABS, ev.code, self.to_tablet(ev.code, ev.value))
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

    def on_pad_events(self):
        try:
            events = list(self.pad.read())
        except OSError:
            log("pad disappeared")
            self.close_pad()
            return
        # The strips deliver position and the touch flag in one frame, so the
        # frame is only interpreted once SYN_REPORT closes it.
        frame = []
        for ev in events:
            if ev.type != e.EV_SYN:
                frame.append(ev)
                continue
            if ev.code == e.SYN_REPORT and frame:
                self.handle_pad_frame(frame)
                frame = []

    def handle_pad_frame(self, frame):
        touch = None
        positions = []
        for ev in frame:
            if ev.type == e.EV_ABS and ev.code == e.ABS_MISC:
                touch = ev.value
            elif ev.type == e.EV_ABS and ev.code in self.strip_axes:
                positions.append((self.strip_axes[ev.code], ev.value))

        if DEBUG and (positions or touch is not None):
            debug(f"pad touch={touch} positions={positions}")

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
            index = self.pad_key_index.get(ev.code)
            if index is None:
                continue
            action = self.mapped_action(f"pad:{index}")
            self.run_action(action, ev.value, "pad")

    def scroll_strip(self, positions, landing=False):
        if not self.pad_grabbed or self.buttons is None:
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
        return {
            "ok": True,
            "enabled": bool(self.cfg.get("enabled", True)),
            "mode": self.cfg.get("mode", "mouse"),
            "auto": bool(self.cfg.get("auto", True)),
            "rules": dict(self.cfg.get("rules", {})),
            "buttons": dict(self.cfg.get("buttons", {})),
            "stripSensitivity": self.cfg.get("stripSensitivity", 1.0),
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
            "devices": {
                "pointer": self.pointer_ok,
                "tablet": self.tablet_ok,
                "buttons": self.buttons_ok,
                "keyboard": self.keyboard_ok,
            },
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
            if name not in PEN_BUTTONS and name not in PAD_BUTTONS and name not in STRIP_BUTTONS:
                return {"ok": False, "error": f"unknown button: {name}"}
            if not valid_action(action):
                return {"ok": False, "error": f"bad action: {action}"}
            if name in STRIP_BUTTONS and action.startswith(("mouse:", "key:")):
                return {"ok": False, "error": f"{name} only takes scroll actions"}
            self.cfg.setdefault("buttons", {})[name] = action
            save_config(self.cfg)

        elif cmd == "set-strip-sensitivity":
            self.cfg["stripSensitivity"] = clean_gain(
                req.get("value", 1.0), self.cfg.get("stripSensitivity", 1.0)
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
            if name not in PEN_BUTTONS and name not in PAD_BUTTONS and name not in STRIP_BUTTONS:
                return {"ok": False, "error": f"unknown button: {name}"}
            action = self.mapped_action(name)
            if name in STRIP_BUTTONS:
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
            if self.pen is not None:
                fds.append(self.pen.fd)
            if self.pad is not None:
                fds.append(self.pad.fd)
            hypr_fd = self.hypr.fileno()
            if hypr_fd >= 0:
                fds.append(hypr_fd)
            try:
                readable, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                if self.pen is not None:
                    self.close_pen()
                if self.pad is not None:
                    self.close_pad()
                self.hypr.sock = None
                continue

            for fd in readable:
                if fd is self.server:
                    self.accept()
                elif self.pen is not None and fd == self.pen.fd:
                    self.on_pen_events()
                elif self.pad is not None and fd == self.pad.fd:
                    self.on_pad_events()
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

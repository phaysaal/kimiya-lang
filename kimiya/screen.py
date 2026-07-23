"""The `screen` surface: a GUI as an observable, effectable world.

Two halves, matching the two doors the world extension allows:

    observe screen(...)   the only door for what is on the display
    act screen.…          the only door for input effects

A click delivered through `act screen.…` is a world effect the checker
can see, so K4 (no irreversible act inside a retry), K5 (no unguarded
irreversible act) and K6 (no unframed world effect in a retry body)
apply to GUI driving exactly as they do to files.

**Irreversibility is a property of the control, not the coordinates.**
Whether clicking (900, 412) is recoverable depends on whether that pixel
is "Cancel" or "Delete forever", and the language cannot know which.
Rather than guess — and rather than classify every click as
irreversible, which would make the rule useless noise — the surface
splits clicking in two and makes the program state its own claim:

    act screen.click(x, y)      -- recoverable: navigate, open, focus
    act screen.confirm(x, y)    -- irreversible: send, delete, publish;
                                   the click that commits

`screen.confirm` is irreversible by default, so K5 forces a verified
gate in front of it. Either classification can be overridden in source
(`effect screen.confirm recoverable`) when a program knows better — the
override is then visible to a reviewer, which is the point.

Delivery is by an external input driver. `xdotool` synthesizes events at
the X server, so the application under test cannot distinguish them from
a human — which matters when the app's own input monitoring is part of
what is being tested.

Drivers:

    KIMIYA_SCREEN=xdotool   deliver for real (default)
    KIMIYA_SCREEN=none      record the act, deliver nothing

`KIMIYA_MOCK=1` implies `none`, so the test suite never touches the
developer's real cursor. Under `none`, `observe screen(...)` serves
`KIMIYA_SCREEN_FIXTURE` if one is set and otherwise reports
`exists: false` — it never fabricates a screenshot that was not taken.

**A multi-monitor X screen is a bounding box, not a union.** Observing a
named monitor (`observe screen("eDP-1")`) records that monitor's origin
on the record, so coordinates found inside the capture can be mapped
back to absolute screen coordinates before a click is delivered.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
from pathlib import Path

# action -> arity. `confirm` is a click; it differs only in effect class.
ACTIONS: dict[str, int] = {
    "click": 2,      # x, y
    "confirm": 2,    # x, y          (irreversible by default)
    "type": 1,       # text
    "key": 1,        # keysym or chord, e.g. "Return", "ctrl+a"
    "drag": 4,       # x1, y1, x2, y2
    "scroll": 3,     # x, y, ticks   (negative ticks scroll up)
}

# Acts whose effect class defaults to irreversible for this surface.
IRREVERSIBLE = {"confirm"}

# Typed text is echoed into the trace for audit, but bounded: a runaway
# paste should not become a megabyte of trace.
TRACE_TEXT_LIMIT = 200

# Pixels of slack allowed between a requested pointer position and where
# the pointer actually landed.
POINTER_TOLERANCE = 2


class ScreenError(Exception):
    """A screen act could not be delivered."""


def driver_name() -> str:
    if os.environ.get("KIMIYA_MOCK") == "1":
        return os.environ.get("KIMIYA_SCREEN", "none")
    return os.environ.get("KIMIYA_SCREEN", "xdotool")


def display() -> str:
    return os.environ.get("DISPLAY") or "?"


def target() -> str:
    """Freshness key for the surface: one display is one world."""
    return "screen:" + display()


def _num(value, action: str, pos: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ScreenError(
            f"screen.{action}: argument {pos + 1} must be a number "
            f"(got {value!r})") from None


def plan(action: str, args: list) -> list[list[str]]:
    """Validate one act and return the xdotool argv sequence for it.

    Pure: builds the commands without running them, so `plan` is also
    what a dry run records.
    """
    if action not in ACTIONS:
        raise ScreenError(f"unknown action screen.{action} "
                          f"(known: {', '.join(sorted(ACTIONS))})")
    arity = ACTIONS[action]
    if len(args) != arity:
        raise ScreenError(f"screen.{action} takes {arity} argument(s), "
                          f"got {len(args)}")

    if action in ("click", "confirm"):
        x, y = _num(args[0], action, 0), _num(args[1], action, 1)
        return [["mousemove", "--sync", str(x), str(y)], ["click", "1"]]
    if action == "type":
        return [["type", "--clearmodifiers", "--delay", "12", str(args[0])]]
    if action == "key":
        return [["key", "--clearmodifiers", str(args[0])]]
    if action == "drag":
        x1, y1 = _num(args[0], action, 0), _num(args[1], action, 1)
        x2, y2 = _num(args[2], action, 2), _num(args[3], action, 3)
        return [["mousemove", "--sync", str(x1), str(y1)],
                ["mousedown", "1"],
                ["mousemove", "--sync", str(x2), str(y2)],
                ["mouseup", "1"]]
    # scroll
    x, y = _num(args[0], action, 0), _num(args[1], action, 1)
    ticks = _num(args[2], action, 2)
    button = "4" if ticks < 0 else "5"
    return ([["mousemove", "--sync", str(x), str(y)]]
            + [["click", button]] * abs(ticks))


def _pointer() -> tuple[int, int]:
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                         capture_output=True, text=True, timeout=10).stdout
    vals = dict(line.split("=", 1) for line in out.strip().splitlines()
                if "=" in line)
    return int(vals.get("X", -1)), int(vals.get("Y", -1))


def _verify_pointer(want_x: int, want_y: int) -> None:
    """A move is only delivered if the pointer actually arrived.

    A multi-monitor X screen is a bounding box, not a union: an L-shaped
    layout leaves uncovered regions, and X silently clamps a pointer sent
    there to the nearest valid pixel. The click then lands somewhere the
    program never named while the trace records what it asked for. Read
    the pointer back and refuse the divergence instead.
    """
    got_x, got_y = _pointer()
    if abs(got_x - want_x) > POINTER_TOLERANCE or \
            abs(got_y - want_y) > POINTER_TOLERANCE:
        raise ScreenError(
            f"pointer did not reach ({want_x}, {want_y}) — it is at "
            f"({got_x}, {got_y}). That coordinate is outside every "
            "connected monitor (an L-shaped multi-monitor layout leaves "
            "uncovered regions in the X screen), so the click would land "
            "somewhere else. Check the layout with `xrandr "
            "--listmonitors`.")


def _require_xdotool() -> None:
    if not shutil.which("xdotool"):
        raise ScreenError(
            "xdotool not found — the screen surface needs it to deliver "
            "input (Debian/Ubuntu: apt install xdotool). Set "
            "KIMIYA_SCREEN=none to record acts without delivering them.")
    if not os.environ.get("DISPLAY"):
        raise ScreenError(
            "DISPLAY is unset — no X display to drive. Under Wayland, run "
            "the program in an Xorg session or an Xwayland-backed nested "
            "server; xdotool cannot synthesize input to native Wayland "
            "clients.")


def perform(action: str, args: list) -> dict:
    """Deliver one screen act. Returns a record for the trace."""
    argv = plan(action, args)
    drv = driver_name()
    rec = {"driver": drv, "delivered": False,
           "args": [_trace_arg(a) for a in args]}
    if drv == "none":
        return rec
    if drv != "xdotool":
        raise ScreenError(f"unknown screen driver {drv!r} "
                          "(known: xdotool, none)")
    _require_xdotool()
    for cmd in argv:
        try:
            subprocess.run(["xdotool", *cmd], check=True,
                           capture_output=True, timeout=10)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode(errors="replace").strip()
            raise ScreenError(
                f"xdotool {' '.join(cmd)} failed: {err or e}") from None
        except subprocess.TimeoutExpired:
            raise ScreenError(
                f"xdotool {' '.join(cmd)} timed out") from None
        # Delivery contract: never press a button at an unverified point.
        if cmd[0] == "mousemove":
            _verify_pointer(int(cmd[2]), int(cmd[3]))
    rec["delivered"] = True
    return rec


def _trace_arg(a):
    s = str(a)
    return s if len(s) <= TRACE_TEXT_LIMIT else s[:TRACE_TEXT_LIMIT] + "…"


# ---------------------------------------------------------------- capture

# Region grabbers, best first. Each entry maps a region to an argv.
CAPTURE_TOOLS = [
    ("maim", lambda p, r: ["maim", "-g", _geom(r), str(p)] if r
     else ["maim", str(p)]),
    ("import", lambda p, r: (
        ["import", "-window", "root", "-crop", _geom(r), "+repage",
         f"png:{p}"] if r else ["import", "-window", "root", f"png:{p}"])),
    ("scrot", lambda p, r: (
        ["scrot", "-o", "-a", ",".join(str(v) for v in r), str(p)] if r
        else ["scrot", "-o", str(p)])),
    ("gnome-screenshot", lambda p, r: ["gnome-screenshot", "-f", str(p)]),
]


def _geom(region) -> str:
    x, y, w, h = region
    return f"{w}x{h}+{x}+{y}"


def monitors() -> dict[str, tuple[int, int, int, int]]:
    """Connected outputs as name -> (x, y, w, h), from xrandr."""
    if not shutil.which("xrandr"):
        return {}
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], check=True,
                             capture_output=True, text=True,
                             timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    found: dict[str, tuple[int, int, int, int]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[-1]
        # e.g.  1920/302x1200/189+0+561
        geom = parts[-2]
        try:
            wh, x, y = geom.split("+")
            w, hgt = wh.split("x")
            found[name] = (int(x), int(y),
                           int(w.split("/")[0]), int(hgt.split("/")[0]))
        except ValueError:
            continue
    return found


def png_size(path) -> tuple[int, int]:
    """Width and height from a PNG IHDR — no image library needed."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", head[16:24])


def _resolve_region(args: list) -> tuple[tuple | None, str]:
    """(region, label) from the `observe screen(...)` arguments."""
    if not args:
        return None, "root"
    if len(args) == 1:
        name = str(args[0])
        mons = monitors()
        if name in mons:
            return mons[name], name
        raise ScreenError(
            f"no monitor named {name!r} — connected: "
            f"{', '.join(sorted(mons)) or 'none detected'}")
    if len(args) == 4:
        x, y, w, h = (_num(a, "observe", i) for i, a in enumerate(args))
        if w <= 0 or h <= 0:
            raise ScreenError(
                f"observe screen: width and height must be positive "
                f"(got {w}x{h})")
        return (x, y, w, h), f"{w}x{h}+{x}+{y}"
    raise ScreenError(
        "observe screen takes no arguments (whole screen), one monitor "
        "name, or four numbers (x, y, w, h)")


def capture(args: list, dest_dir) -> dict:
    """Take a screenshot. Returns the observation record.

    `x`/`y` carry the capture origin so a coordinate found inside the
    image can be mapped back to an absolute screen coordinate — the
    difference matters the moment a second monitor exists.
    """
    region, label = _resolve_region(args)
    x, y = (region[0], region[1]) if region else (0, 0)
    base = {"kind": "screen", "display": display(), "region": label,
            "x": x, "y": y, "width": 0, "height": 0,
            "path": "", "sha": "", "exists": False, "driver": driver_name()}

    if driver_name() == "none":
        fixture = os.environ.get("KIMIYA_SCREEN_FIXTURE")
        if not fixture:
            # No screenshot was taken; say so rather than invent one.
            return base
        path = Path(fixture)
        if not path.exists():
            raise ScreenError(f"KIMIYA_SCREEN_FIXTURE={fixture} does not "
                              "exist")
        return _finish(base, path)

    tool = next((t for t in CAPTURE_TOOLS if shutil.which(t[0])), None)
    if tool is None:
        raise ScreenError(
            "no screenshot tool found — install one of "
            f"{', '.join(t[0] for t in CAPTURE_TOOLS)}. Set "
            "KIMIYA_SCREEN=none with KIMIYA_SCREEN_FIXTURE=<png> to run "
            "against a recorded screenshot instead.")
    name, build = tool
    if name == "gnome-screenshot" and region:
        raise ScreenError(
            "gnome-screenshot cannot capture a region; install maim, "
            "import (ImageMagick) or scrot for region capture")
    if not os.environ.get("DISPLAY"):
        raise ScreenError("DISPLAY is unset — no X display to capture")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"shot-{label.replace('/', '_')}.png"
    try:
        subprocess.run(build(path, region), check=True,
                       capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace").strip()
        raise ScreenError(f"{name} failed: {err or e}") from None
    except subprocess.TimeoutExpired:
        raise ScreenError(f"{name} timed out") from None
    if not path.exists():
        raise ScreenError(f"{name} produced no file at {path}")
    base["driver"] = name
    return _finish(base, path)


def _finish(rec: dict, path) -> dict:
    w, h = png_size(path)
    rec.update({"path": str(path), "exists": True, "width": w, "height": h,
                "sha": hashlib.sha256(
                    Path(path).read_bytes()).hexdigest()[:12]})
    return rec

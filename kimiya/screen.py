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
import shlex
import shutil
import struct
import subprocess
from dataclasses import dataclass
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


@dataclass
class Display:
    """One seat: a display where a screen exists and input can land.

    An actor index (`act<A>`, `observe screen<A>(...)`) resolves to one
    of these. Three shapes:

      local, ambient   x11=None            → the env DISPLAY
      local, explicit  x11=":1"            → another X server on this box
      remote           ssh="user@host"     → xdotool + capture run over
                                             ssh; screenshots stream back

    Two actors on distinct X servers are genuinely independent seats.
    Two actors that resolve to the same X server share one pointer and
    one keyboard focus — the actor names then label intent, not
    independence, and interleaving their acts can interfere. The
    certificate reports where each actor resolved so a reviewer can see
    which situation a run was in.
    """

    name: str = "default"
    x11: str | None = None
    ssh: str | None = None
    monitor: str | None = None    # default capture region, by xrandr name

    @property
    def resolved_x11(self) -> str:
        if self.x11:
            return self.x11
        if self.ssh:
            # The local DISPLAY says nothing about a remote machine.
            return ":0"
        return os.environ.get("DISPLAY") or "?"

    @property
    def is_remote(self) -> bool:
        return bool(self.ssh)

    @property
    def label(self) -> str:
        base = self.resolved_x11
        return f"{self.ssh} {base}" if self.ssh else base

    def target(self) -> str:
        """Freshness key: one seat is one world."""
        return "screen:" + self.label

    def run(self, argv: list[str], timeout: int = 10):
        """Run a display-bound command locally or over ssh.

        BatchMode forbids password prompts — a harness must fail fast on
        missing key auth, not hang waiting for a human to type.
        """
        if self.ssh:
            remote = " ".join(shlex.quote(a) for a in argv)
            cmd = ["ssh", "-o", "BatchMode=yes", self.ssh,
                   f"DISPLAY={shlex.quote(self.resolved_x11)} {remote}"]
            return subprocess.run(cmd, capture_output=True, timeout=timeout)
        env = dict(os.environ, DISPLAY=self.resolved_x11)
        return subprocess.run(argv, capture_output=True, timeout=timeout,
                              env=env)


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

    # No `--sync` on mousemove: it can hang forever on nested X servers
    # (Xephyr/Xvfb) waiting for a motion event that never fires. The
    # pointer read-back in _verify_pointer provides the stronger
    # guarantee anyway — arrival is confirmed, not just awaited.
    if action in ("click", "confirm"):
        x, y = _num(args[0], action, 0), _num(args[1], action, 1)
        return [["mousemove", str(x), str(y)], ["click", "1"]]
    if action == "type":
        return [["type", "--clearmodifiers", "--delay", "12", str(args[0])]]
    if action == "key":
        return [["key", "--clearmodifiers", str(args[0])]]
    if action == "drag":
        x1, y1 = _num(args[0], action, 0), _num(args[1], action, 1)
        x2, y2 = _num(args[2], action, 2), _num(args[3], action, 3)
        return [["mousemove", str(x1), str(y1)],
                ["mousedown", "1"],
                ["mousemove", str(x2), str(y2)],
                ["mouseup", "1"]]
    # scroll
    x, y = _num(args[0], action, 0), _num(args[1], action, 1)
    ticks = _num(args[2], action, 2)
    button = "4" if ticks < 0 else "5"
    return ([["mousemove", str(x), str(y)]]
            + [["click", button]] * abs(ticks))


def _pointer(disp: Display) -> tuple[int, int]:
    out = disp.run(["xdotool", "getmouselocation", "--shell"]).stdout
    text = out.decode(errors="replace") if isinstance(out, bytes) else out
    vals = dict(line.split("=", 1) for line in text.strip().splitlines()
                if "=" in line)
    return int(vals.get("X", -1)), int(vals.get("Y", -1))


def _verify_pointer(disp: Display, want_x: int, want_y: int) -> None:
    """A move is only delivered if the pointer actually arrived.

    A multi-monitor X screen is a bounding box, not a union: an L-shaped
    layout leaves uncovered regions, and X silently clamps a pointer sent
    there to the nearest valid pixel. The click then lands somewhere the
    program never named while the trace records what it asked for. Read
    the pointer back and refuse the divergence instead.
    """
    got_x, got_y = _pointer(disp)
    if abs(got_x - want_x) > POINTER_TOLERANCE or \
            abs(got_y - want_y) > POINTER_TOLERANCE:
        raise ScreenError(
            f"pointer on {disp.label} did not reach ({want_x}, {want_y}) "
            f"— it is at ({got_x}, {got_y}). That coordinate is outside "
            "every connected monitor (an L-shaped multi-monitor layout "
            "leaves uncovered regions in the X screen), so the click "
            "would land somewhere else. Check the layout with `xrandr "
            "--listmonitors`.")


def _require_xdotool(disp: Display) -> None:
    if disp.is_remote:
        probe = disp.run(["sh", "-c", "command -v xdotool"])
        if probe.returncode != 0:
            err = probe.stderr.decode(errors="replace").strip()
            raise ScreenError(
                f"xdotool not available on {disp.ssh} (or ssh failed: "
                f"{err[:120]}) — the remote seat needs xdotool installed "
                "and key-based ssh auth (BatchMode)")
        return
    if not shutil.which("xdotool"):
        raise ScreenError(
            "xdotool not found — the screen surface needs it to deliver "
            "input (Debian/Ubuntu: apt install xdotool). Set "
            "KIMIYA_SCREEN=none to record acts without delivering them.")
    if disp.resolved_x11 == "?":
        raise ScreenError(
            "DISPLAY is unset — no X display to drive. Under Wayland, run "
            "the program in an Xorg session or an Xwayland-backed nested "
            "server; xdotool cannot synthesize input to native Wayland "
            "clients.")


def perform(action: str, args: list, disp: Display | None = None) -> dict:
    """Deliver one screen act on a seat. Returns a record for the trace."""
    disp = disp or Display()
    argv = plan(action, args)
    drv = driver_name()
    rec = {"driver": drv, "delivered": False, "seat": disp.label,
           "args": [_trace_arg(a) for a in args]}
    if drv == "none":
        return rec
    if drv != "xdotool":
        raise ScreenError(f"unknown screen driver {drv!r} "
                          "(known: xdotool, none)")
    _require_xdotool(disp)
    for cmd in argv:
        try:
            out = disp.run(["xdotool", *cmd])
        except subprocess.TimeoutExpired:
            raise ScreenError(
                f"xdotool {' '.join(cmd)} timed out on "
                f"{disp.label}") from None
        if out.returncode != 0:
            err = out.stderr.decode(errors="replace").strip()
            raise ScreenError(
                f"xdotool {' '.join(cmd)} failed on {disp.label}: "
                f"{err or out.returncode}")
        # Delivery contract: never press a button at an unverified point.
        if cmd[0] == "mousemove":
            _verify_pointer(disp, int(cmd[-2]), int(cmd[-1]))
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


def monitors(disp: Display | None = None) -> dict[str, tuple[int, int, int, int]]:
    """Connected outputs as name -> (x, y, w, h), from xrandr."""
    disp = disp or Display()
    if not disp.is_remote and not shutil.which("xrandr"):
        return {}
    try:
        res = disp.run(["xrandr", "--listmonitors"])
        if res.returncode != 0:
            return {}
        out = res.stdout
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
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


def _resolve_region(args: list, disp: Display) -> tuple[tuple | None, str]:
    """(region, label) from the `observe screen(...)` arguments."""
    if not args:
        if disp.monitor:            # the seat's declared default region
            args = [disp.monitor]
        else:
            return None, "root"
    if len(args) == 1:
        name = str(args[0])
        if driver_name() == "none":
            # Recording mode: no X server is consulted; the fixture (or
            # exists:false) stands in and the name is kept as the label.
            return None, name
        mons = monitors(disp)
        if name in mons:
            return mons[name], name
        raise ScreenError(
            f"no monitor named {name!r} on {disp.label} — connected: "
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


def capture(args: list, dest_dir, disp: Display | None = None) -> dict:
    """Take a screenshot of a seat. Returns the observation record.

    `x`/`y` carry the capture origin so a coordinate found inside the
    image can be mapped back to an absolute screen coordinate — the
    difference matters the moment a second monitor exists. Origins are
    per-seat: two actors' captures never share a coordinate space.
    """
    disp = disp or Display()
    region, label = _resolve_region(args, disp)
    x, y = (region[0], region[1]) if region else (0, 0)
    base = {"kind": "screen", "display": disp.label, "actor": disp.name,
            "region": label, "x": x, "y": y, "width": 0, "height": 0,
            "path": "", "sha": "", "exists": False, "driver": driver_name()}

    if driver_name() == "none":
        # Per-actor fixture first, shared fixture as the fallback — so a
        # two-seat scenario can be replayed against two recordings.
        fixture = (os.environ.get(f"KIMIYA_SCREEN_FIXTURE_{disp.name}")
                   or os.environ.get("KIMIYA_SCREEN_FIXTURE"))
        if not fixture:
            # No screenshot was taken; say so rather than invent one.
            return base
        path = Path(fixture)
        if not path.exists():
            raise ScreenError(f"screen fixture {fixture} does not exist")
        return _finish(base, path)

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    safe = f"{disp.name}-{label}".replace("/", "_").replace(":", "_")
    path = dest / f"shot-{safe}.png"

    if disp.is_remote:
        return _capture_remote(disp, region, base, path)

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
    if disp.resolved_x11 == "?":
        raise ScreenError("DISPLAY is unset — no X display to capture")

    try:
        out = disp.run(build(path, region), timeout=30)
    except subprocess.TimeoutExpired:
        raise ScreenError(f"{name} timed out on {disp.label}") from None
    if out.returncode != 0:
        err = out.stderr.decode(errors="replace").strip()
        raise ScreenError(f"{name} failed on {disp.label}: "
                          f"{err or out.returncode}")
    if not path.exists():
        raise ScreenError(f"{name} produced no file at {path}")
    base["driver"] = name
    return _finish(base, path)


def _capture_remote(disp: Display, region, base: dict, path: Path) -> dict:
    """Capture a remote seat: the tool runs there, the PNG streams back.

    One ssh round trip: a shell snippet picks whichever of maim/import
    exists on the remote host and writes the PNG to stdout.
    """
    if region:
        g = _geom(region)
        snippet = (f"if command -v maim >/dev/null 2>&1; then "
                   f"maim -g {shlex.quote(g)}; "
                   f"elif command -v import >/dev/null 2>&1; then "
                   f"import -window root -crop {shlex.quote(g)} +repage "
                   f"png:-; else echo NOTOOL >&2; exit 9; fi")
    else:
        snippet = ("if command -v maim >/dev/null 2>&1; then maim; "
                   "elif command -v import >/dev/null 2>&1; then "
                   "import -window root png:-; "
                   "else echo NOTOOL >&2; exit 9; fi")
    try:
        out = disp.run(["sh", "-c", snippet], timeout=60)
    except subprocess.TimeoutExpired:
        raise ScreenError(f"remote capture timed out on "
                          f"{disp.label}") from None
    if out.returncode == 9:
        raise ScreenError(f"no screenshot tool on {disp.ssh} — install "
                          "maim or ImageMagick there")
    if out.returncode != 0 or not out.stdout:
        err = out.stderr.decode(errors="replace").strip()
        raise ScreenError(f"remote capture failed on {disp.label}: "
                          f"{err[:200] or out.returncode}")
    path.write_bytes(out.stdout)
    base["driver"] = "ssh"
    return _finish(base, path)


def _finish(rec: dict, path) -> dict:
    w, h = png_size(path)
    rec.update({"path": str(path), "exists": True, "width": w, "height": h,
                "sha": hashlib.sha256(
                    Path(path).read_bytes()).hexdigest()[:12]})
    return rec

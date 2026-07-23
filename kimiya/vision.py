"""The vision locator: `select` over a screenshot.

Core Kimiya's `select` is retrieval — pick the members of a store that
bear on a query, at a stated recall. A screenshot is a store too: its
members are the controls on the display. So `select` over an observation
of the screen is the same construct, with a different instrument behind
it, and it returns the same shape: a list, best first.

    shot := observe screen("eDP-1")
    hits := select<0.97>("the Create Group button", shot) under k_ui by L
    b    := first(hits)
    act screen.click(b.x, b.y)

Two things make this an instrument rather than an oracle:

**It is measured, not asserted.** A vision locate contributes its
datasheet's conservative end to θ under the task name `locate:<context>`,
exactly like a judged relation — not the recall the programmer wrote. The
declared recall stays in the source as the programmer's coverage claim
and is *checked against* the measured β; claiming more than the
instrument has been shown to deliver is a warning on every run.

**Coordinates are absolute.** Boxes come back normalized to the captured
image; they are mapped through the capture's origin so a click lands
where the control actually is. On a multi-monitor layout the capture
origin is not (0, 0), and getting this wrong is a click in the wrong
window, not a visible error.

**Locates are cached; the cache has two grades.** Every live locate is
stored in the workspace keyed by (task, capture size, description).

  * An **exact** hit — the current screenshot's SHA matches the cached
    one — is a reading of this very image. Byte-identical pixels,
    identical answer; it re-enters θ at the datasheet rate with nothing
    to disclose.

  * A **replay** hit (`--replay` / `KIMIYA_REPLAY=1`) reuses the cached
    boxes although the pixels have changed. That is the GUI-harness
    trick that makes reruns free — and it is sound only because the
    locate never carries the verdict: a stale coordinate produces a
    wrong click, the world diverges, and the *live* gates (kernel
    checks, `shows` judges — never cached) refuse to commit. The
    certificate counts replays and says the assumption out loud.

Judgments are never cached: a `shows` is a claim about the current
state of the world, and yesterday's screen is not evidence about today's.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

LOCATE_SYSTEM = (
    "You LOCATE user-interface controls in a screenshot. Return ONLY a "
    "JSON array. Each element: {\"box\": [x0, y0, x1, y1], "
    "\"label\": \"<visible text or role>\", \"confidence\": <0..1>}. "
    "Coordinates are integer PIXELS of the image as given, origin "
    "top-left: x0,y0 is the control's top-left corner and x1,y1 its "
    "bottom-right. Return the best match first. Return [] if the "
    "described control is not visible. No prose, no markdown fences."
)

# Structured-output schema for backends that can enforce one, so a
# locate returns valid JSON by construction rather than by regex salvage.
LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "controls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "box": {"type": "array",
                            "items": {"type": "integer"}},
                    "label": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["box", "label", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["controls"],
    "additionalProperties": False,
}


def locate_task(context: str | None) -> str:
    """Datasheet key for this instrument, mirroring `entails:<ctx>`."""
    return f"locate:{context or 'unscoped'}"


class ReplayMiss(Exception):
    """Replay mode was asked for a locate that was never run live."""


class LocateCache:
    """Past locate readings, keyed by (task, capture size, description).

    Stores the parsed image-pixel boxes rather than absolute screen
    coordinates, so a cached control maps correctly even if the monitor
    has moved within the X screen since the live run.

    Only non-empty results are cached — a miss may be transient (a
    dialog mid-animation), and caching it would replay the failure
    forever.
    """

    def __init__(self, workspace):
        self.path = Path(workspace) / "locates.json"
        self._d: dict = {}
        if self.path.exists():
            try:
                self._d = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._d = {}

    @staticmethod
    def key(task: str, shot: dict, description: str) -> str:
        return (f"{task}|{shot.get('width')}x{shot.get('height')}"
                f"|{description}")

    def get(self, task: str, shot: dict, description: str) -> dict | None:
        return self._d.get(self.key(task, shot, description))

    def put(self, task: str, shot: dict, description: str,
            parsed: list, agent_label: str):
        self._d[self.key(task, shot, description)] = {
            "boxes": [{"box": list(p["box"]), "label": p["label"],
                       "confidence": p["confidence"]} for p in parsed],
            "sha": shot.get("sha", ""),
            "agent": agent_label,
            "ts": time.time(),
        }
        self.path.write_text(json.dumps(self._d, indent=2))


def _parse_boxes(text: str) -> list[dict]:
    """Salvage a control list from a model reply.

    Structured outputs make this exact on backends that support them;
    it stays tolerant for the ones that don't — a bare array, an object
    wrapping one, and the usual markdown fencing all parse.
    """
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    raw = None
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, flags=re.S)
        if not m:
            continue
        try:
            raw = json.loads(m.group(0))
            break
        except json.JSONDecodeError:
            continue
    if isinstance(raw, dict):
        raw = (raw.get("controls") or raw.get("boxes")
               or raw.get("results") or [raw])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        box = item.get("box") or item.get("bbox") or item.get("box_2d")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        conf = item.get("confidence", item.get("score", 0.0))
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        out.append({"box": (x0, y0, x1, y1),
                    "label": str(item.get("label", "")),
                    "confidence": conf})
    return out


def _to_pixels(box, shot: dict) -> dict:
    """Image-pixel box -> absolute screen rect and centre.

    The only transform is the capture origin. Coordinates arrive as
    pixels of the image as captured, so a control found in a monitor
    capture that starts at (1920, 0) comes back at its true screen
    position rather than at its offset within the crop.
    """
    x0, y0, x1, y1 = box
    ox, oy = shot.get("x") or 0, shot.get("y") or 0
    return {
        "x": round(ox + (x0 + x1) / 2),      # centre, absolute
        "y": round(oy + (y0 + y1) / 2),
        "w": round(abs(x1 - x0)),
        "h": round(abs(y1 - y0)),
        "left": round(ox + min(x0, x1)),
        "top": round(oy + min(y0, y1)),
    }


def _build_hits(parsed: list, shot: dict) -> list[dict]:
    hits = []
    for p in parsed:
        rec = _to_pixels(p["box"], shot)
        rec["label"] = p["label"]
        rec["confidence"] = p["confidence"]
        hits.append(rec)
    hits.sort(key=lambda r: -r["confidence"])
    return hits


def locate(oracle, agent, trace, shot: dict, description: str,
           purpose: str, context: str | None,
           cache: LocateCache | None = None,
           replay: bool = False) -> tuple[list[dict], str]:
    """Find controls matching `description` in `shot`. Best first.

    Returns (hits, source) where source is one of:
      "live"    — the model read this screenshot
      "exact"   — cache hit, screenshot bytes identical to the live run
      "replay"  — cache hit under replay mode, pixels have changed
    """
    task = locate_task(context)
    ent = cache.get(task, shot, description) if cache else None

    source = None
    if ent and ent.get("sha") and ent["sha"] == shot.get("sha"):
        source = "exact"          # same image, same reading — free
    elif replay:
        if ent is None:
            raise ReplayMiss(
                f"no cached locate for {description!r} at "
                f"{shot.get('width')}x{shot.get('height')} — run live "
                "once before replaying")
        source = "replay"         # layout-stability assumption, disclosed

    if source:
        assert ent is not None
        parsed = [{"box": tuple(b["box"]), "label": b["label"],
                   "confidence": b["confidence"]} for b in ent["boxes"]]
        hits = _build_hits(parsed, shot)
        trace.append({"kind": "locate", "task": task, "cache": source,
                      "description": description[:200],
                      "screenshot_sha": shot.get("sha", ""),
                      "cached_sha": ent.get("sha", ""),
                      "capture_origin": [shot.get("x"), shot.get("y")],
                      "agent": ent.get("agent", "cached"), "hits": hits})
        return hits, source

    w, h = shot.get("width") or 0, shot.get("height") or 0
    prompt = (f"PURPOSE: {purpose}\n\n"
              f"The screenshot is {w}x{h} pixels.\n"
              f"Locate in it: {description}")
    try:
        out = oracle.complete(agent, prompt, system=LOCATE_SYSTEM,
                              temperature=0.0, max_tokens=2048,
                              images=[shot["path"]], schema=LOCATE_SCHEMA)
        err = None
    except (OSError, RuntimeError) as e:
        out, err = "", str(e)[:200]
    parsed = _parse_boxes(out)
    hits = _build_hits(parsed, shot)
    if cache and parsed and not err:
        cache.put(task, shot, description, parsed, agent.label())
    trace.append({"kind": "locate", "task": task, "cache": "live",
                  "description": description[:200],
                  "screenshot": shot.get("path", ""),
                  "screenshot_sha": shot.get("sha", ""),
                  "capture_origin": [shot.get("x"), shot.get("y")],
                  "capture_size": [shot.get("width"), shot.get("height")],
                  "agent": agent.label(), "hits": hits,
                  **({"error": err} if err else {})})
    return hits, "live"

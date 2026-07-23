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
"""

from __future__ import annotations

import json
import re

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


def locate(oracle, agent, trace, shot: dict, description: str,
           purpose: str, context: str | None) -> list[dict]:
    """Find controls matching `description` in `shot`. Best first."""
    w, h = shot.get("width") or 0, shot.get("height") or 0
    prompt = (f"PURPOSE: {purpose}\n\n"
              f"The screenshot is {w}x{h} pixels.\n"
              f"Locate in it: {description}")
    task = locate_task(context)
    try:
        out = oracle.complete(agent, prompt, system=LOCATE_SYSTEM,
                              temperature=0.0, max_tokens=2048,
                              images=[shot["path"]], schema=LOCATE_SCHEMA)
        err = None
    except (OSError, RuntimeError) as e:
        out, err = "", str(e)[:200]
    parsed = _parse_boxes(out)
    hits = []
    for p in parsed:
        rec = _to_pixels(p["box"], shot)
        rec["label"] = p["label"]
        rec["confidence"] = p["confidence"]
        hits.append(rec)
    hits.sort(key=lambda r: -r["confidence"])
    trace.append({"kind": "locate", "task": task,
                  "description": description[:200],
                  "screenshot": shot.get("path", ""),
                  "screenshot_sha": shot.get("sha", ""),
                  "capture_origin": [shot.get("x"), shot.get("y")],
                  "capture_size": [shot.get("width"), shot.get("height")],
                  "agent": agent.label(), "hits": hits,
                  **({"error": err} if err else {})})
    return hits

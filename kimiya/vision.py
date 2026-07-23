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
    "JSON array. Each element: {\"box_2d\": [ymin, xmin, ymax, xmax], "
    "\"label\": \"<visible text or role>\", \"confidence\": <0..1>}. "
    "Coordinates are integers normalized to 0-1000 over the image, "
    "origin top-left. Return the best match first. Return [] if the "
    "described control is not visible. No prose, no markdown fences."
)


def locate_task(context: str | None) -> str:
    """Datasheet key for this instrument, mirroring `entails:<ctx>`."""
    return f"locate:{context or 'unscoped'}"


def _parse_boxes(text: str) -> list[dict]:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        box = item.get("box_2d") or item.get("box") or item.get("bbox")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            ymin, xmin, ymax, xmax = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        conf = item.get("confidence", item.get("score", 0.0))
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        out.append({"box": (ymin, xmin, ymax, xmax),
                    "label": str(item.get("label", "")),
                    "confidence": conf})
    return out


def _to_pixels(box, shot: dict) -> dict:
    """Normalized 0-1000 box -> absolute screen rect and centre."""
    ymin, xmin, ymax, xmax = box
    w, h = shot.get("width") or 0, shot.get("height") or 0
    ox, oy = shot.get("x") or 0, shot.get("y") or 0
    x1, x2 = xmin / 1000.0 * w, xmax / 1000.0 * w
    y1, y2 = ymin / 1000.0 * h, ymax / 1000.0 * h
    return {
        "x": round(ox + (x1 + x2) / 2),      # centre, absolute
        "y": round(oy + (y1 + y2) / 2),
        "w": round(abs(x2 - x1)),
        "h": round(abs(y2 - y1)),
        "left": round(ox + min(x1, x2)),
        "top": round(oy + min(y1, y2)),
    }


def locate(oracle, agent, trace, shot: dict, description: str,
           purpose: str, context: str | None) -> list[dict]:
    """Find controls matching `description` in `shot`. Best first."""
    prompt = (f"PURPOSE: {purpose}\n\n"
              f"Locate in the screenshot: {description}")
    task = locate_task(context)
    try:
        out = oracle.complete(agent, prompt, system=LOCATE_SYSTEM,
                              temperature=0.0, max_tokens=512,
                              images=[shot["path"]])
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

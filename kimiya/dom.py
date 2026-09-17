"""The `dom` surface: a host-owned webview as an observable, effectable world.

A host application (the SafeSelf/CounterSelf pattern: a chromeless Tauri
webview rendering a login-walled site) exposes its page to Kimiya through
an HTTP bridge. The program drives the page with structured ops; the host
turns each op into JavaScript built from a fixed template with the
parameters injected as JSON-encoded data. **Kimiya never sends code** —
there is no eval primitive on this surface, by construction — so the
Kimiya program is the auditable contract for everything the webview does.

Two observation doors, one page:

    observe dom(selector?)   the DOM: {text, nodes} — what to act on
    observe view()           pixels: a screenshot of the webview — what a
                             vision judge verifies (authenticity is
                             checked on the human-visible render, not on
                             the DOM the app manipulates)

Acts (all world effects, so K4/K5/K6 apply exactly as on `screen`):

    act dom.open(url)             navigate                (recoverable)
    act dom.click(sel_or_node)    click an element        (recoverable)
    act dom.confirm(sel_or_node)  the click that commits  (IRREVERSIBLE)
    act dom.scroll(dx, dy)        scroll by pixels        (recoverable)
    act dom.fill(sel, text)       set an input's value    (recoverable)
    act dom.press(sel, key)       key to an element       (recoverable)
    act dom.emit(channel, value)  hand a value to the host (IRREVERSIBLE)

Like `screen.click`/`screen.confirm`, whether a click can be taken back
is a property of the control, not the coordinates — the program states
its own claim by choosing the op, and K5 forces a verified gate in front
of `dom.confirm`. `effect dom.press irreversible` can override per
program, visibly.

`dom.emit` is the sanctioned data-out channel and it is irreversible:
once a value reaches the host it cannot be recalled, so K5 demands a
gate. The audit record keeps only the channel, a SHA-256 prefix and the
length — the value itself never lands in the trace or the certificate.

This surface takes no actor index: the host binds exactly one webview
window to the run.

Drivers:

    KIMIYA_DOM=none      record ops, deliver nothing (the default)
    KIMIYA_DOM=bridge    deliver over HTTP; needs
                           KIMIYA_DOM_BRIDGE=http://127.0.0.1:<port>/op
                           KIMIYA_DOM_TOKEN=<bearer>   (never logged)

Under `none`, `observe dom(...)` serves `KIMIYA_DOM_FIXTURE` (a JSON
file `{text, nodes}`) and `observe view()` serves
`KIMIYA_DOM_VIEW_FIXTURE` (a PNG); with no fixture both report
`exists: false` — a snapshot that was not taken is never fabricated.

The wire protocol is one JSON POST per op (see docs/dom_bridge.md); any
`{"ok": false}` response raises DomError — a blocked URL or a stale
selector is the host refusing, and the program handles it or abstains.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# action -> arity. `confirm` is a click; it differs only in effect class.
ACTIONS: dict[str, int] = {
    "open": 1,       # url
    "click": 1,      # selector or node
    "confirm": 1,    # selector or node   (irreversible by default)
    "scroll": 2,     # dx, dy             (negative dy scrolls up)
    "fill": 2,       # selector, text
    "press": 2,      # selector, key      e.g. "Enter"
    "emit": 2,       # channel, value     (irreversible: data-out)
}

# Acts whose effect class defaults to irreversible for this surface.
IRREVERSIBLE = {"confirm", "emit"}

# Op echoes into the trace are bounded, like typed text on `screen`.
TRACE_TEXT_LIMIT = 200


class DomError(Exception):
    """A dom op could not be delivered, or the host refused it."""


def driver_name() -> str:
    # Unlike `screen`, delivery is never ambient: it needs a host bridge,
    # so the default is the recording mode.
    return os.environ.get("KIMIYA_DOM", "none")


def bridge_url() -> str | None:
    return os.environ.get("KIMIYA_DOM_BRIDGE") or None


def bridge_host() -> str | None:
    """The endpoint's host:port for the certificate — never the token."""
    url = bridge_url()
    if not url:
        return None
    return urllib.parse.urlparse(url).netloc or url


def target() -> str:
    """Freshness key for the surface: one bound webview is one world."""
    return "dom:" + (bridge_host() or "none")


def _post(payload: dict, timeout: int | None = None) -> dict:
    url = bridge_url()
    if not url:
        raise DomError(
            "KIMIYA_DOM=bridge but KIMIYA_DOM_BRIDGE is not set — the "
            "host must provide the per-run endpoint "
            "(http://127.0.0.1:<port>/op)")
    token = os.environ.get("KIMIYA_DOM_TOKEN")
    if not token:
        raise DomError(
            "KIMIYA_DOM=bridge but KIMIYA_DOM_TOKEN is not set — the "
            "host must provide the per-run bearer token")
    timeout = timeout or int(os.environ.get("KIMIYA_TIMEOUT", 60))
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise DomError(
            f"dom bridge at {bridge_host()} unreachable: {e}") from None
    except (json.JSONDecodeError, TimeoutError, OSError) as e:
        raise DomError(f"dom bridge error: {e}") from None
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error", "no reason given") \
            if isinstance(data, dict) else "malformed response"
        raise DomError(f"host refused dom.{payload.get('op')}: {err}")
    return data


def _selector(value, action: str) -> str:
    """A click/fill/press target: a selector string or a select() node."""
    if isinstance(value, dict) and "selector" in value:
        return str(value["selector"])
    if isinstance(value, str):
        return value
    raise DomError(
        f"dom.{action}: target must be a selector string or a node from "
        f"select (got {type(value).__name__})")


def _num(value, action: str, pos: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise DomError(
            f"dom.{action}: argument {pos + 1} must be a number "
            f"(got {value!r})") from None


def plan(action: str, args: list) -> dict:
    """Validate one act and return the structured op for it.

    Pure: builds the op without delivering it, so `plan` is also what a
    dry run records. `confirm` travels as a `click` op with an advisory
    `irreversible` flag — the effect class is the program's claim, and
    the host's whitelist stays minimal.
    """
    if action not in ACTIONS:
        raise DomError(f"unknown action dom.{action} "
                       f"(known: {', '.join(sorted(ACTIONS))})")
    arity = ACTIONS[action]
    if len(args) != arity:
        raise DomError(f"dom.{action} takes {arity} argument(s), "
                       f"got {len(args)}")
    if action == "open":
        return {"op": "open", "url": str(args[0])}
    if action in ("click", "confirm"):
        op: dict = {"op": "click", "selector": _selector(args[0], action)}
        if action == "confirm":
            op["irreversible"] = True
        return op
    if action == "scroll":
        return {"op": "scroll", "dx": _num(args[0], action, 0),
                "dy": _num(args[1], action, 1)}
    if action == "fill":
        return {"op": "fill", "selector": _selector(args[0], action),
                "text": str(args[1])}
    if action == "press":
        return {"op": "press", "selector": _selector(args[0], action),
                "key": str(args[1])}
    # emit
    return {"op": "emit", "channel": str(args[0]), "value": str(args[1])}


def _trace_arg(a):
    from .runtime import Secret
    if isinstance(a, Secret):
        return a.redacted()
    if isinstance(a, dict) and "selector" in a:
        a = a["selector"]
    s = str(a)
    return s if len(s) <= TRACE_TEXT_LIMIT else s[:TRACE_TEXT_LIMIT] + "…"


def perform(action: str, args: list) -> dict:
    """Deliver one dom act. Returns a record for the trace.

    For `emit` the record carries the channel, the value's SHA-256
    prefix and its length — never the value: emit exists to move data
    OUT through the host, and the audit trail proves *which* value left
    without repeating it.
    """
    op = plan(action, args)
    drv = driver_name()
    if action == "emit":
        value = str(args[1])
        rec = {"driver": drv, "delivered": False,
               "channel": op["channel"],
               "value_sha": hashlib.sha256(value.encode()).hexdigest()[:12],
               "value_len": len(value)}
    else:
        rec = {"driver": drv, "delivered": False,
               "args": [_trace_arg(a) for a in args]}
    if drv == "none":
        return rec
    if drv != "bridge":
        raise DomError(f"unknown dom driver {drv!r} "
                       "(known: bridge, none)")
    resp = _post(op)
    if resp.get("matched") == 0:
        raise DomError(
            f"dom.{action}: selector matched no element — the snapshot "
            "it came from is stale; observe dom(...) again")
    rec["delivered"] = True
    return rec


# ---------------------------------------------------------------- observe

def _snapshot_base() -> dict:
    return {"kind": "dom", "text": "", "nodes": [], "sha": "",
            "exists": False, "driver": driver_name()}


def _finish_snapshot(base: dict, text: str, nodes: list) -> dict:
    clean = []
    for n in nodes or []:
        if not isinstance(n, dict) or "selector" not in n:
            continue
        clean.append({"selector": str(n.get("selector", "")),
                      "role": str(n.get("role", "")),
                      "text": str(n.get("text", "")),
                      "visible": bool(n.get("visible", True)),
                      # The one thing pixels cannot show: where an anchor
                      # really goes. Hosts fill it for links; "" otherwise.
                      "href": str(n.get("href", "") or "")})
    ident = text + "".join(n["selector"] for n in clean)
    base.update({"text": text, "nodes": clean, "exists": True,
                 "sha": hashlib.sha256(ident.encode()).hexdigest()[:12]})
    return base


def snapshot(selector: str | None = None) -> dict:
    """Observe the DOM. Returns {kind, text, nodes, sha, exists, driver}."""
    base = _snapshot_base()
    drv = driver_name()
    if drv == "none":
        fixture = os.environ.get("KIMIYA_DOM_FIXTURE")
        if not fixture:
            # No snapshot was taken; say so rather than invent one.
            return base
        path = Path(fixture)
        if not path.exists():
            raise DomError(f"dom fixture {fixture} does not exist")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise DomError(f"dom fixture {fixture} is not JSON: {e}") \
                from None
        return _finish_snapshot(base, str(data.get("text", "")),
                                data.get("nodes", []))
    if drv != "bridge":
        raise DomError(f"unknown dom driver {drv!r} "
                       "(known: bridge, none)")
    resp = _post({"op": "snapshot", "selector": selector})
    return _finish_snapshot(base, str(resp.get("text", "")),
                            resp.get("nodes", []))


def view(dest_dir) -> dict:
    """Observe the webview's pixels. Returns a screenshot-shaped record
    (kind "view") usable by `judge shows(...)` and `gen images=[...]`."""
    base = {"kind": "view", "path": "", "sha": "", "exists": False,
            "width": 0, "height": 0, "driver": driver_name()}
    drv = driver_name()
    if drv == "none":
        fixture = os.environ.get("KIMIYA_DOM_VIEW_FIXTURE")
        if not fixture:
            return base
        path = Path(fixture)
        if not path.exists():
            raise DomError(f"view fixture {fixture} does not exist")
        return _finish_view(base, path)
    if drv != "bridge":
        raise DomError(f"unknown dom driver {drv!r} "
                       "(known: bridge, none)")
    resp = _post({"op": "screenshot"})
    src = Path(str(resp.get("path", "")))
    if not src.exists():
        raise DomError(
            f"host reported a screenshot at {src} but no file is there")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "view.png"
    out.write_bytes(src.read_bytes())
    return _finish_view(base, out)


def _finish_view(rec: dict, path: Path) -> dict:
    from . import screen
    w, h = screen.png_size(path)
    rec.update({"path": str(path), "exists": True, "width": w, "height": h,
                "sha": hashlib.sha256(
                    path.read_bytes()).hexdigest()[:12]})
    return rec


def stable(timeout_ms: int = 8000) -> bool:
    """The readiness predicate for `settle until check dom_stable(ms)`."""
    drv = driver_name()
    if drv == "none":
        return True
    if drv != "bridge":
        raise DomError(f"unknown dom driver {drv!r} "
                       "(known: bridge, none)")
    resp = _post({"op": "stable", "timeout_ms": int(timeout_ms)},
                 timeout=max(10, int(timeout_ms) // 1000 + 10))
    return bool(resp.get("stable"))


# ---------------------------------------------------------------- locate

# `select` over a DOM snapshot: a model reads the candidate elements and
# picks the ones matching the description. An instrument, not an oracle —
# its factor enters θ under dom_locate:<purpose> at the datasheet's
# conservative end, exactly like the vision locate; its datasheet key is
# its own (a screen-locate measurement says nothing about DOM reading).
# The model-facing half is the shared retriever in kimiya/retrieval.py.

from . import retrieval as _retrieval  # noqa: E402

PAGE_TEXT_LIMIT = 2000


def locate_task(context: str | None) -> str:
    """Datasheet key for this instrument, mirroring `locate:<ctx>`."""
    return f"dom_locate:{context or 'unscoped'}"


class ReplayMiss(Exception):
    """Replay mode was asked for a locate that was never run live."""


class DomLocateCache:
    """Past dom locates, keyed by (task, description) — the dom world's
    twin of the vision LocateCache, with the same two grades:

      * exact  — the current snapshot's sha equals the cached one: the
                 same page, the same reading; free and silent.
      * replay — KIMIYA_REPLAY=1 / --replay: the page changed but the
                 cached nodes are reused anyway; disclosed in the
                 certificate. Sound only because a locate never carries
                 the verdict: a stale selector fails at the host
                 (matched: 0) or drives the live gates to refuse.

    Only non-empty results are cached — a miss may be transient.
    """

    def __init__(self, workspace):
        self.path = Path(workspace) / "dom_locates.json"
        self._d: dict = {}
        if self.path.exists():
            try:
                self._d = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._d = {}

    @staticmethod
    def key(task: str, description: str) -> str:
        return f"{task}|{description}"

    def get(self, task: str, description: str) -> dict | None:
        return self._d.get(self.key(task, description))

    def put(self, task: str, description: str, hits: list[dict],
            sha: str, agent_label: str):
        self._d[self.key(task, description)] = {
            "nodes": [dict(h) for h in hits], "sha": sha,
            "agent": agent_label, "ts": time.time()}
        self.path.write_text(json.dumps(self._d, indent=2))


def _candidate_line(x: dict) -> str:
    line = (f"selector={x['selector']!r} role={x['role']!r} "
            f"text={_retrieval.truncate(x['text'], 120)!r} "
            f"visible={x['visible']}")
    if x.get("href"):
        line += f" href={_retrieval.truncate(str(x['href']), 160)!r}"
    return line


def locate(oracle, agent, trace, snap: dict, description: str,
           purpose: str, context: str | None,
           cache: DomLocateCache | None = None,
           replay: bool = False) -> tuple[list[dict], str]:
    """Find nodes matching `description` in a DOM snapshot. Best first.

    Returns (hits, source) with source one of "live", "exact", "replay".
    """
    task = locate_task(context)
    ent = cache.get(task, description) if cache else None
    source = None
    if ent and ent.get("sha") and ent["sha"] == snap.get("sha"):
        source = "exact"
    elif replay:
        if ent is None:
            raise ReplayMiss(
                f"no cached dom locate for {description!r} — run live "
                "once before replaying")
        source = "replay"
    if source:
        assert ent is not None
        hits = [dict(n) for n in ent["nodes"]]
        trace.append({"kind": "dom_locate", "task": task, "cache": source,
                      "description": description[:200],
                      "snapshot_sha": snap.get("sha", ""),
                      "cached_sha": ent.get("sha", ""),
                      "agent": ent.get("agent", "cached"),
                      "hits": [x["selector"] for x in hits]})
        return hits, source

    nodes = snap.get("nodes") or []
    ranked = (sorted(nodes, key=lambda x: not x.get("visible", True))
              [:_retrieval.MAX_CANDIDATES])
    lines = [_candidate_line(x) for x in ranked]
    preamble = ("PAGE TEXT (head):\n"
                + str(snap.get("text", ""))[:PAGE_TEXT_LIMIT])
    picks, err = _retrieval.pick(oracle, agent, lines, description,
                                 purpose, preamble)
    hits = [dict(ranked[i]) for i in picks]
    if cache and hits and not err:
        cache.put(task, description, hits, snap.get("sha", ""),
                  agent.label())
    trace.append({"kind": "dom_locate", "task": task, "cache": "live",
                  "description": description[:200],
                  "snapshot_sha": snap.get("sha", ""),
                  "candidates": len(ranked),
                  "agent": agent.label(),
                  "hits": [x["selector"] for x in hits],
                  **({"error": err} if err else {})})
    return hits, "live"


# ---------------------------------------------------------------- doctor

def doctor_probe() -> tuple[bool, str]:
    """Reachability probe for `kimiya doctor` when KIMIYA_DOM=bridge."""
    try:
        _post({"op": "stable", "timeout_ms": 0}, timeout=5)
        return True, f"dom bridge at {bridge_host()} answers"
    except DomError as e:
        # An ok:false is still an answer — the endpoint is alive.
        if str(e).startswith("host refused"):
            return True, f"dom bridge at {bridge_host()} answers"
        return False, str(e)

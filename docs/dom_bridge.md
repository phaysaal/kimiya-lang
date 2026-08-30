# The dom bridge — wire protocol (v1, kimiya 1.9)

The `dom` world drives a **host application's webview** (e.g. a Tauri
window rendering a login-walled site) through an HTTP endpoint the host
provides per run. This document is the contract between the Kimiya
runtime (client) and the host (server). The SafeSelf/CounterSelf host
implements the server side; `tests/dom_bridge_stub.py` is a canned
reference used by the test suite.

## Security model

- **Kimiya never sends code.** The client emits only the structured ops
  below. The host MUST build the executed JavaScript from fixed
  templates, injecting `selector` / `text` / `url` values as
  JSON-encoded *data* (`document.querySelector(<json>)…`), never by
  string concatenation into code.
- The host SHOULD validate every op: `open` URLs restricted to `https`
  and an allow-listed host set; one bound target window per run (ops
  carry no window id); unknown ops refused.
- The bearer token is per-run, host-generated. Kimiya reads it from
  `KIMIYA_DOM_TOKEN` and never writes it to any trace, certificate, or
  error message.
- `emit` is the only data-out. The host accumulates emitted values as
  the run's result. On the Kimiya side emit is an irreversible act
  (K5-gated), and the audit trail records `channel + sha256 + length`
  only.

## Transport

One JSON object per op, POSTed to `KIMIYA_DOM_BRIDGE`
(`http://127.0.0.1:<port>/op`), with:

    Content-Type: application/json
    Authorization: Bearer <KIMIYA_DOM_TOKEN>

The client blocks for the response (ops are sequential, matching
Kimiya's act model). Every response is a JSON object with `"ok": true`
or `{"ok": false, "error": "<reason>"}`. Any `ok: false` (blocked URL,
stale selector, timeout) raises a runtime error in the program, which
its contract handles or which propagates to a visible ⚡ abstention.

## Ops

```jsonc
// navigation — act dom.open(url)
{ "op": "open", "url": "https://…" }                    → { "ok": true }

// actuation — act dom.click / dom.confirm (advisory flag) /
//             dom.scroll / dom.fill / dom.press
{ "op": "click",  "selector": "button[aria-label='Me']" }
{ "op": "click",  "selector": "#send", "irreversible": true }
{ "op": "scroll", "dx": 0, "dy": 4000 }
{ "op": "fill",   "selector": "input#q", "text": "…" }
{ "op": "press",  "selector": "input#q", "key": "Enter" }
// each → { "ok": true, "matched": 1 }
// A selector that matches nothing MUST return ok:false (or matched: 0,
// which the client treats identically): the snapshot it came from is
// stale, and the program must observe again rather than act blind.

// observation — observe dom(selector?)
{ "op": "snapshot", "selector": null }
  → { "ok": true, "text": "<innerText of selector or main/body>",
      "nodes": [ { "selector": "button[aria-label='Me']",
                   "role": "button", "text": "Me", "visible": true }, …
    ] }
// Node selectors SHOULD be stable and unique enough to re-resolve; the
// client hands them back verbatim in click/fill/press ops.

// observation — observe view()
{ "op": "screenshot" }
  → { "ok": true, "path": "/tmp/….png", "w": 1200, "h": 900 }
// The host writes a PNG of the webview window and returns its local
// path; the client copies it into the workspace and content-addresses
// it (sha256). Local-filesystem paths only — this protocol assumes the
// host and the Kimiya process share a machine.

// readiness — the dom_stable(ms) builtin, used by settle guards
{ "op": "stable", "timeout_ms": 8000 }
  → { "ok": true, "stable": true }
// stable means document.readyState === "complete" and layout quiescent
// within the timeout; return stable:false (still ok:true) on timeout.

// data-out — act dom.emit(channel, value)
{ "op": "emit", "channel": "linkedin:profile", "value": "…" }
  → { "ok": true }
```

Differences from the original SafeSelf spec draft: the `read` op is
dropped (`snapshot` already carries `text`, and
`observe dom("main").text` reads it); `matched: 0` is an error by
contract; `confirm` travels as `click` with an advisory `irreversible`
flag so the host whitelist stays minimal.

## Client-side environment

| Variable | Meaning |
|---|---|
| `KIMIYA_DOM` | `bridge` (deliver) or `none` (record only; default) |
| `KIMIYA_DOM_BRIDGE` | endpoint URL, host-provided per run |
| `KIMIYA_DOM_TOKEN` | bearer token, host-provided per run |
| `KIMIYA_DOM_FIXTURE` | `none` mode: `{text, nodes}` JSON standing in for `snapshot` |
| `KIMIYA_DOM_VIEW_FIXTURE` | `none` mode: PNG standing in for `screenshot` |
| `KIMIYA_TIMEOUT` | per-op HTTP timeout, seconds (default 60) |

The certificate's `dom` block records the driver, the endpoint host
(never the token), act/locate counts, and one `{channel, sha, len}`
entry per emit.

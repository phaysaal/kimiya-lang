"""Kernel-grade oracles for the GUI example.

In a real harness these read the app's SQLite DB or its public HTTP API —
non-visual ground truth, deterministic, so legitimately certainty-1.
Here they read JSON fixtures so the example runs offline.

Note what is NOT here: no clicking. Input effects go through
`act screen.…` so the checker can see them; smuggling them into a kernel
extension would hide them from K4/K5/K6 while the certificate went on
claiming certainty 1 for the whole path.
"""

import json
from pathlib import Path

_STATE = Path(__file__).with_name("app_state.json")


def _state():
    return json.loads(_STATE.read_text()) if _STATE.exists() else {}


def at(text, key):
    """One coordinate out of a locator file."""
    return json.loads(str(text)).get(key, 0)


def row_count(table, name):
    return sum(1 for r in _state().get(table, []) if r.get("name") == name)


def published(name):
    return any(r.get("name") == name and r.get("published")
               for r in _state().get("talks", []))


def status_text():
    return str(_state().get("status_banner", ""))

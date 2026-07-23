"""The Claude backends must be declared, sighted, and non-local.

Non-local is the load-bearing one: a `claude_cli` agent sends prompts
(and screenshots) to Anthropic through a subprocess rather than through
this process's socket. If `is_local` ever returned True for it, the
certificate would report `egress: none` for a program that shipped the
user's display off the machine.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from kimiya.runtime import Agent  # noqa: E402

for backend in ("claude_cli", "anthropic"):
    a = Agent(name="L", model="claude-opus-4-8", backend=backend)
    assert a.family == "anthropic", (backend, a.family)
    assert a.vision, backend
    assert not a.is_local, backend
    assert "api.anthropic.com" in a.host, (backend, a.host)

# Two Claude agents are one family: a panel of them cannot certify.
from kimiya.runtime import Pool  # noqa: E402

pool = Pool({n: Agent(name=n, model="claude-opus-4-8", backend="claude_cli")
             for n in ("L", "J1")})
_, certified = pool.panel_for(pool.agent("L"), 2, ["J1"])
assert not certified, "same-family Claude panel must not certify"

# --- paste: derived layers stay in sync with screen.ACTIONS ---
from kimiya import screen  # noqa: E402
from kimiya.checker import (KNOWN_ACTIONS, ACTION_ARITY,  # noqa: E402
                            DEFAULT_IRREVERSIBLE)

assert ("screen", "paste") in KNOWN_ACTIONS
assert ACTION_ARITY[("screen", "paste")] == 1
assert ("screen", "paste") not in DEFAULT_IRREVERSIBLE
assert screen.plan("paste", ["x"]) == [["key", "--clearmodifiers", "ctrl+v"]]
# none-mode records without touching any clipboard, and truncates trace text
import os  # noqa: E402
os.environ["KIMIYA_SCREEN"] = "none"
rec = screen.perform("paste", ["s" * 500])
assert rec["delivered"] is False
assert rec["args"][0].endswith("…") and len(rec["args"][0]) <= 201
del os.environ["KIMIYA_SCREEN"]

print("backend wiring ok")

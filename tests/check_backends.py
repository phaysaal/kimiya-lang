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

# --- dom: derived layers stay in sync with dom.ACTIONS ---
from kimiya import dom  # noqa: E402

for action, arity in dom.ACTIONS.items():
    assert ("dom", action) in KNOWN_ACTIONS, action
    assert ACTION_ARITY[("dom", action)] == arity, action
for action in dom.IRREVERSIBLE:
    assert ("dom", action) in DEFAULT_IRREVERSIBLE, action
assert ("dom", "click") not in DEFAULT_IRREVERSIBLE
# a confirm travels as a click op carrying the program's claim
assert dom.plan("confirm", ["#send"]) == \
    {"op": "click", "selector": "#send", "irreversible": True}
# none-mode records without delivering, and emit keeps sha+len, not value
os.environ["KIMIYA_DOM"] = "none"
rec = dom.perform("emit", ["chan", "topsecretvalue"])
assert rec["delivered"] is False
assert rec["channel"] == "chan" and rec["value_len"] == 14
assert "topsecretvalue" not in str(rec)
del os.environ["KIMIYA_DOM"]

# --- gen metering: one statement may cost several provider calls ---
# run_gen resamples on a schema miss. Billing the statement rather than
# the calls is the silent-cost bug the paper excludes by construction:
# "a generator that resamples until valid inside a nominal unit cost".
import tempfile  # noqa: E402
from kimiya.runtime import run_gen, Trace, MockOracle  # noqa: E402


class _NeverValid(MockOracle):
    def complete(self, *a, **k):
        return "not a json object at all"


_calls = []
_trace = Trace(pathlib.Path(tempfile.mkdtemp()))
_agent = Agent(name="A", model="llama3.1:8b")
assert run_gen(_NeverValid(), _trace, _agent, "p", ["x"], budget=3,
               meter=lambda: _calls.append(1)) is None
assert len(_calls) == 3, ("a 3-attempt schema miss must bill 3 calls",
                          len(_calls))
_calls.clear()
run_gen(MockOracle(), _trace, _agent, "p", ["x"],
        meter=lambda: _calls.append(1))
assert len(_calls) == 1, len(_calls)

print("backend wiring ok")

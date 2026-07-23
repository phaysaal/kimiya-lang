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

print("backend wiring ok")

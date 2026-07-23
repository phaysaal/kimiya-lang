"""Kernel-grade oracles for the collaboration scenario.

Non-visual ground truth: in a real harness these read the app's database
or its public HTTP API. Deterministic, so certainty-1 — which is exactly
why the scenario settles and gates on *these* rather than on a picture.
Reading pixels is the instrument; reading the row is the kernel.
"""

import json
from pathlib import Path

_STATE = Path(__file__).with_name("collab_state.json")


def _group(name):
    data = json.loads(_STATE.read_text()) if _STATE.exists() else {}
    return data.get(name, {})


def group_exists(name):
    return bool(_group(name))


def join_code(name):
    return str(_group(name).get("join_code", ""))


def member_count(name):
    return len(_group(name).get("members", []))


def message_count(name):
    return len(_group(name).get("messages", []))

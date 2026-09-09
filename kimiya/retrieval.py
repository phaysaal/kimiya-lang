"""Model-backed retrieval over a candidate list.

The one instrument behind two constructs:

    hits  := select<0.9>("the note about the deadline", notes) under k_ev by A
    picks := select<0.9>("the dismiss control", snap) under k_ui by L

A model is shown a purpose, a query, and a numbered list of candidates,
and answers with the numbers of the candidates that bear on the query.
It is an instrument, not an oracle: its factor enters θ under the task's
datasheet (`select:<ctx>` for a text store, `dom_locate:<ctx>` for a DOM
snapshot) at the conservative end — never at the recall the program
declared — and `kimiya calibrate` can label its trace records to tighten
that sheet.

Kimiya's original text `select` is a mechanical keyword filter and stays
available (write no `by`): recall-1 retrieval over an enumerable store
is a kernel operation, and a model should only be consulted when the
store is too large or too semantic for enumeration.
"""

from __future__ import annotations

import json
import re

PICK_SYSTEM = (
    "You are a RETRIEVAL instrument: given a purpose, a query and a "
    "numbered list of candidates, you pick the candidates that genuinely "
    "bear on the query. Return ONLY a JSON object "
    '{"picks": [<candidate numbers>]}, best match first. Return '
    '{"picks": []} if no candidate matches. No prose, no markdown fences.'
)

# Structured-output schema for backends that can enforce one.
PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["picks"],
    "additionalProperties": False,
}

# Candidate lists are bounded so a pathological store cannot become a
# megabyte of prompt.
MAX_CANDIDATES = 200
CANDIDATE_TEXT_LIMIT = 160


def truncate(text: str, limit: int = CANDIDATE_TEXT_LIMIT) -> str:
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def parse_picks(text: str, n: int) -> list[int]:
    """Salvage the pick list from a model reply — exact under structured
    outputs, tolerant of fences and bare arrays elsewhere."""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, flags=re.S) or \
        re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict):
        raw = raw.get("picks", [])
    if not isinstance(raw, list):
        return []
    picks: list[int] = []
    for v in raw:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in picks:
            picks.append(i)
    return picks


def pick(oracle, agent, lines: list[str], query: str, purpose: str,
         preamble: str = "") -> tuple[list[int], str | None]:
    """Ask `agent` which numbered candidates match `query`.

    Returns (indices best-first, error-or-None). `lines` are the
    candidates as the model sees them; the caller maps indices back to
    its own values.
    """
    body = "\n".join(f"{i}. {t}" for i, t in enumerate(lines))
    prompt = (f"PURPOSE: {purpose}\n\n"
              + (f"{preamble}\n\n" if preamble else "")
              + f"CANDIDATES:\n{body}\n\nQUERY: {query}")
    try:
        out = oracle.complete(agent, prompt, system=PICK_SYSTEM,
                              temperature=0.0, max_tokens=512,
                              schema=PICK_SCHEMA)
        err = None
    except (OSError, RuntimeError) as e:
        out, err = "", str(e)[:200]
    return parse_picks(out, len(lines)), err

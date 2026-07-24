"""Kernel-grade oracles for the counterfactual-search example.

Everything deterministic lives here, at certainty 1: parsing the case,
enumerating the intervention space, the minimal-change preference, and
the invariant that an intervention touches only mutable factors. The
models are left exactly the work only they can do — judging whether a
candidate intervention is a plausible route to the stated outcome.
"""

import itertools
import json


def parse_biz(text):
    return json.loads(str(text))


def interventions(biz, max_changes):
    """All single-factor changes; pairs across distinct factors when
    max_changes >= 2. Deterministic enumeration — the search *space* is
    kernel; only the search *judgment* is semantic."""
    singles = [{"factor": f, "from": spec["current"], "to": o}
               for f, spec in biz["mutable"].items()
               for o in spec["options"]]
    combos = [[s] for s in singles]
    if int(max_changes) >= 2:
        for a, b in itertools.combinations(singles, 2):
            if a["factor"] != b["factor"]:
                combos.append([a, b])
    return combos


def minimal(plausible):
    """Fewest changed factors wins; ties break to enumeration order.
    The principle of minimal intervention, decided at certainty 1."""
    return sorted(plausible, key=len)[0] if plausible else []


def touches_only_mutable(c, biz):
    return all(ch["factor"] in biz["mutable"] for ch in c)


def describe_change(c, biz):
    lines = "\n".join(f"- change {ch['factor']}: {ch['from']} -> {ch['to']}"
                      for ch in c)
    return (f"Proposed intervention:\n{lines}\n"
            f"Immutable constraints: {json.dumps(biz['immutable'])}\n"
            f"Current result: {biz['current_result']}\n"
            f"Expected result: {biz['expected_result']}")


# ---- helpers for the evolutionary variant (counterfactual_evolve.kim) ----

def summary(biz):
    """The case, compressed for a generation prompt."""
    muts = "; ".join(f"{f} (now: {s['current']})"
                     for f, s in biz["mutable"].items())
    return (f"Immutable: {json.dumps(biz['immutable'])}. "
            f"Mutable levers: {muts}. "
            f"Current result: {biz['current_result']}. "
            f"Expected result: {biz['expected_result']}.")


def mutable_names(biz):
    return ", ".join(biz["mutable"])


def is_mutable(factor, biz):
    """The invariant, checkable because proposals are schema-structured:
    free generation, kernel-gated."""
    return str(factor) in biz["mutable"]


def seed_intervention(biz):
    """Kernel baseline: the minimal enumerated single change. Evolution
    starts from a defensible answer and must beat it to replace it."""
    return describe_change(minimal(interventions(biz, 1)), biz)


def describe_prop(p, biz):
    """A model-invented proposal, rendered comparably to enumerated ones.
    Only called after is_mutable(p['factor']) — indexing is safe."""
    f = str(p["factor"])
    cur = biz["mutable"][f]["current"]
    return (f"Proposed intervention (novel):\n"
            f"- change {f}: {cur} -> {p['change']}\n"
            f"  rationale: {p['rationale']}\n"
            f"Immutable constraints: {json.dumps(biz['immutable'])}\n"
            f"Current result: {biz['current_result']}\n"
            f"Expected result: {biz['expected_result']}")

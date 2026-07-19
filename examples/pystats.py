"""Kernel-grade helpers for data-heavy Kimiya programs.

Loaded via:  use python "pystats.py"
Every public callable becomes a Kimiya kernel function; this file's SHA
is recorded in the certificate. Keep it deterministic and auditable.
"""

import statistics


def parse_nums(text):
    """All parseable numbers, one per non-empty line."""
    out = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(float(line))
        except ValueError:
            continue
    return out


def mean(xs):
    return statistics.mean(xs) if xs else 0.0


def stdev(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def p95(xs):
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(round(0.95 * (len(ys) - 1))))]

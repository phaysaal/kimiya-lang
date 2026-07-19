"""kimiya — interpreter for the Kimiya language (core + world extension).

Commands:
  check FILE       parse + static checks (the paper's disciplines)
  run FILE         check, then execute; prints the certificate
  hl FILE          ANSI-highlighted source ( --html writes FILE.html )
  doctor           local model check (ollama, families)
  calibrate DIR    label judgments from a .kimiya workspace
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from . import runtime
from .runtime import get_oracle, family_of, Datasheets, Trace
from .parser import ParseError
from .lexer import LexError
from .loader import load_program, LoadError
from .checker import check as static_check
from .interp import Interp
from . import highlight


def _load(path: str):
    try:
        return load_program(Path(path))
    except (ParseError, LexError) as e:
        sys.exit(f"syntax error: {e}")
    except LoadError as e:
        sys.exit(f"load error: {e}")


def _announce_py(py_exts):
    for ext in py_exts:
        if ext["kind"] == "file":
            print(f"⚠ python extension loaded: {ext['path']} "
                  f"(sha {ext['sha']}; functions: "
                  f"{', '.join(ext['functions'])}) — kernel-grade, audit "
                  "this file")
        else:
            print(f"⚠ python binding: {ext['name']} = {ext['target']}")


def cmd_check(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    rep = static_check(prog, frozenset(py_funcs))
    for w in rep.warnings:
        print(f"⚠ {w}")
    for e in rep.errors:
        print(f"✗ {e}")
    if rep.ok:
        n = len(prog.body)
        print(f"✓ {args.file}: {len(prog.decls)} declarations, "
              f"{n} top-level statements, all checks pass")
        return 0
    return 1


def cmd_run(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    rep = static_check(prog, frozenset(py_funcs))
    for w in rep.warnings:
        print(f"⚠ {w}")
    if not rep.ok:
        for e in rep.errors:
            print(f"✗ {e}")
        sys.exit("refusing to run an ill-formed program")
    models = args.models.split(",") if args.models else None
    interp = Interp(prog, Path(args.file), models,
                    py_funcs=py_funcs, py_exts=py_exts)
    cert = interp.run()
    print()
    print("── certificate ──────────────────────────────")
    print(f"  status : {cert['status']}"
          + (f"  ({cert['reason']})" if cert["reason"] else ""))
    if cert["status"] == "COMMITTED":
        val = json.dumps(cert["value"], ensure_ascii=False, default=str)
        print(f"  value  : {val[:200]}")
    print(f"  θ      : {cert['theta']}   "
          f"(factors: {cert['theta_factors']})")
    if cert["uncertified_judgments"]:
        print(f"  ⚠ {cert['uncertified_judgments']} judgment(s) ran without "
              "a cross-provenance panel: UNCERTIFIED")
    for task, s in cert["instruments"].items():
        tag = "calibrated" if s["calibrated"] else "prior-grade"
        print(f"  instrument {task}: α≤{s['alpha_hi']:.2f} "
              f"β≥{s['beta_lo']:.2f} [{tag}]")
    c = cert["cost"]
    print(f"  cost   : {c['gen_calls']} gen, {c['judge_votes']} votes, "
          f"{c['acts']} acts, {c['observes']} observes, {c['seconds']}s")
    print(f"  trace  : {cert['trace_records']} records "
          f"({Path(args.file).parent / '.kimiya' / 'trace.jsonl'})")
    print("─────────────────────────────────────────────")
    return 0 if cert["status"] == "COMMITTED" else 2


def cmd_hl(args):
    src = Path(args.file).read_text()
    if args.html:
        out = Path(args.file).with_suffix(".html")
        out.write_text(highlight.html_page(src, title=Path(args.file).name))
        print(f"wrote {out}")
    else:
        print(highlight.ansi(src), end="")
    return 0


def cmd_doctor(_args):
    oracle = get_oracle()
    try:
        models = oracle.models()
    except Exception as e:
        print(f"✗ ollama not reachable at {runtime.BASE_URL}: {e}")
        return 1
    fams: dict[str, list[str]] = {}
    for m in models:
        fams.setdefault(family_of(m), []).append(m)
    print(f"✓ ollama at {runtime.BASE_URL} (the interpreter's only "
          "network endpoint)")
    print(f"✓ models: {', '.join(models)}")
    print(f"{'✓' if len(fams) >= 2 else '✗'} families: {', '.join(fams)}")
    if len(fams) < 2:
        print("  → judgments will be uncertified until a second family "
              "is pulled")
    return 0


def cmd_calibrate(args):
    ws = Path(args.workspace)
    sheets = Datasheets(ws)
    trace = Trace(ws)
    judges = []
    if trace.path.exists():
        for line in trace.path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("kind") == "judge":
                    judges.append(rec)
    if not judges:
        print("no judge records in this workspace")
        return 1
    random.shuffle(judges)
    for i, rec in enumerate(judges[:args.n], 1):
        print(f"--- {i}  task={rec['task']}  "
              f"panel said {'YES' if rec['verdict'] else 'NO'} "
              f"({rec['votes']}/{rec['k']})")
        print(f"CLAIM: {rec.get('claim')}")
        ans = input("supported? [y/n/s] ").strip().lower()
        if ans in ("y", "n"):
            sheets.add_label(rec["task"], truth=(ans == "y"),
                             verdict=rec["verdict"])
    for task, s in sheets.recompute().items():
        print(f"  {task}: α≤{s['alpha_hi']:.3f} β≥{s['beta_lo']:.3f} "
              f"({'CALIBRATED' if s['calibrated'] else 'prior-grade'})")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="kimiya", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("check")
    cp.add_argument("file")
    rp = sub.add_parser("run")
    rp.add_argument("file")
    rp.add_argument("--models", help="comma-separated ollama models "
                    "(overrides pool declarations)")
    hp = sub.add_parser("hl")
    hp.add_argument("file")
    hp.add_argument("--html", action="store_true")
    sub.add_parser("doctor")
    lp = sub.add_parser("calibrate")
    lp.add_argument("workspace", help="a .kimiya directory")
    lp.add_argument("-n", type=int, default=20)
    args = p.parse_args(argv)
    return {"check": cmd_check, "run": cmd_run, "hl": cmd_hl,
            "doctor": cmd_doctor, "calibrate": cmd_calibrate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

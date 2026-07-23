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

from . import ast_nodes as A
from . import runtime
from . import screen
from .runtime import get_oracle, family_of, Datasheets, Trace
from .parser import ParseError
from .lexer import LexError
from .loader import load_program, LoadError
from .checker import check as static_check
from .types import typecheck
from .interp import Interp
from . import highlight


def _analyze(prog, py_funcs):
    """Run discipline check then type check; return (rep, tyrep)."""
    rep = static_check(prog, frozenset(py_funcs))
    tyrep = typecheck(prog)
    return rep, tyrep


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


def _screen_acts(prog):
    """Every `act screen.…` in the program, bodies and fns included."""
    from .checker import _substmts
    found = []

    def walk(stmts):
        for s in stmts:
            if isinstance(s, A.ActStmt) and s.surface == "screen":
                found.append(s)
            for sub in _substmts(s):
                walk(sub)

    walk(prog.body)
    for d in prog.decls:
        if isinstance(d, A.FnDecl):
            walk(d.body)
    return found


def _screen_observes(prog) -> bool:
    """Does the program capture the display anywhere?"""
    from .checker import _substmts
    found = [False]

    def walk_expr(e):
        if isinstance(e, A.ObserveExpr) and e.surface == "screen":
            found[0] = True

    def walk(stmts):
        for s in stmts:
            for attr in ("rhs", "expr", "iterable", "inv"):
                v = getattr(s, attr, None)
                if v is not None:
                    walk_expr(v)
                    walk_expr(getattr(v, "store", None))
            for sub in _substmts(s):
                walk(sub)

    walk(prog.body)
    return found[0]


def _announce_screen(prog):
    """GUI control is a world effect on the user's own machine; say so
    before it happens, the way remote egress is announced."""
    acts = _screen_acts(prog)
    if not acts:
        return
    risky = sum(1 for s in acts if s.action in screen.IRREVERSIBLE)
    drv = screen.driver_name()
    print(f"⚠ GUI control: this program synthesizes real input on your "
          f"display — {len(acts)} screen act(s)"
          + (f", {risky} irreversible" if risky else ""))
    if drv == "none":
        print("    driver: none (acts are recorded, nothing is delivered)")
    else:
        print(f"    driver: {drv} on display {screen.display()}   "
              "(KIMIYA_SCREEN=none records without delivering)")


def cmd_check(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    rep, tyrep = _analyze(prog, py_funcs)
    for w in rep.warnings + tyrep.warnings:
        print(f"⚠ {w}")
    for e in rep.errors + tyrep.errors:
        print(f"✗ {e}")
    if rep.ok and tyrep.ok:
        n = len(prog.body)
        print(f"✓ {args.file}: {len(prog.decls)} declarations, "
              f"{n} top-level statements, discipline + type checks pass")
        return 0
    return 1


def cmd_run(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    rep, tyrep = _analyze(prog, py_funcs)
    for w in rep.warnings + tyrep.warnings:
        print(f"⚠ {w}")
    if not (rep.ok and tyrep.ok):
        for e in rep.errors + tyrep.errors:
            print(f"✗ {e}")
        sys.exit("refusing to run an ill-formed program")
    models = args.models.split(",") if args.models else None
    interp = Interp(prog, Path(args.file), models,
                    py_funcs=py_funcs, py_exts=py_exts)
    remote = [a for a in interp.pool.agents if not a.is_local]
    if remote:
        print("⚠ network egress: this program sends prompts to remote "
              "agents —")
        for a in remote:
            print(f"    {a.name} → {a.model} @ {a.host} ({a.backend})")
        print("  (declared in source; your data leaves the machine for "
              "these)")
        # Worth its own line: a screenshot is whatever happened to be on
        # the display, which is a different disclosure from a prompt the
        # program composed.
        if _screen_observes(prog):
            print("  ⚠ this program also captures the screen — those "
                  "screenshots leave the machine for the agents above")
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
        if s.get("source"):
            tag = f"measured: {s['source']}"
        print(f"  instrument {task}: α≤{s['alpha_hi']:.2f} "
              f"β≥{s['beta_lo']:.2f} [{tag}]")
    if cert["egress"]:
        print(f"  egress : {', '.join(cert['egress'])} "
              "(prompts left the machine)")
    else:
        print("  egress : none (all agents local)")
    if cert.get("screen"):
        sc = cert["screen"]
        print(f"  screen : {sc['acts']} act(s) via {sc['driver']} "
              f"on {sc['target']}"
              + (f", {sc['locates']} locate(s)" if sc.get("locates") else ""))
    for note in cert.get("overclaims", []):
        print(f"  ⚠ {note}")
    c = cert["cost"]
    print(f"  cost   : {c['gen_calls']} gen, {c['judge_votes']} votes, "
          f"{c['acts']} acts, {c['observes']} observes, {c['seconds']}s")
    print(f"  trace  : {cert['trace_records']} records "
          f"({Path(args.file).parent / '.kimiya' / 'trace.jsonl'})")
    print("─────────────────────────────────────────────")
    return 0 if cert["status"] == "COMMITTED" else 2


def cmd_compile(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    rep, tyrep = _analyze(prog, py_funcs)
    for w in rep.warnings + tyrep.warnings:
        print(f"⚠ {w}")
    if not (rep.ok and tyrep.ok):
        for e in rep.errors + tyrep.errors:
            print(f"✗ {e}")
        sys.exit("refusing to compile an ill-formed program")
    from .compiler import compile_program
    code = compile_program(prog, py_exts, args.file)
    out = Path(args.out) if args.out else Path(args.file).with_suffix(".py")
    out.write_text(code)
    print(f"✓ compiled → {out}")
    print(f"  run it with:  python {out}"
          "   (or: python {out} model1,model2,...)")
    return 0


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
    print(f"✓ ollama at {runtime.BASE_URL} (default local backend)")
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


def cmd_datasheet(args):
    """Install an instrument measured outside this workspace.

    The file is a JSON object of task -> {alpha_hi, beta_lo, ...}, or a
    single sheet when --task names the instrument. Provenance is
    mandatory: a sheet with no source is a number with no history, and
    the certificate would present it as if it had been earned here.
    """
    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    sheets = Datasheets(ws)
    data = json.loads(Path(args.file).read_text())
    incoming = {args.task: data} if args.task else data
    installed = []
    for task, sheet in incoming.items():
        if not isinstance(sheet, dict) or "beta_lo" not in sheet:
            print(f"✗ {task}: not a datasheet (needs at least beta_lo)")
            return 1
        merged = {"alpha_hi": float(sheet.get("alpha_hi", 1.0)),
                  "beta_lo": float(sheet["beta_lo"]),
                  "n_true": int(sheet.get("n_true", 0)),
                  "n_false": int(sheet.get("n_false", 0)),
                  "calibrated": True,
                  "source": args.source or sheet.get("source") or ""}
        if not merged["source"]:
            print(f"✗ {task}: refusing to install a sheet with no source — "
                  "pass --source \"<how it was measured>\"")
            return 1
        if not (0 <= merged["beta_lo"] <= 1 and 0 <= merged["alpha_hi"] <= 1):
            print(f"✗ {task}: α and β must lie in [0, 1]")
            return 1
        sheets.install(task, merged)
        installed.append((task, merged))
    for task, s in installed:
        print(f"✓ {task}: α≤{s['alpha_hi']:.3f} β≥{s['beta_lo']:.3f} "
              f"[imported: {s['source']}]")
    print(f"  written to {sheets.local_path}")
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
    kp = sub.add_parser("compile")
    kp.add_argument("file")
    kp.add_argument("--out", help="output .py path (default: FILE.py)")
    hp = sub.add_parser("hl")
    hp.add_argument("file")
    hp.add_argument("--html", action="store_true")
    sub.add_parser("doctor")
    lp = sub.add_parser("calibrate")
    lp.add_argument("workspace", help="a .kimiya directory")
    lp.add_argument("-n", type=int, default=20)
    dp = sub.add_parser("datasheet",
                        help="install an externally measured instrument")
    dp.add_argument("file", help="JSON: task -> {alpha_hi, beta_lo, ...}")
    dp.add_argument("workspace", help="a .kimiya directory")
    dp.add_argument("--task", help="install FILE as this single instrument, "
                    "e.g. locate:k_ui")
    dp.add_argument("--source", help="how it was measured (required)")
    args = p.parse_args(argv)
    return {"check": cmd_check, "run": cmd_run, "compile": cmd_compile,
            "hl": cmd_hl, "doctor": cmd_doctor,
            "calibrate": cmd_calibrate,
            "datasheet": cmd_datasheet}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

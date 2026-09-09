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
from . import dom
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


def _dom_acts(prog):
    """Every `act dom.…` in the program, bodies and fns included."""
    from .checker import _substmts
    found = []

    def walk(stmts):
        for s in stmts:
            if isinstance(s, A.ActStmt) and s.surface == "dom":
                found.append(s)
            for sub in _substmts(s):
                walk(sub)

    walk(prog.body)
    for d in prog.decls:
        if isinstance(d, A.FnDecl):
            walk(d.body)
    return found


def _dom_observes(prog) -> bool:
    """Does the program read the webview (DOM or pixels) anywhere?"""
    from .checker import _substmts
    found = [False]

    def walk_expr(e):
        if isinstance(e, A.ObserveExpr) and e.surface in ("dom", "view"):
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


def _has_multimodal_gen(prog) -> bool:
    """Does any reachable source body declare `gen(..., images=...)`?"""
    from .checker import _substmts
    found = [False]

    def walk(stmts):
        for stmt in stmts:
            rhs = getattr(stmt, "rhs", None)
            if isinstance(rhs, A.GenExpr) and rhs.images is not None:
                found[0] = True
            for sub in _substmts(stmt):
                walk(sub)

    walk(prog.body)
    for decl in prog.decls:
        if isinstance(decl, A.FnDecl):
            walk(decl.body)
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
    for d in prog.decls:
        if isinstance(d, A.DisplayDecl):
            f = d.fields
            if f.get("ssh"):
                print(f"    actor {d.name}: {f['ssh']} "
                      f"{f.get('x11', ':0 (ambient)')} — input and "
                      "screenshots travel over ssh to that machine")
            else:
                where = f.get("x11") or "the ambient DISPLAY"
                extra = f" (monitor {f['monitor']})" if f.get("monitor") \
                    else ""
                print(f"    actor {d.name}: {where}{extra}")


def _announce_dom(prog):
    """The dom bridge is a control channel into a host application's
    webview and `dom.emit` moves data out through it; both are said
    before anything runs, the way GUI control and egress are."""
    acts = _dom_acts(prog)
    if not (acts or _dom_observes(prog)):
        return
    risky = sum(1 for s in acts if s.action in dom.IRREVERSIBLE)
    emits = sum(1 for s in acts if s.action == "emit")
    drv = dom.driver_name()
    if acts:
        print(f"⚠ webview control: this program drives a host "
              f"application's webview through the dom bridge — "
              f"{len(acts)} dom act(s)"
              + (f", {risky} irreversible" if risky else ""))
    else:
        print("⚠ webview access: this program reads a host application's "
              "webview through the dom bridge (observations only, no "
              "acts)")
    if emits:
        print(f"    {emits} emit(s): values leave the program for the "
              "host only through gated dom.emit; the certificate keeps "
              "sha + length, never the value")
    if drv == "none":
        print("    driver: none (ops are recorded, nothing is delivered)")
    else:
        print(f"    driver: {drv} → {dom.bridge_host() or 'unset'}   "
              "(KIMIYA_DOM=none records without delivering)")


def cmd_check(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    _announce_dom(prog)
    rep, tyrep = _analyze(prog, py_funcs)
    for w in rep.warnings + tyrep.warnings:
        print(f"⚠ {w}")
    for e in rep.errors + tyrep.errors:
        print(f"✗ {e}")
    if rep.ok and tyrep.ok:
        n = len(prog.body)
        print(f"✓ {args.file}: {len(prog.decls)} declarations, "
              f"{n} top-level statements, discipline + type checks pass")
        pdecls = [d for d in prog.decls if isinstance(d, A.ParamDecl)]
        if pdecls:
            bits = [f"{d.name} ({d.type}"
                    + ("" if d.required else f" = {d.default!r}") + ")"
                    + (" required" if d.required else "")
                    for d in pdecls]
            print("  params: " + "; ".join(bits))
        return 0
    return 1


def cmd_run(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    _announce_dom(prog)
    rep, tyrep = _analyze(prog, py_funcs)
    for w in rep.warnings + tyrep.warnings:
        print(f"⚠ {w}")
    if not (rep.ok and tyrep.ok):
        for e in rep.errors + tyrep.errors:
            print(f"✗ {e}")
        sys.exit("refusing to run an ill-formed program")
    models = args.models.split(",") if args.models else None
    pairs = {}
    for a in getattr(args, "params", []) or []:
        if "=" not in a:
            sys.exit(f"bad parameter {a!r} — expected name=value")
        k, v = a.split("=", 1)
        pairs[k] = v
    try:
        interp = Interp(prog, Path(args.file), models,
                        py_funcs=py_funcs, py_exts=py_exts,
                        replay=getattr(args, "replay", False),
                        params=pairs)
    except ValueError as e:
        sys.exit(f"refusing to run: {e}")
    if interp.replay:
        print("▶ replay: cached locates will be reused without a model "
              "call; judges and kernel gates still run live")
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
        if _has_multimodal_gen(prog):
            print("  ⚠ image egress: this program contains multimodal "
                  "generation — observed image pixels leave the machine "
                  "when routed to a remote generator")
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
        if s.get("template_mismatch"):
            tag = "prior-grade — installed sheet did not transfer"
        print(f"  instrument {task}: α≤{s['alpha_hi']:.2f} "
              f"β≥{s['beta_lo']:.2f} [{tag}]"
              + (f" · template {s['template_sha']}"
                 if s.get("template_sha") else ""))
    for sha, tpl in cert.get("prompt_templates", {}).items():
        short = tpl.replace("\n", "⏎")
        if len(short) > 60:
            short = short[:60] + "…"
        print(f'  template {sha} : "{short}"')
    if cert["egress"]:
        print(f"  egress : {', '.join(cert['egress'])} "
              "(prompts left the machine)")
    else:
        print("  egress : none (all agents local)")
    if cert.get("image_egress"):
        disclosures = cert["image_egress"]
        print(f"  image egress : {len(disclosures)} observed image "
              "disclosure(s)")
    elif cert.get("image_observations"):
        print("  image egress : none (observed pixels stayed local)")
    if cert.get("memo_hits"):
        print(f"  memo   : {cert['memo_hits']} reuse(s) — identical "
              "readings, factors counted once")
    if cert.get("explored"):
        print(f"  ⚑ explored : {cert['explored']} judged/select factor(s) "
              "inside explore — trace-recorded, excluded from θ; the "
              "verdict rests only on the gates outside")
    if cert.get("params"):
        shown = ", ".join(f"{k}={v!r}" for k, v in cert["params"].items())
        print(f"  params : {shown}")
    if cert.get("screen"):
        sc = cert["screen"]
        line = (f"  screen : {sc['acts']} act(s) via {sc['driver']} "
                f"on {sc['target']}")
        if sc.get("locates"):
            line += f", {sc['locates']} locate(s)"
            extras = []
            if sc.get("locates_cached"):
                extras.append(f"{sc['locates_cached']} exact-cache")
            if sc.get("locates_replayed"):
                extras.append(f"{sc['locates_replayed']} replayed")
            if extras:
                line += f" ({', '.join(extras)})"
        print(line)
        for aname, ad in (sc.get("actors") or {}).items():
            kind = "ssh" if ad["ssh"] else "local"
            print(f"  actor  : {aname} → {ad['label']} ({kind})")
        if sc.get("locates_replayed"):
            print(f"  ⚠ {sc['locates_replayed']} locate(s) replayed from a "
                  "prior run against changed pixels — layout stability is "
                  "assumed, not measured; the verdict gates (checks, "
                  "judges) still ran live")
    if cert.get("dom"):
        dm = cert["dom"]
        line = f"  dom    : {dm['acts']} act(s) via {dm['driver']}"
        if dm.get("bridge"):
            line += f" @ {dm['bridge']}"
        if dm.get("locates"):
            line += f", {dm['locates']} locate(s)"
            extras = []
            if dm.get("locates_cached"):
                extras.append(f"{dm['locates_cached']} exact-cache")
            if dm.get("locates_replayed"):
                extras.append(f"{dm['locates_replayed']} replayed")
            if extras:
                line += f" ({', '.join(extras)})"
        print(line)
        if dm.get("locates_replayed"):
            print(f"  ⚠ {dm['locates_replayed']} dom locate(s) replayed from "
                  "a prior run against a changed page — layout stability "
                  "is assumed, not measured; the verdict gates still ran "
                  "live")
        for em in dm.get("emits", []):
            print(f"  emit   : {em['channel']} sha {em['sha']} "
                  f"({em['len']} chars) — value not in certificate")
    for note in cert.get("overclaims", []):
        print(f"  ⚠ {note}")
    c = cert["cost"]
    print(f"  cost   : {c['gen_calls']} gen, {c['judge_votes']} votes, "
          f"{c['acts']} acts, {c['observes']} observes, {c['seconds']}s")
    print(f"  kimiya : v{cert.get('kimiya_version', '?')}")
    print(f"  trace  : {cert['trace_records']} records "
          f"({Path(args.file).parent / '.kimiya' / 'trace.jsonl'})")
    print("─────────────────────────────────────────────")
    return 0 if cert["status"] == "COMMITTED" else 2


def cmd_compile(args):
    prog, py_funcs, py_exts = _load(args.file)
    _announce_py(py_exts)
    _announce_screen(prog)
    _announce_dom(prog)
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
    print(f"  run it with:  python {out} [name=value ...] "
          "[--models m1,m2]")
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
    if dom.driver_name() == "bridge":
        ok, msg = dom.doctor_probe()
        print(f"{'✓' if ok else '✗'} {msg}")
    return 0


def cmd_calibrate(args):
    ws = Path(args.workspace)
    sheets = Datasheets(ws)
    trace = Trace(ws)
    records = []
    if trace.path.exists():
        for line in trace.path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("kind") == "judge":
                    records.append(rec)
                elif rec.get("kind") == "dom_locate" and \
                        rec.get("cache", "live") == "live":
                    records.append(rec)
                elif rec.get("kind") == "select" and rec.get("by"):
                    records.append(rec)
    if not records:
        print("no judge or model-retrieval records in this workspace")
        return 1
    random.shuffle(records)
    for i, rec in enumerate(records[:args.n], 1):
        if rec["kind"] == "judge":
            print(f"--- {i}  task={rec['task']}  "
                  f"panel said {'YES' if rec['verdict'] else 'NO'} "
                  f"({rec['votes']}/{rec['k']})")
            print(f"CLAIM: {rec.get('claim')}")
            ans = input("supported? [y/n/s] ").strip().lower()
            if ans in ("y", "n"):
                sheets.add_label(rec["task"], truth=(ans == "y"),
                                 verdict=rec["verdict"])
            continue
        # A retrieval reading: recall is P(retrieved | relevant existed),
        # so the label says whether something relevant existed and
        # whether the instrument surfaced it.
        query = rec.get("query") or rec.get("description") or ""
        picked = rec.get("picked") or rec.get("hits") or []
        print(f"--- {i}  task={rec['task']}  retrieval by "
              f"{rec.get('by') or rec.get('agent')}")
        print(f"QUERY: {query}")
        print("RETURNED: " + (" | ".join(str(x) for x in picked)
                              if picked else "(nothing)"))
        ans = input("y = found what mattered · n = missed something "
                    "relevant · e = nothing relevant existed · s = skip "
                    "[y/n/e/s] ").strip().lower()
        if ans == "y":
            sheets.add_label(rec["task"], truth=True, verdict=True)
        elif ans == "n":
            sheets.add_label(rec["task"], truth=True, verdict=False)
        elif ans == "e":
            sheets.add_label(rec["task"], truth=False,
                             verdict=bool(picked))
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
        # Template identity: a read sheet measured under one prompt
        # template does not price a reading made under another. Carry
        # the campaign's template hash so the runtime can check.
        if sheet.get("template_sha"):
            merged["template_sha"] = str(sheet["template_sha"])
            merged["template"] = str(sheet.get("template", ""))[:400]
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
              f"[imported: {s['source']}]"
              + (f" — bound to prompt template {s['template_sha']}"
                 if s.get("template_sha") else ""))
    print(f"  written to {sheets.local_path}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="kimiya", description=__doc__)
    from . import __version__
    p.add_argument("--version", action="version",
                   version=f"kimiya {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("check")
    cp.add_argument("file")
    rp = sub.add_parser("run")
    rp.add_argument("file")
    rp.add_argument("--models", help="comma-separated ollama models "
                    "(overrides pool declarations)")
    rp.add_argument("params", nargs="*", metavar="name=value",
                    help="program parameters (declared with `param`)")
    rp.add_argument("--replay", action="store_true",
                    help="reuse cached locates even though the screen has "
                    "changed (zero locate model calls; judges still run "
                    "live). Compiled artifacts: KIMIYA_REPLAY=1")
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

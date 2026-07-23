"""Tree-walking evaluator for Kimiya (core + world extension).

Semantics notes (v0.1, honest simplifications documented in README):
- retry has snapshot semantics over PROGRAM state (env restored each
  round); world effects in a body are legal only with inv/compensate
  (checker K6), and compensate runs between failed rounds.
- Budget exhaustion and abstain raise Bolt (the paper's ⚡): explicit,
  observable, never silent. An uncaught Bolt ends the run as ABSTAIN.
- A failed check statement is a kernel refutation: it aborts to ⚡ with
  the failing expression recorded.
- commit prints and writes a certificate: the product of the executed
  path's judged/select factors (conservative datasheet ends), the cost
  meter, and the calibration status of every instrument used.
- Freshness (world extension): the interpreter tracks last act and last
  observation per file path and warns into the trace when an act touches
  a path whose most recent observation predates the path's last act.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import ast_nodes as A
from . import screen
from .runtime import (Pool, Agent, Trace, Datasheets, get_oracle, run_judge,
                      run_gen)


class Bolt(Exception):
    """The ⚡ outcome: abstention / budget exhaustion / kernel refutation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ReturnSignal(Exception):
    def __init__(self, value):
        super().__init__("return")
        self.value = value


class FuncValue:
    """A Kimiya function as a first-class value."""

    def __init__(self, decl, interp):
        self.decl = decl
        self.interp = interp

    def __call__(self, *args):
        return self.interp.call_fn(self.decl, list(args))

    def __repr__(self):
        return f"<fn {self.decl.name}>"


class KimiyaRuntimeError(RuntimeError):
    pass


class Interp:
    def __init__(self, prog: A.Program, program_path: Path,
                 models_override: list[str] | None = None,
                 py_funcs: dict | None = None,
                 py_exts: list | None = None):
        self.prog = prog
        self.workspace = program_path.parent / ".kimiya"
        self.workspace.mkdir(exist_ok=True)
        self.trace = Trace(self.workspace)
        self.sheets = Datasheets(self.workspace)
        self.oracle = get_oracle()
        self.env: dict[str, object] = {}
        self.contexts = {d.name: d for d in prog.decls
                         if isinstance(d, A.ContextDecl)}
        self.schemas = {d.name: d for d in prog.decls
                        if isinstance(d, A.SchemaDecl)}
        self.fns = {d.name: d for d in prog.decls
                    if isinstance(d, A.FnDecl)}
        self.py_funcs = py_funcs or {}
        self.py_exts = py_exts or []
        bindings: dict[str, Agent] = {}
        for d in prog.decls:
            if isinstance(d, A.PoolDecl):
                bindings[d.name] = Agent(name=d.name, model=d.model)
            elif isinstance(d, A.AgentDecl):
                f = d.fields
                bindings[d.name] = Agent(
                    name=d.name, model=f.get("model", ""),
                    backend=f.get("backend", "ollama"),
                    url=f.get("url"), key_env=f.get("key_env"),
                    family_override=f.get("family"))
        if models_override:
            bindings = {f"M{i}": Agent(name=f"M{i}", model=m)
                        for i, m in enumerate(models_override)}
        self.pool = Pool(bindings)
        self.theta: list[tuple[str, float]] = []
        self.cost = {"gen_calls": 0, "judge_votes": 0, "acts": 0,
                     "observes": 0}
        self.uncertified = 0
        self.last_gen_model: Agent | None = None
        self.last_act: dict[str, float] = {}
        self.last_obs: dict[str, float] = {}
        self.screen_acts = 0
        self.committed = None

    # ------------------------------------------------ purpose text
    def purpose_text(self, name: str) -> str:
        c = self.contexts.get(name)
        if not c:
            return name
        return (f"{c.domain}; preserve: {', '.join(c.preserve)}; "
                f"may lose: {', '.join(c.allow_loss)}")

    # ------------------------------------------------ run
    def run(self) -> dict:
        t0 = time.time()
        try:
            self.exec_stmts(self.prog.body)
            status = "COMMITTED" if self.committed is not None else "ENDED"
            reason = ""
        except Bolt as b:
            status, reason = "ABSTAINED", b.reason
        cert = self.certificate(status, reason, time.time() - t0)
        (self.workspace / "certificate.json").write_text(
            json.dumps(cert, indent=2, ensure_ascii=False, default=str))
        return cert

    def certificate(self, status, reason, secs) -> dict:
        theta = 1.0
        for _, f in self.theta:
            theta *= f
        tasks = sorted({name for name, _ in self.theta
                        if not name.startswith("select")})
        return {
            "status": status,
            "reason": reason,
            "value": self.committed,
            "theta": round(theta, 4),
            "theta_factors": [(n, round(f, 4)) for n, f in self.theta],
            "uncertified_judgments": self.uncertified,
            "instruments": {t: self.sheets.get(t) for t in tasks},
            "python_extensions": self.py_exts,
            "agents": [{"name": a.name, "model": a.model,
                        "backend": a.backend, "host": a.host,
                        "family": a.family, "local": a.is_local}
                       for a in self.pool.agents],
            "egress": sorted({a.host for a in self.pool.agents
                              if not a.is_local}),
            "screen": ({"driver": screen.driver_name(),
                        "target": screen.target(),
                        "acts": self.screen_acts}
                       if self.screen_acts else None),
            "cost": dict(self.cost, seconds=round(secs, 1)),
            "trace_records": self.trace.count(),
        }

    # ------------------------------------------------ statements
    def exec_stmts(self, stmts):
        for s in stmts:
            self.exec_stmt(s)

    def exec_stmt(self, s):
        if isinstance(s, A.Assign):
            self.env[s.name] = self.eval_rhs(s.rhs)
        elif isinstance(s, A.CheckStmt):
            v = self.eval(s.expr)
            self.trace.append({"kind": "check", "line": s.line,
                               "ok": bool(v)})
            if not v:
                raise Bolt(f"check failed at line {s.line}")
        elif isinstance(s, A.PrintStmt):
            print(self.to_str(self.eval(s.expr)))
        elif isinstance(s, A.CommitStmt):
            self.committed = self.eval(s.expr)
            self.trace.append({"kind": "commit", "line": s.line})
        elif isinstance(s, A.AbstainStmt):
            raise Bolt(f"abstain at line {s.line}")
        elif isinstance(s, A.ReturnStmt):
            raise ReturnSignal(self.eval(s.expr)
                               if s.expr is not None else None)
        elif isinstance(s, A.IfStmt):
            if self.eval_guard(s.guard):
                self.exec_stmts(s.then)
            elif s.els:
                self.exec_stmts(s.els)
        elif isinstance(s, A.ForallStmt):
            for item in self.as_list(self.eval(s.iterable), s.line):
                self.env[s.var] = item
                self.exec_stmts(s.body)
        elif isinstance(s, A.RetryStmt):
            self.exec_retry(s)
        elif isinstance(s, A.ActStmt):
            self.exec_act(s)
        elif isinstance(s, A.SettleStmt):
            self.exec_settle(s)
        else:
            raise KimiyaRuntimeError(f"unknown statement {s}")

    def eval_rhs(self, rhs):
        if isinstance(rhs, A.GenExpr):
            return self.eval_gen(rhs)
        if isinstance(rhs, A.SelectExpr):
            return self.eval_select(rhs)
        if isinstance(rhs, A.RetryStmt):
            return self.exec_retry(rhs)
        return self.eval(rhs)

    # ------------------------------------------------ gen / select
    def eval_gen(self, g: A.GenExpr):
        prompt = self.to_str(self.eval(g.prompt))
        agent = (self.pool.agent(g.by) if g.by
                 else self.pool.default_generator())
        self.last_gen_model = agent
        fields = None
        if g.schema == "Text":
            fields = None
        elif g.schema == "Json":
            fields = []
        else:
            fields = [f for f, _ in self.schemas[g.schema].fields]
        self.cost["gen_calls"] += 1
        if fields == []:
            out = run_gen(self.oracle, self.trace, agent,
                          prompt + "\n\nFIELDS: result", ["result"])
            return out
        return run_gen(self.oracle, self.trace, agent, prompt, fields)

    def eval_select(self, sel: A.SelectExpr):
        query = self.to_str(self.eval(sel.query)).lower()
        store = self.as_list(self.eval(sel.store), sel.line)
        words = {w for w in query.split() if len(w) > 3}

        def score(item):
            t = self.to_str(item).lower()
            return sum(1 for w in words if w in t)

        hits = [x for x in sorted(store, key=lambda x: -score(x))
                if score(x) > 0] or list(store)
        self.theta.append((f"select<{sel.recall}>", sel.recall))
        self.trace.append({"kind": "select", "recall": sel.recall,
                           "store_size": len(store), "hits": len(hits),
                           "line": sel.line})
        return hits

    # ------------------------------------------------ guards
    def eval_guard(self, g) -> bool:
        if isinstance(g, A.CheckGuard):
            v = bool(self.eval(g.expr))
            self.trace.append({"kind": "check_guard", "ok": v,
                               "line": g.line})
            return v
        assert isinstance(g, A.JudgeGuard)
        left = self.to_str(self.eval(g.left))
        right = self.to_str(self.eval(g.right)) if g.right is not None else ""
        purpose = self.purpose_text(g.context)
        task = f"{g.relation}:{g.context}"
        if g.relation == "entails":
            evidence, claim = left, f"The evidence supports: {right}"
        elif g.relation == "equiv":
            evidence = f"A: {left}\nB: {right}"
            claim = ("A and B are interchangeable for the stated purpose")
        elif g.relation == "contradicts":
            evidence = f"A: {left}\nB: {right}"
            claim = "A and B are in direct contradiction"
        else:
            evidence, claim = left, f"The value satisfies: {purpose}"
        generator = self.last_gen_model or self.pool.default_generator()
        j = run_judge(self.pool, self.oracle, self.trace, self.sheets,
                      task, claim, evidence, generator, g.k, g.tau,
                      purpose, g.panel, g.paraphrases)
        self.cost["judge_votes"] += g.k
        if not j.certified:
            self.uncertified += 1
        sheet = self.sheets.get(task)
        if j.verdict:
            self.theta.append((task, sheet["beta_lo"]))
        else:
            self.theta.append((f"neg:{task}", 1 - sheet["alpha_hi"]))
        return j.verdict

    # ------------------------------------------------ retry
    def exec_retry(self, s: A.RetryStmt):
        last_name = None
        for st in s.body:
            if isinstance(st, A.Assign):
                last_name = st.name
        for round_no in range(s.budget):
            snapshot = dict(self.env)
            theta_mark = len(self.theta)
            try:
                self.exec_stmts(s.body)
                if self.eval_guard(s.guard):
                    self.trace.append({"kind": "retry", "line": s.line,
                                       "round": round_no, "ok": True})
                    return (self.env.get(last_name)
                            if last_name is not None else None)
            except Bolt:
                pass  # a failed round is priced, not fatal
            # failed round: restore program state (snapshot semantics)
            del self.theta[theta_mark:]
            self.env = snapshot
            if s.inv is not None and not self.eval(s.inv):
                if s.compensate:
                    self.exec_stmts(s.compensate)
                if not self.eval(s.inv):
                    raise Bolt(f"world invariant broken at line {s.line} "
                               "and compensation did not restore it")
            elif s.compensate and self.body_acted(s.body):
                self.exec_stmts(s.compensate)
        self.trace.append({"kind": "retry", "line": s.line,
                           "exhausted": True})
        raise Bolt(f"retry budget exhausted at line {s.line}")

    @staticmethod
    def body_acted(stmts) -> bool:
        from .checker import _substmts
        for st in stmts:
            if isinstance(st, A.ActStmt):
                return True
            if any(Interp.body_acted(sub) for sub in _substmts(st)):
                return True
        return False

    # ------------------------------------------------ world
    def exec_act(self, s: A.ActStmt):
        args = [self.eval(a) for a in s.args]
        self.cost["acts"] += 1
        if s.surface == "screen":
            return self.act_screen(s, args)
        if s.surface != "file":
            raise KimiyaRuntimeError(f"unknown surface {s.surface}")
        return self.act_file(s, args)

    def act_screen(self, s: A.ActStmt, args: list):
        self.check_freshness(screen.target(), s.line)
        try:
            rec = screen.perform(s.action, args)
        except screen.ScreenError as e:
            raise KimiyaRuntimeError(f"line {s.line}: {e}") from None
        self.last_act[screen.target()] = time.time()
        self.screen_acts += 1
        self.trace.append({"kind": "act", "surface": "screen",
                           "action": s.action, "target": screen.target(),
                           "line": s.line, **rec})

    def act_file(self, s: A.ActStmt, args: list):
        if not args:
            raise KimiyaRuntimeError(
                f"line {s.line}: act needs a path argument")
        path = Path(str(args[0]))
        self.check_freshness(str(path), s.line)
        if s.action == "create":
            if path.exists():
                raise Bolt(f"file.create: {path} already exists "
                           f"(line {s.line})")
            path.write_text(str(args[1]))
        elif s.action == "append":
            with path.open("a") as f:
                f.write(str(args[1]))
        elif s.action == "overwrite":
            path.write_text(str(args[1]))
        elif s.action == "delete":
            path.unlink(missing_ok=True)
        elif s.action == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        else:
            raise KimiyaRuntimeError(f"unknown action file.{s.action}")
        self.last_act[str(path)] = time.time()
        self.trace.append({"kind": "act", "surface": s.surface,
                           "action": s.action, "path": str(path),
                           "line": s.line})

    def check_freshness(self, key: str, line: int):
        """Warn when a program acts on a world it observed before its own
        last act on that same world (a stale read)."""
        if key in self.last_obs and \
                self.last_obs[key] < self.last_act.get(key, -1):
            self.trace.append({"kind": "freshness_warning",
                               "path": key, "line": line})

    def exec_settle(self, s: A.SettleStmt):
        deadline = time.time() + s.within
        while time.time() < deadline:
            if self.eval_guard(s.guard):
                return
            time.sleep(min(1.0, s.within / 10))
        raise Bolt(f"settle deadline ({s.within}s) elapsed at line {s.line}")

    # ------------------------------------------------ expressions
    def call_fn(self, decl, args):
        """Call a Kimiya fn: fresh scope of params only (no globals)."""
        if len(args) != len(decl.params):
            raise KimiyaRuntimeError(
                f"'{decl.name}' takes {len(decl.params)} argument(s), "
                f"got {len(args)}")
        saved = self.env
        self.env = dict(zip(decl.params, args))
        try:
            self.exec_stmts(decl.body)
            return None
        except ReturnSignal as ret:
            return ret.value
        finally:
            self.env = saved

    def resolve_callable(self, name: str):
        v = self.env.get(name)
        if isinstance(v, FuncValue) or callable(v):
            return v
        if name in self.fns:
            return FuncValue(self.fns[name], self)
        if name in self.py_funcs:
            return self.py_funcs[name]
        return None

    def eval(self, e):
        if isinstance(e, A.Lit):
            return e.value
        if isinstance(e, A.Var):
            if e.name in self.env:
                return self.env[e.name]
            f = self.resolve_callable(e.name)
            if f is not None:
                return f
            if e.name in _BUILTIN_VALUES:
                return _BUILTIN_VALUES[e.name]
            raise KimiyaRuntimeError(
                f"line {e.line}: undefined variable '{e.name}'")
        if isinstance(e, A.ListExpr):
            return [self.eval(x) for x in e.items]
        if isinstance(e, A.RecordExpr):
            return {k: self.eval(v) for k, v in e.fields}
        if isinstance(e, A.Field):
            obj = self.eval(e.obj)
            if isinstance(obj, dict) and e.name in obj:
                return obj[e.name]
            raise KimiyaRuntimeError(
                f"line {e.line}: no field '{e.name}'")
        if isinstance(e, A.Index):
            obj = self.eval(e.obj)
            return obj[int(self.eval(e.index))]
        if isinstance(e, A.Call):
            return self.call(e)
        if isinstance(e, A.BinOp):
            return self.binop(e)
        if isinstance(e, A.UnOp):
            v = self.eval(e.operand)
            return (not v) if e.op == "not" else -v
        if isinstance(e, A.ObserveExpr):
            return self.observe(e)
        raise KimiyaRuntimeError(f"cannot evaluate {e}")

    def observe(self, e: A.ObserveExpr):
        path = Path(str(self.eval(e.args[0])))
        self.cost["observes"] += 1
        self.last_obs[str(path)] = time.time()
        if not path.exists():
            self.trace.append({"kind": "observe", "path": str(path),
                               "exists": False, "line": e.line})
            return {"text": "", "path": str(path), "exists": False,
                    "mtime": 0, "sha": ""}
        text = path.read_text(encoding="utf-8", errors="replace")
        rec = {"text": text, "path": str(path), "exists": True,
               "mtime": path.stat().st_mtime,
               "sha": hashlib.sha256(text.encode()).hexdigest()[:12]}
        self.trace.append({"kind": "observe", "path": str(path),
                           "exists": True, "sha": rec["sha"],
                           "line": e.line})
        return rec

    def binop(self, e: A.BinOp):
        if e.op == "and":
            return bool(self.eval(e.left)) and bool(self.eval(e.right))
        if e.op == "or":
            return bool(self.eval(e.left)) or bool(self.eval(e.right))
        left, right = self.eval(e.left), self.eval(e.right)
        if e.op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return self.to_str(left) + self.to_str(right)
            if isinstance(left, list):
                return left + right
            return left + right
        if e.op == "-":
            return left - right
        ops = {"==": lambda: left == right, "!=": lambda: left != right,
               "<": lambda: left < right, ">": lambda: left > right,
               "<=": lambda: left <= right, ">=": lambda: left >= right}
        if e.op in ops:
            return ops[e.op]()
        raise KimiyaRuntimeError(f"unknown operator {e.op}")

    def call(self, e: A.Call):
        args = [self.eval(a) for a in e.args]
        f = e.func
        target = self.resolve_callable(f)
        if target is not None:
            try:
                return _pyify(target(*args))
            except (Bolt, ReturnSignal):
                raise
            except Exception as ex:
                raise KimiyaRuntimeError(
                    f"line {e.line}: {f}() failed: {ex}") from ex
        if f == "map":
            return [_pyify(args[0](x)) for x in args[1]]
        if f == "filter":
            return [x for x in args[1] if args[0](x)]
        if f == "sort_by":
            return sorted(args[1], key=args[0])
        if f == "sum":
            return sum(args[0])
        if f == "len":
            return len(args[0])
        if f == "contains":
            return self.to_str(args[1]) in self.to_str(args[0])
        if f == "starts_with":
            return self.to_str(args[0]).startswith(self.to_str(args[1]))
        if f == "lower":
            return self.to_str(args[0]).lower()
        if f == "trim":
            return self.to_str(args[0]).strip()
        if f == "lines":
            return self.to_str(args[0]).splitlines()
        if f == "join":
            return self.to_str(args[1]).join(self.to_str(x)
                                             for x in args[0])
        if f == "str":
            return self.to_str(args[0])
        if f == "num":
            return float(args[0])
        if f == "hash":
            return hashlib.sha256(
                self.to_str(args[0]).encode()).hexdigest()[:12]
        if f == "now":
            return time.time()
        if f == "range":
            return list(range(int(args[0])))
        if f == "first":
            return args[0][0] if args[0] else None
        if f == "last":
            return args[0][-1] if args[0] else None
        if f == "keys":
            return list(args[0].keys())
        if f == "file_exists":
            self.trace.append({"kind": "observe", "path": str(args[0]),
                               "predicate": "exists", "line": e.line})
            return Path(str(args[0])).exists()
        raise KimiyaRuntimeError(f"line {e.line}: unknown function {f}")

    @staticmethod
    def as_list(v, line) -> list:
        if isinstance(v, list):
            return v
        raise KimiyaRuntimeError(f"line {line}: expected a list")

    # first-class builtins usable as map/filter arguments
    # (populated at module level below)

    @staticmethod
    def to_str(v) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return str(v)


def _pyify(v):
    """Marshal a Python return value into a Kimiya value."""
    if isinstance(v, (str, bool, int, float, list, dict)) or v is None:
        return v
    if isinstance(v, (tuple, set, frozenset)):
        return [_pyify(x) for x in v]
    return str(v)


_BUILTIN_VALUES = {
    "num": lambda x: float(x),
    "str": Interp.to_str,
    "lower": lambda s: str(s).lower(),
    "trim": lambda s: str(s).strip(),
    "len": len,
    "first": lambda xs: xs[0] if xs else None,
    "last": lambda xs: xs[-1] if xs else None,
}

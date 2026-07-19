"""Static checker: the paper's disciplines as rejection rules.

  K1  judged relations must cite a declared purpose
  K2  a retry's judge panel must be cross-provenance from the body's
      generator (J-not-lhd-C)
  K3  select recall must lie in (0, 1]
  K4  an irreversible act may not occur inside a retry body
  K5  an irreversible act must be gated (verified gate)
  K6  world-touching retry needs inv/compensate (snapshot retry over an
      external world is unsound)
  K7  gen schemas must be declared (or builtin Text/Json)
  K8  names defined before use; functions called at declared arity;
      function bodies see only their parameters and other functions
  K9  retry budgets and settle deadlines positive; return only inside fn
"""

from __future__ import annotations

from . import ast_nodes as A
from .runtime import family_of

BUILTIN_SCHEMAS = {"Text", "Json"}
BUILTIN_FUNCS = {
    "len", "contains", "starts_with", "lower", "trim", "lines", "join",
    "str", "num", "hash", "now", "range", "first", "last", "keys",
    "file_exists", "map", "filter", "sort_by", "sum",
}
DEFAULT_IRREVERSIBLE = {("file", "overwrite"), ("file", "delete")}
KNOWN_ACTIONS = {("file", "create"), ("file", "append"),
                 ("file", "overwrite"), ("file", "delete"),
                 ("file", "mkdir")}


def _substmts(s) -> list[list]:
    if isinstance(s, A.IfStmt):
        return [s.then] + ([s.els] if s.els else [])
    if isinstance(s, A.ForallStmt):
        return [s.body]
    if isinstance(s, A.RetryStmt):
        return [s.body] + ([s.compensate] if s.compensate else [])
    if isinstance(s, A.Assign) and isinstance(s.rhs, A.RetryStmt):
        return _substmts(s.rhs)
    return []


class CheckReport:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, line: int, msg: str):
        self.errors.append(f"line {line}: error: {msg}")

    def warn(self, line: int, msg: str):
        self.warnings.append(f"line {line}: warning: {msg}")

    @property
    def ok(self) -> bool:
        return not self.errors


def check(prog: A.Program, py_fn_names=frozenset()) -> CheckReport:
    r = CheckReport()
    pools = {d.name: d.model for d in prog.decls
             if isinstance(d, A.PoolDecl)}
    for d in prog.decls:
        if isinstance(d, A.AgentDecl):
            fk = d.fields
            backend = fk.get("backend", "ollama")
            if backend not in ("ollama", "openai", "openrouter"):
                r.err(d.line, f"agent '{d.name}': unknown backend "
                              f"'{backend}'")
            if "model" not in fk:
                r.err(d.line, f"agent '{d.name}': missing model")
            if backend == "openrouter" and "key_env" not in fk:
                r.warn(d.line, f"agent '{d.name}': openrouter without "
                               "key_env — set key_env to a variable name")
            if backend == "openai" and "url" not in fk:
                r.err(d.line, f"agent '{d.name}': openai backend needs a "
                              "url (the pod's /v1 endpoint)")
            # family_of over the model, respecting a family override
            fam = fk.get("family") or family_of(fk.get("model", ""))
            pools[d.name] = "override://" + fam if fk.get("family") \
                else fk.get("model", "")
    contexts = {d.name for d in prog.decls if isinstance(d, A.ContextDecl)}
    schemas = ({d.name for d in prog.decls if isinstance(d, A.SchemaDecl)}
               | BUILTIN_SCHEMAS)
    fns = {d.name: d for d in prog.decls if isinstance(d, A.FnDecl)}
    pyfns = set(py_fn_names) | {d.name for d in prog.decls
                                if isinstance(d, A.PyFnDecl)}
    irreversible = set(DEFAULT_IRREVERSIBLE)
    for d in prog.decls:
        if isinstance(d, A.EffectDecl):
            if d.klass == "irreversible":
                irreversible.add((d.surface, d.action))
            else:
                irreversible.discard((d.surface, d.action))

    defined: set[str] = set()
    in_fn = [False]

    def known_callable(name: str) -> bool:
        return (name in BUILTIN_FUNCS or name in fns or name in pyfns
                or name in defined)

    # ---------- expressions ----------
    def chk_expr(e):
        if isinstance(e, A.Var):
            if not (e.name in defined or e.name in fns or e.name in pyfns
                    or e.name in BUILTIN_FUNCS):
                r.err(e.line, f"undefined name '{e.name}'")
        elif isinstance(e, A.Call):
            if not known_callable(e.func):
                r.err(e.line, f"unknown function '{e.func}'")
            if e.func in fns and len(e.args) != len(fns[e.func].params):
                r.err(e.line,
                      f"'{e.func}' takes {len(fns[e.func].params)} "
                      f"argument(s), got {len(e.args)}")
            for a in e.args:
                chk_expr(a)
        elif isinstance(e, A.BinOp):
            chk_expr(e.left)
            chk_expr(e.right)
        elif isinstance(e, A.UnOp):
            chk_expr(e.operand)
        elif isinstance(e, A.Field):
            chk_expr(e.obj)
        elif isinstance(e, A.Index):
            chk_expr(e.obj)
            chk_expr(e.index)
        elif isinstance(e, A.ListExpr):
            for x in e.items:
                chk_expr(x)
        elif isinstance(e, A.RecordExpr):
            for _, x in e.fields:
                chk_expr(x)
        elif isinstance(e, A.ObserveExpr):
            if e.surface != "file":
                r.err(e.line, f"unknown observe surface '{e.surface}'")
            for a in e.args:
                chk_expr(a)

    # ---------- guards ----------
    def chk_guard(g, generator_model):
        if isinstance(g, A.CheckGuard):
            chk_expr(g.expr)
            return
        if g.context not in contexts:
            r.err(g.line, f"judge cites undeclared purpose '{g.context}' "
                          "(a judged relation without a declared purpose "
                          "is ill-formed)")
        if not 0 < g.tau <= 1:
            r.err(g.line, f"threshold {g.tau} outside (0,1]")
        if g.panel:
            for p in g.panel:
                if p not in pools:
                    r.err(g.line,
                          f"panel member '{p}' is not a declared pool")
        if generator_model:
            gf = family_of(generator_model)
            cand = ([pools[p] for p in (g.panel or []) if p in pools]
                    if g.panel else list(pools.values()))
            cand = [m for m in cand if m != generator_model] or cand
            if cand and all(family_of(m) == gf for m in cand):
                r.err(g.line,
                      "J ⋪ C violated: every available panel model shares "
                      f"the generator's family '{gf}' — self-judgment "
                      "never certifies (add a pool from another family)")
        chk_expr(g.left)
        if g.right is not None:
            chk_expr(g.right)

    # ---------- statements ----------
    def body_generator(stmts):
        for s in stmts:
            if isinstance(s, A.Assign) and isinstance(s.rhs, A.GenExpr):
                if s.rhs.by:
                    return pools.get(s.rhs.by)
                return next(iter(pools.values()), None)
        return None

    def contains_act(stmts) -> bool:
        for s in stmts:
            if isinstance(s, A.ActStmt):
                return True
            if any(contains_act(sub) for sub in _substmts(s)):
                return True
        return False

    def gated(prev, s) -> bool:
        return (isinstance(prev, A.CheckStmt)
                or getattr(s, "gated_by_if", False))

    def chk_stmts(stmts, in_retry: bool):
        prev = None
        for s in stmts:
            chk_stmt(s, in_retry, prev)
            prev = s

    def chk_stmt(s, in_retry: bool, prev):
        if isinstance(s, A.Assign):
            rhs = s.rhs
            if isinstance(rhs, A.GenExpr):
                if rhs.schema not in schemas:
                    r.err(rhs.line,
                          f"gen cites undeclared schema '{rhs.schema}'")
                if rhs.by and rhs.by not in pools:
                    r.err(rhs.line, f"'by {rhs.by}': not a declared pool")
                chk_expr(rhs.prompt)
            elif isinstance(rhs, A.SelectExpr):
                if not 0 < rhs.recall <= 1:
                    r.err(rhs.line,
                          f"select recall {rhs.recall} outside (0,1] — a "
                          "coverage claim needs a stated recall")
                if rhs.context and rhs.context not in contexts:
                    r.err(rhs.line, f"select cites undeclared purpose "
                                    f"'{rhs.context}'")
                chk_expr(rhs.query)
                chk_expr(rhs.store)
            elif isinstance(rhs, A.RetryStmt):
                chk_retry(rhs)
            else:
                chk_expr(rhs)
            defined.add(s.name)
        elif isinstance(s, (A.CheckStmt, A.PrintStmt, A.CommitStmt)):
            chk_expr(s.expr)
        elif isinstance(s, A.AbstainStmt):
            pass
        elif isinstance(s, A.ReturnStmt):
            if not in_fn[0]:
                r.err(s.line, "return outside a function")
            if s.expr is not None:
                chk_expr(s.expr)
        elif isinstance(s, A.IfStmt):
            chk_guard(s.guard, None)
            for branch in (s.then, s.els or []):
                if branch and isinstance(branch[0], A.ActStmt):
                    branch[0].gated_by_if = True
            chk_stmts(s.then, in_retry)
            if s.els:
                chk_stmts(s.els, in_retry)
        elif isinstance(s, A.ForallStmt):
            chk_expr(s.iterable)
            defined.add(s.var)
            chk_stmts(s.body, in_retry)
        elif isinstance(s, A.RetryStmt):
            chk_retry(s)
        elif isinstance(s, A.ActStmt):
            key = (s.surface, s.action)
            if key not in KNOWN_ACTIONS and key not in irreversible:
                r.err(s.line, f"unknown action {s.surface}.{s.action}")
            for a in s.args:
                chk_expr(a)
            if key in irreversible:
                if in_retry:
                    r.err(s.line,
                          f"irreversible act {s.surface}.{s.action} inside "
                          "a retry body (forbidden: retry may re-fire it)")
                elif not gated(prev, s):
                    r.err(s.line,
                          f"irreversible act {s.surface}.{s.action} is "
                          "unguarded — precede it with a check, or place "
                          "it first in a checked/judged if-branch (the "
                          "verified gate)")
        elif isinstance(s, A.SettleStmt):
            chk_guard(s.guard, None)
            if s.within <= 0:
                r.err(s.line, "settle deadline must be positive")

    def chk_retry(s: A.RetryStmt):
        if s.budget <= 0:
            r.err(s.line, "retry budget must be positive")
        gen_model = body_generator(s.body)
        chk_stmts(s.body, in_retry=True)
        chk_guard(s.guard, gen_model)
        if contains_act(s.body) and s.inv is None and s.compensate is None:
            r.err(s.line,
                  "retry body performs world effects but declares neither "
                  "inv nor compensate — snapshot retry over an external "
                  "world is unsound (world-frame retry required)")
        if s.inv is not None:
            chk_expr(s.inv)
        if s.compensate:
            chk_stmts(s.compensate, in_retry=False)

    # ---------- function bodies: own scope, no globals ----------
    for f in fns.values():
        saved = set(defined)
        defined.clear()
        defined.update(f.params)
        in_fn[0] = True
        chk_stmts(f.body, in_retry=False)
        in_fn[0] = False
        defined.clear()
        defined.update(saved)

    if not pools:
        has_model_step = any(
            isinstance(s, A.Assign)
            and isinstance(s.rhs, (A.GenExpr, A.SelectExpr, A.RetryStmt))
            for s in prog.body)
        if has_model_step:
            r.warn(0, "no pool declared: gen/select/judge will fail at "
                      "runtime")
    chk_stmts(prog.body, in_retry=False)
    return r

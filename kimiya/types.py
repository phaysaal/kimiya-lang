"""A lightweight, gradual type checker for Kimiya.

Goal: catch a real class of bugs *before* a model call is spent — mainly
field typos on schema/observation records and obvious shape errors
(iterating a string, selecting over a non-list) — without false
positives. It is deliberately gradual: anything it cannot pin down is
`Unknown`, and Unknown never produces an error.

What it knows:
- gen<Schema> yields a closed record with the schema's fields.
- observe file(...) yields the observation record
  {text, path, exists, mtime, sha}.
- select yields a list; lines/keys/range yield lists; join/lower/trim/str
  yield text; len/num/now yield num; contains/starts_with/file_exists
  yield bool.
- field access on a *known* record with an *undeclared* field is an error.
- forall over, and select over, a value that is statically text is an
  error (a shape mistake the discipline checker doesn't catch).

Everything else widens to Unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as A


# ---------------- type lattice ----------------

class Ty:
    pass


class _Prim(Ty):
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name

    def __eq__(self, other):
        return isinstance(other, _Prim) and other.name == self.name

    def __hash__(self):
        return hash(("prim", self.name))


TEXT = _Prim("text")
NUM = _Prim("num")
BOOL = _Prim("bool")
NULLT = _Prim("null")
UNKNOWN = _Prim("unknown")


@dataclass
class ListTy(Ty):
    elem: Ty

    def __repr__(self):
        return f"list<{self.elem!r}>"

    def __eq__(self, other):
        return isinstance(other, ListTy) and other.elem == self.elem

    def __hash__(self):
        return hash(("list", repr(self.elem)))


@dataclass
class RecordTy(Ty):
    fields: dict           # name -> Ty
    origin: str = ""       # "schema Foo" / "observation" / "record"

    def __repr__(self):
        return (self.origin or "record") + "{" + \
            ", ".join(self.fields) + "}"

    def __eq__(self, other):
        return (isinstance(other, RecordTy)
                and set(other.fields) == set(self.fields))

    def __hash__(self):
        return hash(("record", tuple(sorted(self.fields))))


OBSERVATION = RecordTy(
    {"text": TEXT, "path": TEXT, "exists": BOOL, "mtime": NUM, "sha": TEXT},
    origin="observation")

# `observe screen(...)`. Distinct from a file observation: it has no
# .text (nothing was read, only captured), and it carries the capture
# origin so coordinates can be mapped back to the whole display.
SCREENSHOT = RecordTy(
    {"kind": TEXT, "path": TEXT, "sha": TEXT, "exists": BOOL,
     "width": NUM, "height": NUM, "x": NUM, "y": NUM,
     "display": TEXT, "region": TEXT, "driver": TEXT},
    origin="screenshot")

# What a vision `select` yields: x/y are the control's centre in absolute
# screen coordinates, ready to hand to `act screen.click`.
CONTROL = RecordTy(
    {"x": NUM, "y": NUM, "w": NUM, "h": NUM, "left": NUM, "top": NUM,
     "label": TEXT, "confidence": NUM}, origin="control")

SCHEMA_FIELD_TYPES = {
    "text": TEXT, "string": TEXT, "str": TEXT,
    "num": NUM, "number": NUM, "int": NUM, "float": NUM,
    "bool": BOOL, "boolean": BOOL,
    "list": ListTy(UNKNOWN),
}

BUILTIN_RET = {
    "len": NUM, "num": NUM, "now": NUM,
    "contains": BOOL, "starts_with": BOOL, "file_exists": BOOL,
    "lower": TEXT, "trim": TEXT, "str": TEXT, "hash": TEXT, "join": TEXT,
    "lines": ListTy(TEXT), "keys": ListTy(TEXT), "range": ListTy(NUM),
    "sum": NUM,
}


def _widen(a: Ty, b: Ty) -> Ty:
    if a == b:
        return a
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    if isinstance(a, ListTy) and isinstance(b, ListTy):
        return ListTy(_widen(a.elem, b.elem))
    return UNKNOWN


class TypeReport:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, line, msg):
        self.errors.append(f"line {line}: type error: {msg}")

    def warn(self, line, msg):
        self.warnings.append(f"line {line}: type warning: {msg}")

    @property
    def ok(self):
        return not self.errors


def typecheck(prog: A.Program) -> TypeReport:
    r = TypeReport()
    param_env = {d.name: {"text": TEXT, "num": NUM, "bool": BOOL}
                 .get(d.type, UNKNOWN)
                 for d in prog.decls if isinstance(d, A.ParamDecl)}
    schemas: dict[str, RecordTy] = {}
    for d in prog.decls:
        if isinstance(d, A.SchemaDecl):
            schemas[d.name] = RecordTy(
                {f: SCHEMA_FIELD_TYPES.get(t.lower(), UNKNOWN)
                 for f, t in d.fields}, origin=f"schema {d.name}")
    def ty(e, env) -> Ty:
        if isinstance(e, A.Lit):
            v = e.value
            if isinstance(v, bool):
                return BOOL
            if isinstance(v, (int, float)):
                return NUM
            if isinstance(v, str):
                return TEXT
            if v is None:
                return NULLT
            return UNKNOWN
        if isinstance(e, A.Var):
            return env.get(e.name, UNKNOWN)
        if isinstance(e, A.ListExpr):
            if not e.items:
                return ListTy(UNKNOWN)
            el = ty(e.items[0], env)
            for x in e.items[1:]:
                el = _widen(el, ty(x, env))
            return ListTy(el)
        if isinstance(e, A.RecordExpr):
            return RecordTy({k: ty(v, env) for k, v in e.fields},
                            origin="record")
        if isinstance(e, A.Field):
            base = ty(e.obj, env)
            if isinstance(base, RecordTy):
                if e.name in base.fields:
                    return base.fields[e.name]
                r.err(e.line, f"{base!r} has no field '{e.name}' "
                              f"(fields: {', '.join(base.fields) or 'none'})")
                return UNKNOWN
            if base in (TEXT, NUM, BOOL):
                r.err(e.line, f"cannot read field '{e.name}' of a {base!r} "
                              "value")
                return UNKNOWN
            return UNKNOWN
        if isinstance(e, A.Index):
            base = ty(e.obj, env)
            if isinstance(base, ListTy):
                return base.elem
            return UNKNOWN
        if isinstance(e, A.ObserveExpr):
            return SCREENSHOT if e.surface == "screen" else OBSERVATION
        if isinstance(e, A.Call):
            return ty_call(e, env)
        if isinstance(e, A.BinOp):
            return ty_binop(e, env)
        if isinstance(e, A.UnOp):
            return BOOL if e.op == "not" else NUM
        return UNKNOWN

    def ty_call(e: A.Call, env) -> Ty:
        for a in e.args:
            ty(a, env)
        f = e.func
        if f in BUILTIN_RET:
            return BUILTIN_RET[f]
        if f in ("first", "last"):
            a = ty(e.args[0], env) if e.args else UNKNOWN
            return a.elem if isinstance(a, ListTy) else UNKNOWN
        if f in ("filter", "sort_by"):
            return ty(e.args[1], env) if len(e.args) > 1 else ListTy(UNKNOWN)
        if f == "map":
            return ListTy(UNKNOWN)
        return UNKNOWN

    def ty_binop(e: A.BinOp, env) -> Ty:
        lt, rt = ty(e.left, env), ty(e.right, env)
        if e.op in ("and", "or", "==", "!=", "<", ">", "<=", ">="):
            return BOOL
        if e.op == "+":
            if lt is TEXT or rt is TEXT:
                return TEXT
            if isinstance(lt, ListTy) or isinstance(rt, ListTy):
                return _widen(lt if isinstance(lt, ListTy) else rt,
                              rt if isinstance(rt, ListTy) else lt)
            if lt is NUM and rt is NUM:
                return NUM
            return UNKNOWN
        if e.op == "-":
            return NUM
        return UNKNOWN

    def ty_rhs(rhs, env) -> Ty:
        if isinstance(rhs, A.GenExpr):
            if rhs.schema in schemas:
                return schemas[rhs.schema]
            if rhs.schema == "Text":
                return TEXT
            return UNKNOWN         # Json
        if isinstance(rhs, A.SelectExpr):
            st = ty(rhs.store, env)
            if st == SCREENSHOT:            # the vision instrument
                return ListTy(CONTROL)
            if st in (TEXT, NUM, BOOL):
                r.err(rhs.line, f"select store is {st!r}, expected a list "
                                "(wrap a text with lines(...))")
            return ListTy(st.elem if isinstance(st, ListTy) else UNKNOWN)
        if isinstance(rhs, A.RetryStmt):
            return ty_retry(rhs, env)
        return ty(rhs, env)

    def check_guard(g, env):
        if isinstance(g, A.CheckGuard):
            ty(g.expr, env)
        else:
            ty(g.left, env)
            if g.right is not None:
                ty(g.right, env)

    def ty_retry(s: A.RetryStmt, env) -> Ty:
        last = UNKNOWN
        for st in s.body:
            check_stmt(st, env)
            if isinstance(st, A.Assign):
                last = env.get(st.name, UNKNOWN)
        check_guard(s.guard, env)
        if s.inv is not None:
            ty(s.inv, env)
        if s.compensate:
            for st in s.compensate:
                check_stmt(st, env)
        return last

    def check_stmt(s, env):
        if isinstance(s, A.Assign):
            env[s.name] = ty_rhs(s.rhs, env)
        elif isinstance(s, (A.CheckStmt, A.PrintStmt, A.CommitStmt)):
            ty(s.expr, env)
        elif isinstance(s, A.ReturnStmt):
            if s.expr is not None:
                ty(s.expr, env)
        elif isinstance(s, A.IfStmt):
            check_guard(s.guard, env)
            then_env = dict(env)
            for st in s.then:
                check_stmt(st, then_env)
            else_env = dict(env)
            for st in (s.els or []):
                check_stmt(st, else_env)
            for k in set(then_env) | set(else_env):
                a = then_env.get(k, env.get(k, UNKNOWN))
                b = else_env.get(k, env.get(k, UNKNOWN))
                env[k] = _widen(a, b)
        elif isinstance(s, A.ForallStmt):
            it = ty(s.iterable, env)
            if it in (TEXT, NUM, BOOL):
                r.err(s.line, f"cannot iterate a {it!r} with forall "
                              "(a list is required; lines(...) splits text)")
            elem = it.elem if isinstance(it, ListTy) else UNKNOWN
            body_env = dict(env)
            body_env[s.var] = elem
            for st in s.body:
                check_stmt(st, body_env)
            for k in body_env:            # loop may run 0 times: widen
                if k != s.var:
                    env[k] = _widen(env.get(k, UNKNOWN), body_env[k])
        elif isinstance(s, A.RetryStmt):
            ty_retry(s, env)
        elif isinstance(s, A.ActStmt):
            for a in s.args:
                ty(a, env)
        elif isinstance(s, A.SettleStmt):
            check_guard(s.guard, env)

    # function bodies: own scope, params Unknown
    for d in prog.decls:
        if isinstance(d, A.FnDecl):
            fenv = {p: UNKNOWN for p in d.params}
            for st in d.body:
                check_stmt(st, fenv)

    top_env: dict[str, Ty] = dict(param_env)
    for st in prog.body:
        check_stmt(st, top_env)
    return r

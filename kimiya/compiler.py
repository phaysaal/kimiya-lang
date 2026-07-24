"""Compile a checked Kimiya program to a standalone Python file.

Strategy: the control flow is inlined as plain Python; every variable
lives in an explicit `env` dict (so retry can snapshot/restore it exactly
as the interpreter does); semantic operations call the shared Runtime.
Kimiya functions compile to Python functions with their own env.

The emitted file imports only `kimiya.compiled_runtime`. Static checks
run before emission, so a compiled artifact is guaranteed well-formed.
"""

from __future__ import annotations

import json

from . import ast_nodes as A


class Compiler:
    def __init__(self, prog: A.Program, py_exts, source_file: str):
        self.prog = prog
        self.py_exts = py_exts
        self.source_file = source_file
        self.lines: list[str] = []
        self.indent = 0

    def emit(self, s: str = ""):
        self.lines.append("    " * self.indent + s if s else "")

    # ---------------- expressions ----------------
    def expr(self, e) -> str:
        if isinstance(e, A.Lit):
            return repr(e.value)
        if isinstance(e, A.Var):
            return f"_v(env, {e.name!r})"
        if isinstance(e, A.ListExpr):
            return "[" + ", ".join(self.expr(x) for x in e.items) + "]"
        if isinstance(e, A.RecordExpr):
            return ("{" + ", ".join(f"{k!r}: {self.expr(v)}"
                                    for k, v in e.fields) + "}")
        if isinstance(e, A.Field):
            return f"({self.expr(e.obj)})[{e.name!r}]"
        if isinstance(e, A.Index):
            return f"({self.expr(e.obj)})[int({self.expr(e.index)})]"
        if isinstance(e, A.Call):
            args = ", ".join(self.expr(a) for a in e.args)
            return f"rt.call({e.func!r}, [{args}])"
        if isinstance(e, A.BinOp):
            return self.binop(e)
        if isinstance(e, A.UnOp):
            if e.op == "not":
                return f"(not ({self.expr(e.operand)}))"
            return f"(-({self.expr(e.operand)}))"
        if isinstance(e, A.ObserveExpr):
            args = ", ".join(self.expr(a) for a in e.args)
            return f"rt.observe({e.surface!r}, [{args}], {e.actor!r})"
        raise NotImplementedError(f"expr {e}")

    def binop(self, e: A.BinOp) -> str:
        lo, ro = self.expr(e.left), self.expr(e.right)
        if e.op == "and":
            return f"(bool({lo}) and bool({ro}))"
        if e.op == "or":
            return f"(bool({lo}) or bool({ro}))"
        if e.op == "+":
            return f"_add({lo}, {ro})"
        py = {"-": "-", "==": "==", "!=": "!=", "<": "<", ">": ">",
              "<=": "<=", ">=": ">="}[e.op]
        return f"({lo} {py} {ro})"

    # ---------------- guards ----------------
    def guard_lambda(self, g) -> str:
        if isinstance(g, A.CheckGuard):
            return f"lambda: rt.check_guard({self.expr(g.expr)})"
        right = self.expr(g.right) if g.right is not None else "None"
        panel = json.dumps(g.panel) if g.panel else "None"
        return (f"lambda: rt.judge({g.k}, {g.tau}, {g.relation!r}, "
                f"{self.expr(g.left)}, {right}, {g.context!r}, {panel}, "
                f"{g.paraphrases})")

    # ---------------- statements ----------------
    def stmts(self, body):
        if not body:
            self.emit("pass")
        for s in body:
            self.stmt(s)

    def stmt(self, s):
        if isinstance(s, A.Assign):
            self.emit(f"env[{s.name!r}] = {self.rhs(s.rhs, s.name)}")
        elif isinstance(s, A.CheckStmt):
            self.emit(f"rt.check({self.expr(s.expr)}, {s.line})")
        elif isinstance(s, A.PrintStmt):
            self.emit(f"print(rt.call('str', [{self.expr(s.expr)}]))")
        elif isinstance(s, A.CommitStmt):
            self.emit(f"rt.commit({self.expr(s.expr)})")
        elif isinstance(s, A.AbstainStmt):
            self.emit(f"rt.abstain({s.line})")
        elif isinstance(s, A.ReturnStmt):
            self.emit("return "
                      + (self.expr(s.expr) if s.expr is not None else "None"))
        elif isinstance(s, A.IfStmt):
            self.emit(f"if ({self.guard_lambda(s.guard)})():")
            self.indent += 1
            self.stmts(s.then)
            self.indent -= 1
            if s.els:
                self.emit("else:")
                self.indent += 1
                self.stmts(s.els)
                self.indent -= 1
        elif isinstance(s, A.ForallStmt):
            self.emit(f"for _it in {self.expr(s.iterable)}:")
            self.indent += 1
            self.emit(f"env[{s.var!r}] = _it")
            self.stmts(s.body)
            self.indent -= 1
        elif isinstance(s, A.RetryStmt):
            self.retry(s, None)
        elif isinstance(s, A.ActStmt):
            args = ", ".join(self.expr(a) for a in s.args)
            self.emit(f"rt.act({s.surface!r}, {s.action!r}, [{args}], "
                      f"{s.actor!r})")
        elif isinstance(s, A.SettleStmt):
            self.emit(f"rt.settle({self.guard_lambda(s.guard)}, "
                      f"{s.within}, {s.line})")
        else:
            raise NotImplementedError(f"stmt {s}")

    def rhs(self, rhs, name):
        if isinstance(rhs, A.GenExpr):
            by = repr(rhs.by) if rhs.by else "None"
            return (f"rt.gen({rhs.schema!r}, {self.expr(rhs.prompt)}, {by})")
        if isinstance(rhs, A.SelectExpr):
            ctx = repr(rhs.context) if rhs.context else "None"
            by = repr(rhs.by) if rhs.by else "None"
            return (f"rt.select({rhs.recall}, {self.expr(rhs.query)}, "
                    f"{self.expr(rhs.store)}, {ctx}, {by})")
        if isinstance(rhs, A.RetryStmt):
            self.retry(rhs, name)
            return "env.get(%r)" % (self._retry_last(rhs.body))
        return self.expr(rhs)

    def _retry_last(self, body):
        last = None
        for s in body:
            if isinstance(s, A.Assign):
                last = s.name
        return last

    def retry(self, s: A.RetryStmt, _name):
        writes = self._writes(s.body)
        bid = id(s)
        self.emit(f"def _body_{bid}():")
        self.indent += 1
        self.stmts(s.body)
        self.indent -= 1
        self.emit(f"def _guard_{bid}():")
        self.indent += 1
        self.emit(f"return ({self.guard_lambda(s.guard)})()")
        self.indent -= 1
        inv = "None"
        if s.inv is not None:
            self.emit(f"def _inv_{bid}():")
            self.indent += 1
            self.emit(f"return bool({self.expr(s.inv)})")
            self.indent -= 1
            inv = f"_inv_{bid}"
        comp = "None"
        if s.compensate:
            self.emit(f"def _comp_{bid}():")
            self.indent += 1
            self.stmts(s.compensate)
            self.indent -= 1
            comp = f"_comp_{bid}"
        self.emit(f"rt.retry({s.budget}, _body_{bid}, _guard_{bid}, env, "
                  f"{json.dumps(writes)}, {inv}, {comp}, {s.line})")

    def _writes(self, body) -> list[str]:
        out = []
        for s in body:
            if isinstance(s, A.Assign) and s.name not in out:
                out.append(s.name)
        return out

    # ---------------- module ----------------
    def compile(self) -> str:
        agents = []
        displays = []
        params = []
        contexts = {}
        schemas = {}
        fns = []
        pyfns = []
        for d in self.prog.decls:
            if isinstance(d, A.ParamDecl):
                params.append({"name": d.name, "type": d.type,
                               "default": d.default,
                               "required": d.required})
            elif isinstance(d, A.DisplayDecl):
                displays.append({"name": d.name, **d.fields})
            elif isinstance(d, A.PoolDecl):
                agents.append({"name": d.name, "model": d.model})
            elif isinstance(d, A.AgentDecl):
                agents.append({"name": d.name, **d.fields})
            elif isinstance(d, A.ContextDecl):
                contexts[d.name] = (
                    f"{d.domain}; preserve: {', '.join(d.preserve)}; "
                    f"may lose: {', '.join(d.allow_loss)}")
            elif isinstance(d, A.SchemaDecl):
                schemas[d.name] = [f for f, _ in d.fields]
            elif isinstance(d, A.FnDecl):
                fns.append(d)
            elif isinstance(d, A.PyFnDecl):
                pyfns.append(d)

        self.emit("# Generated by `kimiya compile` — do not edit by hand.")
        self.emit("# This artifact passed the static checks at compile "
                  "time; it")
        self.emit("# cannot represent an ill-formed Kimiya program.")
        self.emit("import sys")
        self.emit("import importlib, importlib.util")
        self.emit("from kimiya.compiled_runtime import Runtime, Bolt, "
                  "_to_str, _pyify, parse_cli")
        self.emit()
        # repr, not json.dumps: agent fields may be booleans (vision), and
        # JSON's `true` is not Python.
        self.emit(f"_AGENTS = {agents!r}")
        self.emit(f"_DISPLAYS = {displays!r}")
        self.emit(f"_PARAMS = {params!r}")
        self.emit(f"_CONTEXTS = {json.dumps(contexts)}")
        self.emit(f"_SCHEMAS = {json.dumps(schemas)}")
        self.emit(f"_PY_EXTS = {json.dumps(self.py_exts)}")
        self.emit()
        self.emit("def _add(a, b):")
        self.indent += 1
        self.emit("if isinstance(a, str) or isinstance(b, str):")
        self.emit("    return _to_str(a) + _to_str(b)")
        self.emit("return a + b")
        self.indent -= 1
        self.emit()
        self.emit("def _v(env, name):")
        self.indent += 1
        self.emit("if name in env: return env[name]")
        self.emit("return rt.ref(name)")
        self.indent -= 1
        self.emit()

        # user functions
        for f in fns:
            self.emit(f"def _fn_{f.name}(*_args):")
            self.indent += 1
            self.emit(f"env = dict(zip({json.dumps(f.params)}, _args))")
            self.stmts(f.body)
            self.emit("return None")
            self.indent -= 1
            self.emit()

        # python extension loading (re-verifies SHA, preserves audit story)
        self.emit("def _load_py(rt):")
        self.indent += 1
        self.emit("for ext in _PY_EXTS:")
        self.indent += 1
        self.emit("if ext['kind'] == 'file':")
        self.indent += 1
        self.emit("spec = importlib.util.spec_from_file_location("
                  "'kx_'+ext['sha'], ext['path'])")
        self.emit("m = importlib.util.module_from_spec(spec)")
        self.emit("spec.loader.exec_module(m)")
        self.emit("for name in ext['functions']:")
        self.emit("    rt.py_funcs[name] = getattr(m, name)")
        self.emit("print(f\"⚠ python extension: {ext['path']} \"")
        self.emit("      f\"(sha {ext['sha']}) — kernel-grade, audit it\")")
        self.indent -= 1
        self.emit("else:")
        self.indent += 1
        self.emit("mod, _, attr = ext['target'].rpartition('.')")
        self.emit("rt.py_funcs[ext['name']] = getattr("
                  "importlib.import_module(mod), attr)")
        self.indent -= 2
        self.indent -= 1
        self.emit()

        # main
        self.emit("def main():")
        self.indent += 1
        self.emit("global rt")
        self.emit("models, params = parse_cli(sys.argv[1:], _PARAMS)")
        self.emit(f"rt = Runtime(__file__, _AGENTS, _CONTEXTS, _SCHEMAS, "
                  f"_PY_EXTS, models, displays=_DISPLAYS, params=params)")
        self.emit("_load_py(rt)")
        fnmap = ("{" + ", ".join(f"{f.name!r}: _fn_{f.name}"
                                 for f in fns) + "}")
        self.emit(f"rt.register({fnmap}, rt.py_funcs)")
        for d in pyfns:
            self.emit(f"import importlib as _il")
            mod = d.target.rpartition(".")
            self.emit(f"rt.py_funcs[{d.name!r}] = getattr("
                      f"_il.import_module({mod[0]!r}), {mod[2]!r})")
        self.emit("remote = [a for a in rt.pool.agents if not a.is_local]")
        self.emit("for a in remote:")
        self.emit("    print(f'⚠ egress: {a.name} → {a.model} @ "
                  "{a.host} ({a.backend})')")
        self.emit("env = dict(params)")
        self.emit("try:")
        self.indent += 1
        self.stmts(self.prog.body)
        self.emit("rt.report()")
        self.indent -= 1
        self.emit("except Bolt as b:")
        self.indent += 1
        self.emit("rt.report('ABSTAINED', b.reason)")
        self.indent -= 1
        self.indent -= 1
        self.emit()
        self.emit("if __name__ == '__main__':")
        self.emit("    main()")
        return "\n".join(self.lines) + "\n"


def compile_program(prog: A.Program, py_exts, source_file: str) -> str:
    return Compiler(prog, py_exts, source_file).compile()

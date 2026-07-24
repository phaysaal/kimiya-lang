"""Recursive-descent parser for Kimiya (core + world extension).

Layout summary (see README for the full grammar):

    pool A = "llama3.1:8b"
    context k_ev:
        domain     = "grounded entailment"
        preserve   = [evidential_support]
        allow_loss = [style]
    schema Summary:
        text: text

    e := select<0.95>(support_of(c), sources) under k_ev
    s := retry budget 4 until judge<5,4/5> (e |= s) under k_ev:
        s := gen<Summary>(ground(c, e)) by A
    if check contains(s.text, "claim"):
        commit(s)
    else:
        abstain

World extension:

    x := observe file("notes.txt")
    act file.create("out.txt", digest)
    settle until check file_mtime_gt("out.txt", t0) within 10
    retry budget 3 until check ...:
        act file.append("log.txt", line)
    inv file_lines_le("log.txt", 100)
    compensate:
        act file.delete("log.txt.tmp")
"""

from __future__ import annotations

from .lexer import lex, Token
from . import ast_nodes as A


class ParseError(SyntaxError):
    pass


class Parser:
    def __init__(self, source: str):
        self.toks = lex(source)
        self.i = 0

    # ------------- token helpers -------------
    def peek(self, off=0) -> Token:
        return self.toks[min(self.i + off, len(self.toks) - 1)]

    def next(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, kind: str, value: str | None = None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    def at_kw(self, *words) -> bool:
        t = self.peek()
        return t.kind in ("KEYWORD", "WKEYWORD") and t.value in words

    def expect(self, kind: str, value: str | None = None) -> Token:
        t = self.peek()
        if not self.at(kind, value):
            want = value or kind
            raise ParseError(
                f"line {t.line}: expected {want!r}, found {t.value or t.kind!r}")
        return self.next()

    def expect_kw(self, word: str) -> Token:
        t = self.peek()
        if not self.at_kw(word):
            raise ParseError(
                f"line {t.line}: expected {word!r}, found {t.value or t.kind!r}")
        return self.next()

    def skip_newlines(self):
        while self.at("NEWLINE"):
            self.next()

    # ------------- entry -------------
    def parse(self) -> A.Program:
        prog = A.Program()
        self.skip_newlines()
        while not self.at("EOF"):
            if self.at_kw("pool", "context", "schema", "effect", "fn",
                          "use", "pyfn", "agent", "display", "param"):
                prog.decls.append(self.decl())
            else:
                prog.body.append(self.stmt())
            self.skip_newlines()
        return prog

    # ------------- declarations -------------
    def decl(self):
        t = self.peek()
        if self.at_kw("pool"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", "=")
            model = self.expect("STRING").value
            self.expect("NEWLINE")
            return A.PoolDecl(name, model, t.line)
        if self.at_kw("param"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", ":")
            ptype = self.expect("NAME").value
            default, required = None, True
            if self.at("OP", "="):
                self.next()
                required = False
                neg = False
                if self.at("OP", "-"):
                    self.next()
                    neg = True
                t2 = self.peek()
                if t2.kind == "NUMBER":
                    default = float(self.next().value)
                    if neg:
                        default = -default
                elif neg:
                    raise ParseError(f"line {t2.line}: '-' must be followed "
                                     "by a number in a param default")
                elif t2.kind == "STRING":
                    default = self.next().value
                elif self.at_kw("true") or self.at_kw("false"):
                    default = self.next().value == "true"
                else:
                    raise ParseError(
                        f"line {t2.line}: param default must be a string, "
                        "number, or true/false literal")
            self.expect("NEWLINE")
            return A.ParamDecl(name, ptype, default, required, t.line)
        if self.at_kw("display"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            dfields = {}
            while not self.at("DEDENT"):
                self.skip_newlines()
                if self.at("DEDENT"):
                    break
                key = self.expect("NAME").value
                self.expect("OP", "=")
                dfields[key] = self.expect("STRING").value
                self.expect("NEWLINE")
            self.expect("DEDENT")
            return A.DisplayDecl(name, dfields, t.line)
        if self.at_kw("agent"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            afields = {}
            while not self.at("DEDENT"):
                self.skip_newlines()
                if self.at("DEDENT"):
                    break
                key = self.expect("NAME").value
                self.expect("OP", "=")
                if self.at_kw("true") or self.at_kw("false"):
                    afields[key] = self.next().value == "true"
                else:
                    afields[key] = self.expect("STRING").value
                self.expect("NEWLINE")
            self.expect("DEDENT")
            return A.AgentDecl(name, afields, t.line)
        if self.at_kw("context"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            domain, preserve, allow = "", [], []
            while not self.at("DEDENT"):
                self.skip_newlines()
                if self.at("DEDENT"):
                    break
                key = self.next()
                self.expect("OP", "=")
                if key.value == "domain":
                    domain = self.expect("STRING").value
                elif key.value in ("preserve", "allow_loss"):
                    vals = self.name_list()
                    if key.value == "preserve":
                        preserve = vals
                    else:
                        allow = vals
                else:
                    raise ParseError(f"line {key.line}: unknown context field "
                                     f"{key.value!r}")
                self.expect("NEWLINE")
            self.expect("DEDENT")
            return A.ContextDecl(name, domain, preserve, allow, t.line)
        if self.at_kw("schema"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            fields = []
            while not self.at("DEDENT"):
                self.skip_newlines()
                if self.at("DEDENT"):
                    break
                fname = self.expect("NAME").value
                self.expect("OP", ":")
                ftype = self.expect("NAME").value
                fields.append((fname, ftype))
                self.expect("NEWLINE")
            self.expect("DEDENT")
            return A.SchemaDecl(name, fields, t.line)
        if self.at_kw("effect"):
            self.next()
            surface = self.expect("NAME").value
            self.expect("OP", ".")
            action = self.expect("NAME").value
            klass = self.next()
            if klass.value not in ("irreversible", "recoverable"):
                raise ParseError(f"line {klass.line}: effect class must be "
                                 "irreversible or recoverable")
            self.expect("NEWLINE")
            return A.EffectDecl(surface, action, klass.value, t.line)
        if self.at_kw("fn"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", "(")
            params = []
            while not self.at("OP", ")"):
                params.append(self.expect("NAME").value)
                if self.at("OP", ","):
                    self.next()
            self.expect("OP", ")")
            self.expect("OP", ":")
            body = self.block()
            return A.FnDecl(name, params, body, t.line)
        if self.at_kw("use"):
            self.next()
            is_python = False
            if self.at_kw("python"):
                self.next()
                is_python = True
            path = self.expect("STRING").value
            self.expect("NEWLINE")
            return A.UseDecl(path, is_python, t.line)
        if self.at_kw("pyfn"):
            self.next()
            name = self.expect("NAME").value
            self.expect("OP", "=")
            target = self.expect("STRING").value
            self.expect("NEWLINE")
            return A.PyFnDecl(name, target, t.line)
        raise ParseError(f"line {t.line}: expected declaration")

    def name_list(self) -> list[str]:
        self.expect("OP", "[")
        out = []
        while not self.at("OP", "]"):
            out.append(self.expect("NAME").value)
            if self.at("OP", ","):
                self.next()
        self.expect("OP", "]")
        return out

    # ------------- statements -------------
    def block(self) -> list:
        self.expect("NEWLINE")
        self.expect("INDENT")
        stmts = []
        self.skip_newlines()
        while not self.at("DEDENT"):
            stmts.append(self.stmt())
            self.skip_newlines()
        self.expect("DEDENT")
        return stmts

    def stmt(self):
        t = self.peek()
        if self.at_kw("check"):
            self.next()
            e = self.expr()
            self.end_stmt()
            return A.CheckStmt(e, t.line)
        if self.at_kw("print"):
            self.next()
            e = self.expr()
            self.end_stmt()
            return A.PrintStmt(e, t.line)
        if self.at_kw("commit"):
            self.next()
            self.expect("OP", "(")
            e = self.expr()
            self.expect("OP", ")")
            self.end_stmt()
            return A.CommitStmt(e, t.line)
        if self.at_kw("abstain"):
            self.next()
            self.end_stmt()
            return A.AbstainStmt(t.line)
        if self.at_kw("return"):
            self.next()
            expr = None
            if not (self.at("NEWLINE") or self.at("DEDENT")):
                expr = self.expr()
            self.end_stmt()
            return A.ReturnStmt(expr, t.line)
        if self.at_kw("if"):
            self.next()
            g = self.guard()
            self.expect("OP", ":")
            then = self.block()
            els = None
            self.skip_newlines()
            if self.at_kw("else"):
                self.next()
                self.expect("OP", ":")
                els = self.block()
            return A.IfStmt(g, then, els, t.line)
        if self.at_kw("forall"):
            self.next()
            var = self.expect("NAME").value
            self.expect_kw("in")
            it = self.expr()
            self.expect("OP", ":")
            body = self.block()
            return A.ForallStmt(var, it, body, t.line)
        if self.at_kw("retry"):
            return self.retry_stmt()
        if self.at_kw("act"):
            self.next()
            actor = self.actor_index()
            surface = self.expect("NAME").value
            self.expect("OP", ".")
            action = self.expect("NAME").value
            self.expect("OP", "(")
            args = self.args_until_rparen()
            self.end_stmt()
            return A.ActStmt(surface, action, args, actor=actor,
                             line=t.line)
        if self.at_kw("settle"):
            self.next()
            actor = self.actor_index()
            self.expect_kw("until")
            g = self.guard()
            self.expect_kw("within")
            secs = float(self.expect("NUMBER").value)
            self.end_stmt()
            return A.SettleStmt(g, secs, actor=actor, line=t.line)
        if self.at("NAME") and self.peek(1).kind == "OP" \
                and self.peek(1).value == ":=":
            name = self.next().value
            self.next()  # :=
            rhs = self.rhs()
            if not isinstance(rhs, A.RetryStmt):   # block rhs ends itself
                self.end_stmt()
            return A.Assign(name, rhs, t.line)
        raise ParseError(
            f"line {t.line}: unexpected {t.value or t.kind!r} "
            "(expected a statement)")

    def end_stmt(self):
        if self.at("NEWLINE"):
            self.next()
        elif not (self.at("DEDENT") or self.at("EOF")):
            t = self.peek()
            raise ParseError(f"line {t.line}: unexpected {t.value!r} "
                             "after statement")

    def retry_stmt(self):
        t = self.expect_kw("retry")
        self.expect_kw("budget")
        budget = int(float(self.expect("NUMBER").value))
        self.expect_kw("until")
        guard = self.guard()
        self.expect("OP", ":")
        body = self.block()
        inv = None
        comp = None
        self.skip_newlines()
        if self.at_kw("inv"):
            self.next()
            inv = self.expr()
            self.end_stmt()
            self.skip_newlines()
        if self.at_kw("compensate"):
            self.next()
            self.expect("OP", ":")
            comp = self.block()
        return A.RetryStmt(budget, body, guard, inv, comp, t.line)

    # ------------- rhs / guards -------------
    def rhs(self):
        t = self.peek()
        if self.at_kw("gen"):
            self.next()
            self.expect("OP", "<")
            schema = self.expect("NAME").value
            self.expect("OP", ">")
            self.expect("OP", "(")
            prompt = self.expr()
            self.expect("OP", ")")
            by = None
            if self.at_kw("by"):
                self.next()
                by = self.expect("NAME").value
            return A.GenExpr(schema, prompt, by, t.line)
        if self.at_kw("select"):
            self.next()
            self.expect("OP", "<")
            recall = float(self.expect("NUMBER").value)
            self.expect("OP", ">")
            self.expect("OP", "(")
            query = self.expr()
            self.expect("OP", ",")
            store = self.expr()
            self.expect("OP", ")")
            ctx = None
            if self.at_kw("under"):
                self.next()
                ctx = self.expect("NAME").value
            by = None
            if self.at_kw("by"):     # the instrument, for a vision select
                self.next()
                by = self.expect("NAME").value
            return A.SelectExpr(recall, query, store, ctx, by, t.line)
        if self.at_kw("observe"):
            self.next()
            surface = self.expect("NAME").value
            actor = self.actor_index()
            self.expect("OP", "(")
            args = self.args_until_rparen()
            return A.ObserveExpr(surface, args, actor=actor, line=t.line)
        if self.at_kw("retry"):
            # x := retry ... — value is the body's last assigned variable
            return self.retry_stmt()
        return self.expr()

    def guard(self):
        t = self.peek()
        if self.at_kw("check"):
            self.next()
            return A.CheckGuard(self.expr(), t.line)
        if self.at_kw("judge"):
            self.next()
            self.expect("OP", "<")
            k = int(float(self.expect("NUMBER").value))
            self.expect("OP", ",")
            num = float(self.expect("NUMBER").value)
            tau = num
            if self.at("OP", "/"):
                self.next()
                tau = num / float(self.expect("NUMBER").value)
            self.expect("OP", ">")
            # The relation may be parenthesised — `judge<5,4/5> (a |= b)` —
            # or not, as `shows(shot, "…")` reads better bare.
            wrapped = self.at("OP", "(")
            if wrapped:
                self.next()
            left = self.expr()
            relation, right = "rubric", None
            # shows(screenshot, "…") — a relation whose left side is an
            # image, written as a call so no new keyword is needed.
            if isinstance(left, A.Call) and left.func == "shows":
                if len(left.args) != 2:
                    raise ParseError(
                        f"line {t.line}: shows takes 2 arguments "
                        f"(a screenshot and a claim), got {len(left.args)}")
                relation, right, left = "shows", left.args[1], left.args[0]
            elif self.at("OP", "|="):
                self.next()
                relation, right = "entails", self.expr()
            elif self.at("OP", "~"):
                self.next()
                relation, right = "equiv", self.expr()
            elif self.at_kw("contradicts"):
                self.next()
                relation, right = "contradicts", self.expr()
            if wrapped:
                self.expect("OP", ")")
            self.expect_kw("under")
            ctx = self.expect("NAME").value
            panel = None
            if self.at_kw("panel"):
                self.next()
                panel = self.name_list()
            paras = 2
            if self.at_kw("paraphrase_prompts"):
                self.next()
                paras = int(float(self.expect("NUMBER").value))
            return A.JudgeGuard(k, tau, relation, left, right, ctx,
                                panel, paras, t.line)
        raise ParseError(f"line {t.line}: expected check or judge guard")

    def actor_index(self):
        """An optional `<NAME>` after act/settle/observe-surface."""
        if self.at("OP", "<"):
            self.next()
            actor = self.expect("NAME").value
            self.expect("OP", ">")
            return actor
        return None

    # ------------- expressions -------------
    def args_until_rparen(self) -> list:
        args = []
        while not self.at("OP", ")"):
            args.append(self.expr())
            if self.at("OP", ","):
                self.next()
        self.expect("OP", ")")
        return args

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        left = self.and_expr()
        while self.at_kw("or"):
            t = self.next()
            left = A.BinOp("or", left, self.and_expr(), t.line)
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.at_kw("and"):
            t = self.next()
            left = A.BinOp("and", left, self.not_expr(), t.line)
        return left

    def not_expr(self):
        if self.at_kw("not"):
            t = self.next()
            return A.UnOp("not", self.not_expr(), t.line)
        return self.cmp_expr()

    def cmp_expr(self):
        left = self.add_expr()
        while self.at("OP") and self.peek().value in (
                "==", "!=", "<", ">", "<=", ">="):
            t = self.next()
            left = A.BinOp(t.value, left, self.add_expr(), t.line)
        return left

    def add_expr(self):
        left = self.unary_expr()
        while self.at("OP") and self.peek().value in ("+", "-"):
            t = self.next()
            left = A.BinOp(t.value, left, self.unary_expr(), t.line)
        return left

    def unary_expr(self):
        if self.at("OP", "-"):
            t = self.next()
            return A.UnOp("-", self.unary_expr(), t.line)
        return self.postfix_expr()

    def postfix_expr(self):
        e = self.atom()
        while True:
            if self.at("OP", "."):
                t = self.next()
                name = self.expect("NAME").value
                e = A.Field(e, name, t.line)
            elif self.at("OP", "["):
                t = self.next()
                idx = self.expr()
                self.expect("OP", "]")
                e = A.Index(e, idx, t.line)
            else:
                return e

    def atom(self):
        t = self.peek()
        if self.at("STRING"):
            self.next()
            return A.Lit(t.value, t.line)
        if self.at("NUMBER"):
            self.next()
            v = float(t.value)
            return A.Lit(int(v) if v.is_integer() else v, t.line)
        if self.at_kw("true"):
            self.next()
            return A.Lit(True, t.line)
        if self.at_kw("false"):
            self.next()
            return A.Lit(False, t.line)
        if self.at_kw("null"):
            self.next()
            return A.Lit(None, t.line)
        if self.at_kw("observe"):
            self.next()
            surface = self.expect("NAME").value
            actor = self.actor_index()
            self.expect("OP", "(")
            args = self.args_until_rparen()
            return A.ObserveExpr(surface, args, actor=actor, line=t.line)
        if self.at("OP", "["):
            self.next()
            items = []
            while not self.at("OP", "]"):
                items.append(self.expr())
                if self.at("OP", ","):
                    self.next()
            self.expect("OP", "]")
            return A.ListExpr(items, t.line)
        if self.at("OP", "{"):
            self.next()
            fields = []
            while not self.at("OP", "}"):
                name = self.expect("NAME").value
                self.expect("OP", ":")
                fields.append((name, self.expr()))
                if self.at("OP", ","):
                    self.next()
            self.expect("OP", "}")
            return A.RecordExpr(fields, t.line)
        if self.at("OP", "("):
            self.next()
            e = self.expr()
            self.expect("OP", ")")
            return e
        if self.at("NAME"):
            self.next()
            if self.at("OP", "("):
                self.next()
                args = self.args_until_rparen()
                return A.Call(t.value, args, t.line)
            return A.Var(t.value, t.line)
        raise ParseError(
            f"line {t.line}: unexpected {t.value or t.kind!r} in expression")


def parse(source: str) -> A.Program:
    return Parser(source).parse()

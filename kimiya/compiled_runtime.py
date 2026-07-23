"""Runtime library for COMPILED Kimiya programs.

`kimiya compile FILE` emits a standalone Python file whose control flow is
inlined; the semantic operations (gen/select/judge/check/act/observe,
retry snapshotting, the θ/cost/trace accounting, the certificate) live
here and are shared with — and equivalence-tested against — the
interpreter. The emitted program builds one Runtime, drives it with plain
Python control flow, and prints the same certificate `kimiya run` does.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import screen
from .runtime import (Pool, Agent, Trace, Datasheets, get_oracle,
                      run_judge, run_gen)


class Bolt(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Runtime:
    def __init__(self, source_file: str, agents, contexts, schemas,
                 py_exts=None, models_override=None):
        self.workspace = Path(source_file).parent / ".kimiya"
        self.workspace.mkdir(exist_ok=True)
        self.trace = Trace(self.workspace)
        self.sheets = Datasheets(self.workspace)
        self.oracle = get_oracle()
        self.contexts = contexts        # name -> purpose text
        self.schemas = schemas          # name -> [field, ...]
        self.py_exts = py_exts or []
        self.fns: dict = {}             # registered by the compiled module
        self.py_funcs: dict = {}
        binds = {}
        for a in agents:
            binds[a["name"]] = Agent(
                name=a["name"], model=a.get("model", ""),
                backend=a.get("backend", "ollama"), url=a.get("url"),
                key_env=a.get("key_env"), family_override=a.get("family"))
        if models_override:
            binds = {f"M{i}": Agent(name=f"M{i}", model=m)
                     for i, m in enumerate(models_override)}
        self.pool = Pool(binds)
        self.theta: list = []
        self.cost = {"gen_calls": 0, "judge_votes": 0, "acts": 0,
                     "observes": 0}
        self.uncertified = 0
        self.screen_acts = 0
        self.last_gen: Agent | None = None
        self.committed = None

    def register(self, fns: dict, py_funcs: dict):
        self.fns = fns
        self.py_funcs = py_funcs

    # ---- primitive operations (mirror the interpreter) ----
    def gen(self, schema: str, prompt: str, by: str | None):
        agent = self.pool.agent(by) if by else self.pool.default_generator()
        self.last_gen = agent
        self.cost["gen_calls"] += 1
        if schema == "Text":
            return run_gen(self.oracle, self.trace, agent, prompt, None)
        if schema == "Json":
            r = run_gen(self.oracle, self.trace, agent,
                        prompt + "\n\nFIELDS: result", ["result"])
            return r
        fields = self.schemas[schema]
        return run_gen(self.oracle, self.trace, agent, prompt, fields)

    def select(self, recall: float, query: str, store: list, ctx):
        words = {w for w in str(query).lower().split() if len(w) > 3}

        def score(x):
            t = _to_str(x).lower()
            return sum(1 for w in words if w in t)

        hits = [x for x in sorted(store, key=lambda x: -score(x))
                if score(x) > 0] or list(store)
        self.theta.append((f"select<{recall}>", recall))
        self.trace.append({"kind": "select", "recall": recall,
                           "store_size": len(store), "hits": len(hits)})
        return hits

    def judge(self, k, tau, relation, left, right, ctx_name,
              panel, paraphrases):
        purpose = self.contexts.get(ctx_name, ctx_name)
        left, right = _to_str(left), _to_str(right) if right is not None else ""
        task = f"{relation}:{ctx_name}"
        if relation == "entails":
            evidence, claim = left, f"The evidence supports: {right}"
        elif relation == "equiv":
            evidence = f"A: {left}\nB: {right}"
            claim = "A and B are interchangeable for the stated purpose"
        elif relation == "contradicts":
            evidence = f"A: {left}\nB: {right}"
            claim = "A and B are in direct contradiction"
        else:
            evidence, claim = left, f"The value satisfies: {purpose}"
        gen = self.last_gen or self.pool.default_generator()
        j = run_judge(self.pool, self.oracle, self.trace, self.sheets,
                      task, claim, evidence, gen, k, tau, purpose,
                      panel, paraphrases)
        self.cost["judge_votes"] += k
        if not j.certified:
            self.uncertified += 1
        sheet = self.sheets.get(task)
        self.theta.append((task if j.verdict else f"neg:{task}",
                           sheet["beta_lo"] if j.verdict
                           else 1 - sheet["alpha_hi"]))
        return j.verdict

    def check(self, value, line=0):
        self.trace.append({"kind": "check", "ok": bool(value), "line": line})
        if not value:
            raise Bolt(f"check failed at line {line}")
        return True

    def check_guard(self, value):
        self.trace.append({"kind": "check_guard", "ok": bool(value)})
        return bool(value)

    def commit(self, value):
        self.committed = value
        self.trace.append({"kind": "commit"})

    def abstain(self, line=0):
        raise Bolt(f"abstain at line {line}")

    def observe(self, surface, args):
        path = Path(_to_str(args[0]))
        self.cost["observes"] += 1
        if not path.exists():
            return {"text": "", "path": str(path), "exists": False,
                    "mtime": 0, "sha": ""}
        text = path.read_text(encoding="utf-8", errors="replace")
        rec = {"text": text, "path": str(path), "exists": True,
               "mtime": path.stat().st_mtime,
               "sha": hashlib.sha256(text.encode()).hexdigest()[:12]}
        self.trace.append({"kind": "observe", "path": str(path),
                           "sha": rec["sha"]})
        return rec

    def act(self, surface, action, args):
        self.cost["acts"] += 1
        if surface == "screen":
            self.screen_acts += 1
            try:
                rec = screen.perform(action, args)
            except screen.ScreenError as e:
                raise Bolt(str(e)) from None
            self.trace.append({"kind": "act", "surface": "screen",
                               "action": action, "target": screen.target(),
                               **rec})
            return
        path = Path(_to_str(args[0]))
        if action == "create":
            if path.exists():
                raise Bolt(f"file.create: {path} exists")
            path.write_text(_to_str(args[1]))
        elif action == "append":
            with path.open("a") as f:
                f.write(_to_str(args[1]))
        elif action == "overwrite":
            path.write_text(_to_str(args[1]))
        elif action == "delete":
            path.unlink(missing_ok=True)
        elif action == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        self.trace.append({"kind": "act", "action": action,
                           "path": str(path)})

    def settle(self, guard_fn, within, line=0):
        deadline = time.time() + within
        while time.time() < deadline:
            if guard_fn():
                return
            time.sleep(min(1.0, within / 10))
        raise Bolt(f"settle deadline elapsed at line {line}")

    def retry(self, budget, body_fn, guard_fn, env, writes,
              inv_fn=None, comp_fn=None, line=0):
        for _ in range(budget):
            snap = {w: env.get(w) for w in writes}
            mark = len(self.theta)
            try:
                body_fn()
                if guard_fn():
                    return
            except Bolt:
                pass
            del self.theta[mark:]
            for w, v in snap.items():
                env[w] = v
            if inv_fn is not None and not inv_fn():
                if comp_fn:
                    comp_fn()
                if not inv_fn():
                    raise Bolt(f"world invariant broken at line {line}")
            elif comp_fn:
                comp_fn()
        raise Bolt(f"retry budget exhausted at line {line}")

    # ---- callables (fns, python, builtins) ----
    def ref(self, name):
        if name in self.fns:
            return self.fns[name]
        if name in self.py_funcs:
            return self.py_funcs[name]
        if name in _BUILTINS:
            return _BUILTINS[name]
        raise Bolt(f"undefined name '{name}'")

    def call(self, name, args):
        f = None
        if name in self.fns:
            f = self.fns[name]
        elif name in self.py_funcs:
            f = self.py_funcs[name]
        elif name in _BUILTINS:
            f = _BUILTINS[name]
        if f is None:
            raise Bolt(f"unknown function '{name}'")
        return _pyify(f(*args))

    # ---- certificate ----
    def report(self, status=None, reason=""):
        if status is None:
            status = "COMMITTED" if self.committed is not None else "ENDED"
        theta = 1.0
        for _, x in self.theta:
            theta *= x
        tasks = sorted({n for n, _ in self.theta if not n.startswith("select")})
        egress = sorted({a.host for a in self.pool.agents if not a.is_local})
        cert = {
            "status": status, "reason": reason, "value": self.committed,
            "theta": round(theta, 4),
            "theta_factors": [(n, round(x, 4)) for n, x in self.theta],
            "uncertified_judgments": self.uncertified,
            "instruments": {t: self.sheets.get(t) for t in tasks},
            "python_extensions": self.py_exts,
            "egress": egress,
            "screen": ({"driver": screen.driver_name(),
                        "target": screen.target(),
                        "acts": self.screen_acts}
                       if self.screen_acts else None),
            "cost": dict(self.cost), "trace_records": self.trace.count(),
        }
        (self.workspace / "certificate.json").write_text(
            json.dumps(cert, indent=2, ensure_ascii=False, default=str))
        print("\n── certificate ──────────────────────────────")
        print(f"  status : {status}" + (f"  ({reason})" if reason else ""))
        if status == "COMMITTED":
            print("  value  : "
                  + json.dumps(self.committed, ensure_ascii=False,
                               default=str)[:200])
        print(f"  θ      : {cert['theta']}   {cert['theta_factors']}")
        if self.uncertified:
            print(f"  ⚠ {self.uncertified} uncertified judgment(s)")
        if egress:
            print(f"  egress : {', '.join(egress)} (prompts left the machine)")
        else:
            print("  egress : none (all agents local)")
        if cert["screen"]:
            sc = cert["screen"]
            print(f"  screen : {sc['acts']} act(s) via {sc['driver']} "
                  f"on {sc['target']}")
        c = cert["cost"]
        print(f"  cost   : {c['gen_calls']} gen, {c['judge_votes']} votes, "
              f"{c['acts']} acts, {c['observes']} observes")
        print("─────────────────────────────────────────────")
        return cert


def _to_str(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _pyify(v):
    if isinstance(v, (str, bool, int, float, list, dict)) or v is None:
        return v
    if isinstance(v, (tuple, set, frozenset)):
        return [_pyify(x) for x in v]
    return str(v)


_BUILTINS = {
    "len": len, "contains": lambda a, b: _to_str(b) in _to_str(a),
    "starts_with": lambda a, b: _to_str(a).startswith(_to_str(b)),
    "lower": lambda s: _to_str(s).lower(), "trim": lambda s: _to_str(s).strip(),
    "lines": lambda s: _to_str(s).splitlines(),
    "join": lambda xs, sep: _to_str(sep).join(_to_str(x) for x in xs),
    "str": _to_str, "num": lambda x: float(x),
    "hash": lambda s: hashlib.sha256(_to_str(s).encode()).hexdigest()[:12],
    "now": lambda: time.time(), "range": lambda n: list(range(int(n))),
    "first": lambda xs: xs[0] if xs else None,
    "last": lambda xs: xs[-1] if xs else None,
    "keys": lambda d: list(d.keys()),
    "file_exists": lambda p: Path(_to_str(p)).exists(),
    "map": lambda f, xs: [_pyify(f(x)) for x in xs],
    "filter": lambda f, xs: [x for x in xs if f(x)],
    "sort_by": lambda f, xs: sorted(xs, key=f),
    "sum": lambda xs: sum(xs),
}

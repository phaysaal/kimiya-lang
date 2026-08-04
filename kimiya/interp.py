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
import os
import time
from pathlib import Path

from . import ast_nodes as A
from . import image as image_surface
from . import screen
from . import vision
from ._version import __version__ as KIMIYA_VERSION
from .runtime import (Pool, Agent, Trace, Datasheets, MemoStore,
                      get_oracle, run_judge, resolve_params, run_gen,
                      Secret, redact_value)


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
                 py_exts: list | None = None,
                 replay: bool = False,
                 params: dict | None = None):
        self.prog = prog
        self.workspace = program_path.parent / ".kimiya"
        self.workspace.mkdir(exist_ok=True)
        self.trace = Trace(self.workspace)
        self.sheets = Datasheets(self.workspace)
        self.locate_cache = vision.LocateCache(self.workspace)
        self.memo = MemoStore(self.workspace)
        self.memo_counted: set[str] = set()   # per-run: factor entered θ
        self.memo_hits = 0
        self.explore_depth = 0
        self.theta_excluded = 0               # factors gated by explore
        self.replay = replay or os.environ.get("KIMIYA_REPLAY") == "1"
        self.oracle = get_oracle()
        # Resolve declared params against the caller's pairs BEFORE
        # anything else: a missing required param must refuse the run
        # while refusal is still free (no model has been consulted).
        table = [{"name": d.name, "type": d.type, "default": d.default,
                  "required": d.required}
                 for d in prog.decls if isinstance(d, A.ParamDecl)]
        self.cli_params = resolve_params(table, params or {})
        self.env: dict[str, object] = dict(self.cli_params)
        self.contexts = {d.name: d for d in prog.decls
                         if isinstance(d, A.ContextDecl)}
        self.schemas = {d.name: d for d in prog.decls
                        if isinstance(d, A.SchemaDecl)}
        self.fns = {d.name: d for d in prog.decls
                    if isinstance(d, A.FnDecl)}
        self.py_funcs = py_funcs or {}
        self.py_exts = py_exts or []
        self.displays: dict[str, screen.Display] = {}
        for d in prog.decls:
            if isinstance(d, A.DisplayDecl):
                f = d.fields
                self.displays[d.name] = screen.Display(
                    name=d.name, x11=f.get("x11"), ssh=f.get("ssh"),
                    monitor=f.get("monitor"))
        self.default_display = screen.Display()
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
                    key_file=f.get("key_file"), zdr=f.get("zdr", False),
                    family_override=f.get("family"),
                    vision_declared=f.get("vision"))
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
        self.locates = 0
        self.locates_cached = 0      # exact-sha hits: same image, free
        self.locates_replayed = 0    # replay hits: pixels changed, disclosed
        self.overclaims: list[str] = []
        self.image_observations: list[dict] = []
        self.image_egress: list[dict] = []
        self.committed = None

    def add_theta(self, name: str, factor: float):
        """One door for reliability factors. Inside `explore`, a factor
        is trace-recorded but excluded from θ — exploration gates
        progress, never the verdict (the same rule retry applies to its
        failed rounds)."""
        if self.explore_depth > 0:
            self.theta_excluded += 1
            self.trace.append({"kind": "theta_excluded", "task": name,
                               "factor": round(factor, 4)})
        else:
            self.theta.append((name, factor))

    # ------------------------------------------------ purpose text
    def purpose_text(self, name: str | None) -> str:
        if name is None:
            return "unscoped"
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
        # A negative reading is the same instrument, read the other way —
        # report it under the instrument's own name, not "neg:…", which
        # has no datasheet and would print as prior-grade.
        tasks = sorted({name[4:] if name.startswith("neg:") else name
                        for name, _ in self.theta})
        return {
            "status": status,
            "reason": reason,
            "value": redact_value(self.committed),
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
            "image_observations": list(self.image_observations),
            "image_egress": list(self.image_egress),
            "screen": ({"driver": screen.driver_name(),
                        "target": screen.target(),
                        "acts": self.screen_acts,
                        "locates": self.locates,
                        "locates_cached": self.locates_cached,
                        "locates_replayed": self.locates_replayed,
                        "actors": {n: {"label": d.label,
                                       "ssh": d.is_remote}
                                   for n, d in self.displays.items()}}
                       if self.screen_acts or self.locates else None),
            "overclaims": list(self.overclaims),
            "params": redact_value(dict(self.cli_params)),
            "kimiya_version": KIMIYA_VERSION,
            "memo_hits": self.memo_hits,
            "explored": self.theta_excluded,
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
            v = self.eval(s.expr)
            print(v.redacted() if isinstance(v, Secret)
                  else self.to_str(v))
        elif isinstance(s, A.CommitStmt):
            if self.explore_depth > 0:
                raise KimiyaRuntimeError(
                    f"line {s.line}: commit while exploring — the judged "
                    "factors here are excluded from θ, so this verdict "
                    "would rest on unaccounted judgments")
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
        elif isinstance(s, A.ExploreStmt):
            self.explore_depth += 1
            self.trace.append({"kind": "explore", "line": s.line,
                               "phase": "enter"})
            try:
                self.exec_stmts(s.body)
            finally:
                self.explore_depth -= 1
                self.trace.append({"kind": "explore", "line": s.line,
                                   "phase": "exit"})
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
        image_paths = None
        image_meta = []
        if g.images is not None:
            try:
                image_paths, image_meta = image_surface.prepare(
                    self.eval(g.images))
            except image_surface.ImageError as ex:
                raise Bolt(f"gen at line {g.line}: {ex}") from None
            if image_paths and not agent.vision:
                raise Bolt(
                    f"gen at line {g.line}: agent {agent.name} "
                    f"({agent.model}) is not vision-capable")
            if image_paths and not agent.is_local:
                for item in image_meta:
                    entry = {"agent": agent.name, "host": agent.host, **item}
                    if entry not in self.image_egress:
                        self.image_egress.append(entry)
        fields = None
        if g.schema == "Text":
            fields = None
        elif g.schema == "Json":
            fields = []
        else:
            fields = [f for f, _ in self.schemas[g.schema].fields]
        # A gen that consumes images is a READ — an instrument reading
        # of the world, like a locate — and enters θ at its datasheet's
        # conservative end under read:<purpose>. Text-only gen stays
        # free: the model proposes, and only the gates warrant.
        read_task = (f"read:{g.context or 'unscoped'}"
                     if image_paths else None)
        if g.memo:
            image_key = ",".join(item["preview_sha"] for item in image_meta)
            key = MemoStore.key("gen", g.schema, prompt, agent.label(),
                                image_key)
            ent = self.memo.get(key)
            if ent is not None:
                self.memo_hits += 1
                self.trace.append({"kind": "gen", "cache": "memo",
                                   "agent": ent.get("agent", ""),
                                   "line": g.line})
                if read_task and key not in self.memo_counted:
                    if self.explore_depth == 0:
                        self.memo_counted.add(key)
                    self.add_theta(ent.get("factor_name", read_task),
                                   ent.get("factor",
                                           self.sheets.get(read_task)
                                           ["beta_lo"]))
                return ent["value"]
        self.cost["gen_calls"] += 1
        if fields == []:
            out = run_gen(self.oracle, self.trace, agent,
                          prompt + "\n\nFIELDS: result", ["result"],
                          images=image_paths)
        else:
            out = run_gen(self.oracle, self.trace, agent, prompt, fields,
                          images=image_paths)
        factor = None
        if read_task and out is not None:
            factor = self.sheets.get(read_task)["beta_lo"]
            self.add_theta(read_task, factor)
        if g.memo and out is not None:
            entry = {"value": out, "agent": agent.label()}
            if read_task:
                entry["factor_name"] = read_task
                entry["factor"] = factor
            self.memo.put(key, entry)
            if read_task and self.explore_depth == 0:
                self.memo_counted.add(key)
        return out

    def eval_select(self, sel: A.SelectExpr):
        store_val = self.eval(sel.store)
        if isinstance(store_val, dict) and store_val.get("kind") == "screen":
            return self.select_vision(sel, store_val)
        query = self.to_str(self.eval(sel.query)).lower()
        store = self.as_list(store_val, sel.line)
        words = {w for w in query.split() if len(w) > 3}

        def score(item):
            t = self.to_str(item).lower()
            return sum(1 for w in words if w in t)

        hits = [x for x in sorted(store, key=lambda x: -score(x))
                if score(x) > 0] or list(store)
        task = f"select:{sel.context or 'unscoped'}"
        sheet = self.sheets.get(task)
        self.add_theta(task, sheet["beta_lo"])
        if sel.recall > sheet["beta_lo"]:
            note = (f"line {sel.line}: declared recall {sel.recall} exceeds "
                    f"the measured β≥{sheet['beta_lo']:.3f} of instrument "
                    f"{task} — θ uses the measured end, not the claim")
            if note not in self.overclaims:
                self.overclaims.append(note)
            self.trace.append({"kind": "overclaim", "task": task,
                               "declared_recall": sel.recall,
                               "measured_beta_lo": sheet["beta_lo"],
                               "line": sel.line})
        self.trace.append({"kind": "select", "task": task,
                           "recall": sel.recall,
                           "store_size": len(store), "hits": len(hits),
                           "line": sel.line})
        return hits

    def select_vision(self, sel: A.SelectExpr, shot: dict):
        """`select` over a screenshot: retrieval by a measured instrument.

        θ takes the datasheet's conservative end, not the recall the
        programmer declared — a locate is an instrument reading, and the
        certificate should carry the rate the instrument was measured at.
        The declared recall stays a coverage claim, and overclaiming it
        against the measured β is reported on every run.
        """
        if not shot.get("exists"):
            raise Bolt(
                f"select at line {sel.line}: no screenshot to look at — "
                "`observe screen(...)` returned exists: false (driver "
                f"{shot.get('driver')!r})")
        agent = (self.pool.agent(sel.by) if sel.by
                 else self.pool.default_generator())
        query = self.to_str(self.eval(sel.query))
        try:
            hits, source = vision.locate(
                self.oracle, agent, self.trace, shot, query,
                self.purpose_text(sel.context), sel.context,
                cache=self.locate_cache, replay=self.replay)
        except vision.ReplayMiss as e:
            raise Bolt(f"select at line {sel.line}: {e}") from None
        task = vision.locate_task(sel.context)
        sheet = self.sheets.get(task)
        self.locates += 1
        if source == "exact":
            self.locates_cached += 1
        elif source == "replay":
            self.locates_replayed += 1
        if hits:
            self.add_theta(task, sheet["beta_lo"])
        else:
            self.add_theta(f"neg:{task}", 1 - sheet["alpha_hi"])
        if sel.recall > sheet["beta_lo"]:
            note = (f"line {sel.line}: declared recall {sel.recall} exceeds "
                    f"the measured β≥{sheet['beta_lo']:.3f} of instrument "
                    f"{task} — θ uses the measured end, not the claim")
            if note not in self.overclaims:
                self.overclaims.append(note)
            self.trace.append({"kind": "overclaim", "task": task,
                               "declared_recall": sel.recall,
                               "measured_beta_lo": sheet["beta_lo"],
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
        purpose = self.purpose_text(g.context)
        task = f"{g.relation}:{g.context}"
        images = None
        image_meta = []
        if g.relation == "shows":
            shot = self.eval(g.left)
            if not (isinstance(shot, dict)
                    and shot.get("kind") in ("screen", "image")):
                raise KimiyaRuntimeError(
                    f"line {g.line}: shows(...) needs an observed image "
                    "as its first argument")
            if not shot.get("exists"):
                raise Bolt(
                    f"shows at line {g.line}: image observation returned "
                    "exists: false")
            if shot.get("kind") == "screen":
                images = [shot["path"]]
                left = (
                    f"screenshot of {shot['region']} on {shot['display']}, "
                    f"{shot['width']}x{shot['height']} at origin "
                    f"({shot['x']}, {shot['y']}), sha {shot['sha']}"
                )
            else:
                try:
                    images, image_meta = image_surface.prepare([shot])
                except image_surface.ImageError as ex:
                    raise Bolt(f"shows at line {g.line}: {ex}") from None
                left = (
                    f"observed {shot['format']} image {shot['width']}x"
                    f"{shot['height']}, source sha {shot['sha']}, "
                    f"preview sha {shot['preview_sha']}"
                )
            right = self.to_str(self.eval(g.right))
            evidence, claim = left, f"The observed image shows: {right}"
            return self.run_guard(
                g, task, claim, evidence, purpose, images, image_meta)
        left = self.to_str(self.eval(g.left))
        right = self.to_str(self.eval(g.right)) if g.right is not None else ""
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
        return self.run_guard(g, task, claim, evidence, purpose)

    def run_guard(self, g, task, claim, evidence, purpose, images=None,
                  image_meta=None):
        memo_key = None
        if getattr(g, "memo", False):
            memo_key = MemoStore.key(
                "judge", task, claim, evidence, g.k, g.tau,
                ",".join(g.panel or []))
            ent = self.memo.get(memo_key)
            if ent is not None:
                # Same reading, consulted again: free, and its factor
                # enters θ at most once per run.
                self.memo_hits += 1
                self.trace.append({"kind": "judge", "cache": "memo",
                                   "task": task,
                                   "verdict": ent["verdict"],
                                   "line": g.line})
                if memo_key not in self.memo_counted:
                    if self.explore_depth == 0:
                        self.memo_counted.add(memo_key)
                    self.add_theta(ent["factor_name"], ent["factor"])
                return ent["verdict"]
        generator = self.last_gen_model or self.pool.default_generator()
        j = run_judge(self.pool, self.oracle, self.trace, self.sheets,
                      task, claim, evidence, generator, g.k, g.tau,
                      purpose, g.panel, g.paraphrases, images)
        if image_meta:
            for voter in j.record.get("panel", []):
                agent = self.pool.agent(voter["agent"])
                if agent.is_local:
                    continue
                for item in image_meta:
                    entry = {
                        "agent": agent.name, "host": agent.host, **item}
                    if entry not in self.image_egress:
                        self.image_egress.append(entry)
        self.cost["judge_votes"] += g.k
        if not j.certified:
            self.uncertified += 1
        sheet = self.sheets.get(task)
        name = task if j.verdict else f"neg:{task}"
        factor = (sheet["beta_lo"] if j.verdict
                  else 1 - sheet["alpha_hi"])
        self.add_theta(name, factor)
        if memo_key is not None:
            self.memo.put(memo_key, {"verdict": j.verdict,
                                     "factor_name": name,
                                     "factor": factor})
            if self.explore_depth == 0:
                self.memo_counted.add(memo_key)
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

    def display_for(self, actor: str | None, line: int) -> screen.Display:
        if actor is None:
            return self.default_display
        try:
            return self.displays[actor]
        except KeyError:
            raise KimiyaRuntimeError(
                f"line {line}: '{actor}' is not a declared display") \
                from None

    def act_screen(self, s: A.ActStmt, args: list):
        disp = self.display_for(s.actor, s.line)
        self.check_freshness(disp.target(), s.line)
        try:
            rec = screen.perform(s.action, args, disp=disp)
        except screen.ScreenError as e:
            raise KimiyaRuntimeError(f"line {s.line}: {e}") from None
        self.last_act[disp.target()] = time.time()
        self.screen_acts += 1
        self.trace.append({"kind": "act", "surface": "screen",
                           "action": s.action, "target": disp.target(),
                           **({"actor": s.actor} if s.actor else {}),
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
                if s.actor is not None:
                    # The wait was on this actor's world; a successful
                    # settle is an observation of it.
                    disp = self.display_for(s.actor, s.line)
                    self.last_obs[disp.target()] = time.time()
                return
            time.sleep(min(1.0, s.within / 10))
        raise Bolt(f"settle deadline ({s.within}s) elapsed at line {s.line}"
                   + (f" (actor {s.actor})" if s.actor else ""))

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
        self.cost["observes"] += 1
        if e.surface == "screen":
            return self.observe_screen(e)
        if e.surface == "image":
            if len(e.args) != 1:
                raise KimiyaRuntimeError(
                    f"line {e.line}: observe image expects one path")
            rec = image_surface.observe(
                self.eval(e.args[0]), self.workspace / "images")
            self.trace.append({
                "kind": "observe", "surface": "image", "line": e.line,
                **{k: v for k, v in rec.items() if k != "kind"},
            })
            if rec.get("exists"):
                meta = {
                    k: rec[k] for k in (
                        "path", "sha", "preview_sha", "decoder",
                        "width", "height", "format"
                    )
                }
                if meta not in self.image_observations:
                    self.image_observations.append(meta)
            return rec
        path = Path(str(self.eval(e.args[0])))
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

    def observe_screen(self, e: A.ObserveExpr):
        args = [self.eval(a) for a in e.args]
        disp = self.display_for(e.actor, e.line)
        try:
            rec = screen.capture(args, self.workspace / "shots", disp=disp)
        except screen.ScreenError as ex:
            raise KimiyaRuntimeError(f"line {e.line}: {ex}") from None
        self.last_obs[disp.target()] = time.time()
        self.trace.append({"kind": "observe", "surface": "screen",
                           "line": e.line,
                           **{k: v for k, v in rec.items() if k != "kind"}})
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

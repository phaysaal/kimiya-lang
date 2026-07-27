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
import os
import time
from pathlib import Path

from . import image as image_surface
from . import screen
from . import vision
from ._version import __version__ as KIMIYA_VERSION
from .runtime import (Pool, Agent, Trace, Datasheets, MemoStore,
                      get_oracle, run_judge, run_gen, resolve_params)


class Bolt(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_artifact_compat(compiled_with: str | None) -> str | None:
    """The compatibility contract for compiled artifacts.

    Policy: within a MAJOR version the runtime stays backward compatible —
    an older artifact runs, with a note when MINOR differs. Across MAJOR
    versions compatibility is not promised; the artifact refuses with a
    recompile instruction rather than failing somewhere ambiguous later.
    An artifact NEWER than the runtime also refuses — it may call runtime
    surface the installed kimiya does not have. Every break is recorded in
    CHANGELOG.md. Returns a warning string, or None."""
    if not compiled_with:
        return ("this artifact predates version stamping (< v1.4.0) — "
                "behavior is best-effort; recompile with `kimiya compile`")
    try:
        aj, an = (int(x) for x in compiled_with.split(".")[:2])
        rj, rn = (int(x) for x in KIMIYA_VERSION.split(".")[:2])
    except ValueError:
        return f"unparseable artifact version {compiled_with!r}"
    if aj != rj:
        raise SystemExit(
            f"artifact compiled with kimiya v{compiled_with}, but the "
            f"installed runtime is v{KIMIYA_VERSION} — MAJOR versions "
            "differ and compatibility is not promised across them (see "
            "CHANGELOG.md). Recompile: kimiya compile <program>.kim")
    if (aj, an) > (rj, rn):
        raise SystemExit(
            f"artifact compiled with kimiya v{compiled_with} is NEWER "
            f"than the installed runtime v{KIMIYA_VERSION} — it may use "
            "runtime surface this installation lacks. Upgrade kimiya or "
            "recompile with this version.")
    if an != rn:
        return (f"artifact compiled with v{compiled_with}; runtime is "
                f"v{KIMIYA_VERSION} — compatible (same MAJOR), but "
                "recompiling is recommended")
    return None


def parse_cli(argv, param_table):
    """The compiled artifact's CLI contract, shared with `kimiya run`:
    positional `name=value` pairs for declared params, `--models m1,m2`
    to override the pool. Errors exit before any model runs."""
    import sys as _sys
    models, pairs = None, {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--models":
            i += 1
            if i >= len(argv):
                _sys.exit("--models needs a comma-separated list")
            models = argv[i].split(",")
        elif a.startswith("--models="):
            models = a.split("=", 1)[1].split(",")
        elif "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            pairs[k] = v
        else:
            _sys.exit(f"unknown argument {a!r} — expected name=value "
                      "pairs or --models m1,m2")
        i += 1
    try:
        resolved = resolve_params(param_table, pairs)
    except ValueError as e:
        _sys.exit(f"refusing to run: {e}")
    return models, resolved


class Runtime:
    def __init__(self, source_file: str, agents, contexts, schemas,
                 py_exts=None, models_override=None, displays=None,
                 params=None, compiled_with=None):
        self.workspace = Path(source_file).parent / ".kimiya"
        self.workspace.mkdir(exist_ok=True)
        self.trace = Trace(self.workspace)
        self.sheets = Datasheets(self.workspace)
        self.locate_cache = vision.LocateCache(self.workspace)
        self.replay = os.environ.get("KIMIYA_REPLAY") == "1"
        self.displays = {d["name"]: screen.Display(
            name=d["name"], x11=d.get("x11"), ssh=d.get("ssh"),
            monitor=d.get("monitor")) for d in (displays or [])}
        self.default_display = screen.Display()
        self.params = dict(params or {})
        self.compiled_with = compiled_with
        note = check_artifact_compat(compiled_with)
        if note:
            print(f"⚠ {note}")
        self.memo = MemoStore(self.workspace)
        self.memo_counted: set = set()
        self.memo_hits = 0
        self.explore_depth = 0
        self.theta_excluded = 0
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
                key_env=a.get("key_env"), key_file=a.get("key_file"),
                zdr=a.get("zdr", False), family_override=a.get("family"),
                vision_declared=a.get("vision"))
        if models_override:
            binds = {f"M{i}": Agent(name=f"M{i}", model=m)
                     for i, m in enumerate(models_override)}
        self.pool = Pool(binds)
        self.theta: list = []
        self.cost = {"gen_calls": 0, "judge_votes": 0, "acts": 0,
                     "observes": 0}
        self.uncertified = 0
        self.screen_acts = 0
        self.locates = 0
        self.locates_cached = 0
        self.locates_replayed = 0
        self.overclaims = []
        self.image_observations: list[dict] = []
        self.image_egress: list[dict] = []
        self.last_gen: Agent | None = None
        self.committed = None

    def add_theta(self, name, factor):
        if self.explore_depth > 0:
            self.theta_excluded += 1
            self.trace.append({"kind": "theta_excluded", "task": name,
                               "factor": round(factor, 4)})
        else:
            self.theta.append((name, factor))

    def explore_push(self):
        self.explore_depth += 1
        self.trace.append({"kind": "explore", "phase": "enter"})

    def explore_pop(self):
        self.explore_depth -= 1
        self.trace.append({"kind": "explore", "phase": "exit"})

    def register(self, fns: dict, py_funcs: dict):
        self.fns = fns
        self.py_funcs = py_funcs

    # ---- primitive operations (mirror the interpreter) ----
    def gen(self, schema: str, prompt: str, by: str | None, images=None,
            memo=False, context=None):
        agent = self.pool.agent(by) if by else self.pool.default_generator()
        self.last_gen = agent
        prompt = _to_str(prompt)
        image_paths = None
        image_meta = []
        if images is not None:
            try:
                image_paths, image_meta = image_surface.prepare(images)
            except image_surface.ImageError as exc:
                raise Bolt(f"gen: {exc}") from None
            if image_paths and not agent.vision:
                raise Bolt(
                    f"gen: agent {agent.name} ({agent.model}) is not "
                    "vision-capable")
            if image_paths and not agent.is_local:
                for item in image_meta:
                    entry = {"agent": agent.name, "host": agent.host, **item}
                    if entry not in self.image_egress:
                        self.image_egress.append(entry)
        # gen with images is a priced READ: one θ factor at the
        # datasheet's conservative end, under read:<purpose>.
        read_task = (f"read:{context or 'unscoped'}"
                     if image_paths else None)
        if memo:
            image_key = ",".join(item["preview_sha"] for item in image_meta)
            key = MemoStore.key("gen", schema, prompt, agent.label(),
                                image_key)
            ent = self.memo.get(key)
            if ent is not None:
                self.memo_hits += 1
                self.trace.append({"kind": "gen", "cache": "memo",
                                   "agent": ent.get("agent", "")})
                if read_task and key not in self.memo_counted:
                    if self.explore_depth == 0:
                        self.memo_counted.add(key)
                    self.add_theta(ent.get("factor_name", read_task),
                                   ent.get("factor",
                                           self.sheets.get(read_task)
                                           ["beta_lo"]))
                return ent["value"]
        self.cost["gen_calls"] += 1
        if schema == "Text":
            out = run_gen(self.oracle, self.trace, agent, prompt, None,
                          images=image_paths)
        elif schema == "Json":
            out = run_gen(self.oracle, self.trace, agent,
                          prompt + "\n\nFIELDS: result", ["result"],
                          images=image_paths)
        else:
            out = run_gen(self.oracle, self.trace, agent, prompt,
                          self.schemas[schema], images=image_paths)
        factor = None
        if read_task and out is not None:
            factor = self.sheets.get(read_task)["beta_lo"]
            self.add_theta(read_task, factor)
        if memo and out is not None:
            entry = {"value": out, "agent": agent.label()}
            if read_task:
                entry["factor_name"] = read_task
                entry["factor"] = factor
            self.memo.put(key, entry)
            if read_task and self.explore_depth == 0:
                self.memo_counted.add(key)
        return out

    def select(self, recall: float, query: str, store, ctx, by=None):
        if isinstance(store, dict) and store.get("kind") == "screen":
            return self.select_vision(recall, query, store, ctx, by)
        words = {w for w in str(query).lower().split() if len(w) > 3}

        def score(x):
            t = _to_str(x).lower()
            return sum(1 for w in words if w in t)

        hits = [x for x in sorted(store, key=lambda x: -score(x))
                if score(x) > 0] or list(store)
        self.add_theta(f"select<{recall}>", recall)
        self.trace.append({"kind": "select", "recall": recall,
                           "store_size": len(store), "hits": len(hits)})
        return hits

    def select_vision(self, recall, query, shot, ctx, by):
        if not shot.get("exists"):
            raise Bolt("select: no screenshot to look at — observe screen "
                       "returned exists: false")
        agent = (self.pool.agent(by) if by
                 else self.pool.default_generator())
        purpose = self.contexts.get(ctx, ctx or "unscoped")
        try:
            hits, source = vision.locate(
                self.oracle, agent, self.trace, shot, _to_str(query),
                purpose, ctx, cache=self.locate_cache, replay=self.replay)
        except vision.ReplayMiss as e:
            raise Bolt(f"select: {e}") from None
        task = vision.locate_task(ctx)
        sheet = self.sheets.get(task)
        self.locates += 1
        if source == "exact":
            self.locates_cached += 1
        elif source == "replay":
            self.locates_replayed += 1
        self.add_theta(task if hits else f"neg:{task}",
                       sheet["beta_lo"] if hits
                       else 1 - sheet["alpha_hi"])
        if recall > sheet["beta_lo"]:
            note = (f"declared recall {recall} exceeds the measured "
                    f"β≥{sheet['beta_lo']:.3f} of instrument {task} — θ "
                    "uses the measured end, not the claim")
            if note not in self.overclaims:
                self.overclaims.append(note)
            self.trace.append({"kind": "overclaim", "task": task,
                               "declared_recall": recall,
                               "measured_beta_lo": sheet["beta_lo"]})
        return hits

    def judge(self, k, tau, relation, left, right, ctx_name,
              panel, paraphrases, memo=False):
        purpose = self.contexts.get(ctx_name, ctx_name)
        task = f"{relation}:{ctx_name}"
        images = None
        image_meta = []
        if relation == "shows":
            shot = left
            if not (isinstance(shot, dict)
                    and shot.get("kind") in ("screen", "image")):
                raise Bolt("shows(...) needs an observed image as its "
                           "first argument")
            if not shot.get("exists"):
                raise Bolt("shows: image observation returned exists: false")
            if shot.get("kind") == "screen":
                images = [shot["path"]]
                evidence = (
                    f"screenshot of {shot['region']} on "
                    f"{shot['display']}, {shot['width']}x"
                    f"{shot['height']} at origin ({shot['x']}, "
                    f"{shot['y']}), sha {shot['sha']}"
                )
            else:
                try:
                    images, image_meta = image_surface.prepare([shot])
                except image_surface.ImageError as exc:
                    raise Bolt(f"shows: {exc}") from None
                evidence = (
                    f"observed {shot['format']} image {shot['width']}x"
                    f"{shot['height']}, source sha {shot['sha']}, "
                    f"preview sha {shot['preview_sha']}"
                )
            claim = f"The observed image shows: {_to_str(right)}"
            return self._run_judge(k, tau, task, claim, evidence, purpose,
                                   panel, paraphrases, images, memo,
                                   image_meta)
        left, right = _to_str(left), _to_str(right) if right is not None else ""
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
        return self._run_judge(k, tau, task, claim, evidence, purpose,
                               panel, paraphrases, None, memo)

    def _run_judge(self, k, tau, task, claim, evidence, purpose, panel,
                   paraphrases, images=None, memo=False, image_meta=None):
        memo_key = None
        if memo:
            memo_key = MemoStore.key("judge", task, claim, evidence,
                                     k, tau, ",".join(panel or []))
            ent = self.memo.get(memo_key)
            if ent is not None:
                self.memo_hits += 1
                self.trace.append({"kind": "judge", "cache": "memo",
                                   "task": task,
                                   "verdict": ent["verdict"]})
                if memo_key not in self.memo_counted:
                    if self.explore_depth == 0:
                        self.memo_counted.add(memo_key)
                    self.add_theta(ent["factor_name"], ent["factor"])
                return ent["verdict"]
        gen = self.last_gen or self.pool.default_generator()
        j = run_judge(self.pool, self.oracle, self.trace, self.sheets,
                      task, claim, evidence, gen, k, tau, purpose,
                      panel, paraphrases, images)
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
        self.cost["judge_votes"] += k
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

    def check(self, value, line=0):
        self.trace.append({"kind": "check", "ok": bool(value), "line": line})
        if not value:
            raise Bolt(f"check failed at line {line}")
        return True

    def check_guard(self, value):
        self.trace.append({"kind": "check_guard", "ok": bool(value)})
        return bool(value)

    def commit(self, value):
        if self.explore_depth > 0:
            raise Bolt("commit while exploring — judged factors here are "
                       "excluded from θ; commit outside the explore block")
        self.committed = value
        self.trace.append({"kind": "commit"})

    def abstain(self, line=0):
        raise Bolt(f"abstain at line {line}")

    def _display_for(self, actor):
        if actor is None:
            return self.default_display
        try:
            return self.displays[actor]
        except KeyError:
            raise Bolt(f"'{actor}' is not a declared display") from None

    def observe(self, surface, args, actor=None):
        self.cost["observes"] += 1
        if surface == "screen":
            try:
                rec = screen.capture(args, self.workspace / "shots",
                                     disp=self._display_for(actor))
            except screen.ScreenError as e:
                raise Bolt(str(e)) from None
            self.trace.append({"kind": "observe", "surface": "screen",
                               **{k: v for k, v in rec.items()
                                  if k != "kind"}})
            return rec
        if surface == "image":
            if len(args) != 1:
                raise Bolt("observe image expects one path")
            rec = image_surface.observe(
                args[0], self.workspace / "images")
            self.trace.append({
                "kind": "observe", "surface": "image",
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
        path = Path(_to_str(args[0]))
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

    def act(self, surface, action, args, actor=None):
        self.cost["acts"] += 1
        if surface == "screen":
            self.screen_acts += 1
            disp = self._display_for(actor)
            try:
                rec = screen.perform(action, args, disp=disp)
            except screen.ScreenError as e:
                raise Bolt(str(e)) from None
            self.trace.append({"kind": "act", "surface": "screen",
                               "action": action, "target": disp.target(),
                               **({"actor": actor} if actor else {}),
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
        tasks = sorted({n[4:] if n.startswith("neg:") else n
                        for n, _ in self.theta
                        if not n.startswith("select")})
        egress = sorted({a.host for a in self.pool.agents if not a.is_local})
        cert = {
            "status": status, "reason": reason, "value": self.committed,
            "theta": round(theta, 4),
            "theta_factors": [(n, round(x, 4)) for n, x in self.theta],
            "uncertified_judgments": self.uncertified,
            "instruments": {t: self.sheets.get(t) for t in tasks},
            "python_extensions": self.py_exts,
            "egress": egress,
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
            "params": self.params,
            "kimiya_version": KIMIYA_VERSION,
            "compiled_with": self.compiled_with,
            "memo_hits": self.memo_hits,
            "explored": self.theta_excluded,
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
        if self.image_egress:
            print(f"  image egress : {len(self.image_egress)} observed "
                  "image disclosure(s)")
        elif self.image_observations:
            print("  image egress : none (observed pixels stayed local)")
        if self.params:
            shown = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
            print(f"  params : {shown}")
        if self.memo_hits:
            print(f"  memo   : {self.memo_hits} reuse(s) — identical "
                  "readings, factors counted once")
        if self.theta_excluded:
            print(f"  ⚑ explored : {self.theta_excluded} judged/select "
                  "factor(s) inside explore — excluded from θ")
        if cert["screen"]:
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
            if sc.get("locates_replayed"):
                print(f"  ⚠ {sc['locates_replayed']} locate(s) replayed "
                      "from a prior run against changed pixels — layout "
                      "stability is assumed, not measured; the verdict "
                      "gates (checks, judges) still ran live")
        for note in cert["overclaims"]:
            print(f"  ⚠ {note}")
        c = cert["cost"]
        print(f"  kimiya : v{KIMIYA_VERSION}"
              + (f" · artifact compiled with v{self.compiled_with}"
                 if self.compiled_with and
                 self.compiled_with != KIMIYA_VERSION else ""))
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

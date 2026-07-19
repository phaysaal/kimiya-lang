"""Local-model runtime for the Kimiya interpreter.

The single network touchpoint of the interpreter: 127.0.0.1 only (port via
KIMIYA_OLLAMA_PORT). Judge panels enforce J-not-lhd-C; datasheets carry
conservative Wilson ends from locally collected labels; every judgment and
generation appends to an append-only trace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_HOST = "127.0.0.1"   # loopback only, never configurable
ALLOWED_PORT = int(os.environ.get("KIMIYA_OLLAMA_PORT", "11434"))
BASE_URL = f"http://{ALLOWED_HOST}:{ALLOWED_PORT}"

FAMILY_PATTERNS = [
    ("llama", "llama"), ("qwen", "qwen"), ("mistral", "mistral"),
    ("mixtral", "mistral"), ("gemma", "gemma"), ("phi", "phi"),
    ("deepseek", "deepseek"), ("granite", "granite"),
    ("command-r", "cohere"), ("olmo", "olmo"), ("smollm", "smollm"),
]


def family_of(model: str) -> str:
    m = model.lower()
    for pat, fam in FAMILY_PATTERNS:
        if pat in m:
            return fam
    return m.split(":")[0]


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


PRIOR_SHEET = {"alpha_hi": 0.25, "beta_lo": 0.60,
               "n_true": 0, "n_false": 0, "calibrated": False}


class Datasheets:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.labels_path = self.workspace / "labels.jsonl"
        self.local_path = self.workspace / "datasheets.json"
        self._sheets: dict[str, dict] = {}
        if self.local_path.exists():
            self._sheets = json.loads(self.local_path.read_text())

    def get(self, task: str) -> dict:
        return dict(self._sheets.get(task, PRIOR_SHEET))

    def add_label(self, task: str, truth: bool, verdict: bool):
        with self.labels_path.open("a") as f:
            f.write(json.dumps({"task": task, "truth": truth,
                                "verdict": verdict, "ts": time.time()}) + "\n")

    def recompute(self):
        by: dict[str, dict] = {}
        if self.labels_path.exists():
            for line in self.labels_path.read_text().splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                t = by.setdefault(r["task"], {"tp": 0, "n_true": 0,
                                              "fp": 0, "n_false": 0})
                if r["truth"]:
                    t["n_true"] += 1
                    t["tp"] += int(r["verdict"])
                else:
                    t["n_false"] += 1
                    t["fp"] += int(r["verdict"])
        for task, c in by.items():
            self._sheets[task] = {
                "alpha_hi": _wilson(c["fp"], c["n_false"])[1],
                "beta_lo": _wilson(c["tp"], c["n_true"])[0],
                "n_true": c["n_true"], "n_false": c["n_false"],
                "calibrated": c["n_true"] >= 10 and c["n_false"] >= 10,
            }
        self.local_path.write_text(json.dumps(self._sheets, indent=2))
        return self._sheets


class Trace:
    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "trace.jsonl"

    def append(self, record: dict):
        record = dict(record)
        record["ts"] = time.time()
        with self.path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False,
                               default=str) + "\n")

    def count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text().splitlines()
                   if line.strip())


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


class Oracle:
    def __init__(self, timeout: int = 900):
        self.timeout = int(os.environ.get("KIMIYA_TIMEOUT", timeout))

    def models(self) -> list[str]:
        req = urllib.request.Request(BASE_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]

    def complete(self, model: str, prompt: str, system: str = "",
                 temperature: float = 0.2, max_tokens: int = 1024) -> str:
        payload = {"model": model, "prompt": prompt, "system": system,
                   "stream": False,
                   "options": {"temperature": temperature,
                               "num_predict": max_tokens}}
        req = urllib.request.Request(
            BASE_URL + "/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data.get("response", "")


class MockOracle(Oracle):
    """KIMIYA_MOCK=1: deterministic, offline. YES unless the claim text
    contains 'MOCKNO'; gen fills schema fields with plausible strings."""

    def models(self) -> list[str]:
        return ["mock-llama:1b", "mock-qwen:1b", "mock-gemma:1b"]

    def complete(self, model: str, prompt: str, system: str = "",
                 temperature: float = 0.2, max_tokens: int = 1024) -> str:
        if "Answer with exactly YES or NO" in system:
            return "NO" if "MOCKNO" in prompt else "YES"
        m = re.search(r"FIELDS: ([a-z_, ]+)", prompt)
        if m:
            fields = [f.strip() for f in m.group(1).split(",") if f.strip()]
            return json.dumps({f: f"mock {f}" for f in fields})
        return "mock text output"


def get_oracle() -> Oracle:
    if os.environ.get("KIMIYA_MOCK") == "1":
        return MockOracle()
    return Oracle()


@dataclass
class Pool:
    bindings: dict           # pool name -> model, insertion-ordered

    @property
    def models(self) -> list[str]:
        return list(self.bindings.values())

    def model(self, name: str) -> str:
        return self.bindings[name]

    def default_generator(self) -> str:
        return self.models[0]

    def panel_for(self, generator: str, k: int,
                  names: list[str] | None) -> tuple[list[str], bool]:
        gen_fam = family_of(generator)
        cand = ([self.bindings[n] for n in names] if names
                else self.models)
        others = [m for m in cand if family_of(m) != gen_fam]
        if others:
            return (others * ((k // len(others)) + 1))[:k], True
        if not cand:
            cand = [generator]
        return (cand * k)[:k], False


JUDGE_SYSTEM = (
    "You are a strict verifier. Read the evidence and the claim. "
    "Answer with exactly YES or NO on the first line. YES only if the "
    "evidence clearly supports the claim. If unsure, answer NO."
)
GEN_SYSTEM = (
    "You are a careful generation engine. Return ONLY a JSON object with "
    "exactly the requested fields, no prose, no markdown fences."
)


@dataclass
class Judgment:
    verdict: bool
    votes: int
    k: int
    certified: bool
    task: str
    record: dict = field(default_factory=dict)


def run_judge(pool: Pool, oracle: Oracle, trace: Trace, sheets: Datasheets,
              task: str, claim: str, evidence: str, generator: str,
              k: int, tau: float, purpose: str,
              panel_names: list[str] | None, paraphrases: int) -> Judgment:
    panel, certified = pool.panel_for(generator, k, panel_names)
    base = (f"PURPOSE: {purpose}\n\nEVIDENCE:\n{evidence[:6000]}\n\n"
            f"CLAIM: {claim}\n\nDoes the evidence support the claim?")
    variants = [base]
    if paraphrases > 1:
        variants.append(base.replace(
            "Does the evidence support the claim?",
            "Is the claim warranted by the evidence above?"))
    votes, voters = 0, []
    for i, model in enumerate(panel):
        p = variants[i % len(variants)]
        try:
            out = oracle.complete(model, p, system=JUDGE_SYSTEM,
                                  temperature=0.1, max_tokens=8)
            vote = out.strip().upper().startswith("YES")
            err = None
        except OSError as e:
            vote, err = False, str(e)[:80]
        votes += int(vote)
        voters.append({"model": model, "vote": vote,
                       **({"error": err} if err else {})})
    verdict = votes >= math.ceil(tau * k)
    rec = {"kind": "judge", "task": task, "purpose": purpose,
           "claim": claim[:300], "claim_hash": h(claim),
           "evidence_hash": h(evidence), "votes": votes, "k": k,
           "tau": tau, "verdict": verdict, "certified": certified,
           "panel": voters, "generator": generator,
           "datasheet": sheets.get(task)}
    trace.append(rec)
    return Judgment(verdict, votes, k, certified, task, rec)


def run_gen(oracle: Oracle, trace: Trace, model: str, prompt: str,
            schema_fields: list[str] | None, budget: int = 3):
    """schema_fields None => free text; else JSON with those fields."""
    if schema_fields is None:
        try:
            out = oracle.complete(model, prompt, temperature=0.3)
            trace.append({"kind": "gen", "model": model,
                          "prompt_hash": h(prompt), "ok": True})
            return out.strip()
        except OSError as e:
            trace.append({"kind": "gen", "model": model,
                          "prompt_hash": h(prompt), "ok": False,
                          "error": str(e)[:80]})
            return None
    want = ", ".join(schema_fields)
    full = f"{prompt}\n\nFIELDS: {want}\nReturn ONLY a JSON object."
    for attempt in range(budget):
        try:
            out = oracle.complete(model, full, system=GEN_SYSTEM,
                                  temperature=0.3 + 0.2 * attempt)
        except OSError as e:
            trace.append({"kind": "gen", "model": model, "attempt": attempt,
                          "prompt_hash": h(full), "ok": False,
                          "error": str(e)[:80]})
            continue
        text = re.sub(r"^```(json)?|```$", "", out.strip(), flags=re.M).strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            continue
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if all(f in obj for f in schema_fields):
            trace.append({"kind": "gen", "model": model, "attempt": attempt,
                          "prompt_hash": h(full), "ok": True})
            return obj
    trace.append({"kind": "gen", "model": model, "prompt_hash": h(full),
                  "ok": False, "budget_exhausted": True})
    return None

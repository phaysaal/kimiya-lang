"""Local-model runtime for the Kimiya interpreter.

The single network touchpoint of the interpreter: 127.0.0.1 only (port via
KIMIYA_OLLAMA_PORT). Judge panels enforce J-not-lhd-C; datasheets carry
conservative Wilson ends from locally collected labels; every judgment and
generation appends to an append-only trace.
"""

from __future__ import annotations

import base64
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

# Models that can read an image. Used to reject, at check time, a vision
# instrument pointed at a text-only model — the failure mode otherwise is
# a confident answer about an image the model never saw.
VISION_PATTERNS = [
    "llava", "vision", "-vl", "pixtral", "minicpm-v", "moondream",
    "gemma3", "internvl", "multimodal", "bakllava", "granite3.2-vision",
    "gpt-4o", "gpt-4.1", "gpt-5", "claude", "gemini", "qwen3-vl",
    "mistral-small3", "nova-lite", "nova-pro", "grok-vision",
]

FAMILY_PATTERNS = [
    ("llama", "llama"), ("qwen", "qwen"), ("mistral", "mistral"),
    ("mixtral", "mistral"), ("gemma", "gemma"), ("phi", "phi"),
    ("deepseek", "deepseek"), ("granite", "granite"),
    ("command-r", "cohere"), ("olmo", "olmo"), ("smollm", "smollm"),
    ("claude", "anthropic"), ("gpt", "openai"), ("o3", "openai"),
    ("grok", "xai"), ("nova", "amazon"),
]
PROVIDER_FAMILIES = {
    "anthropic": "anthropic", "openai": "openai", "google": "google",
    "meta-llama": "llama", "mistralai": "mistral", "qwen": "qwen",
    "deepseek": "deepseek", "x-ai": "xai", "cohere": "cohere",
    "amazon": "amazon", "microsoft": "phi",
}


def family_of(model: str) -> str:
    m = model.lower()
    if m.startswith("override://"):   # a declared family override
        return m.split("://", 1)[1]
    if "/" in m:                      # openrouter-style provider/model
        provider = m.split("/", 1)[0]
        if provider in PROVIDER_FAMILIES:
            return PROVIDER_FAMILIES[provider]
    for pat, fam in FAMILY_PATTERNS:
        if pat in m:
            return fam
    return m.split("/")[-1].split(":")[0]


@dataclass
class Agent:
    """A declared model instance: who it is, where it runs, its family.

    Backends: "ollama" (default; local unless url says otherwise),
    "openai" (any OpenAI-compatible /v1 endpoint — vLLM on a vast.ai pod,
    llama.cpp server, LM Studio), "openrouter". API keys never appear in
    source; key_env names an environment variable.
    """

    name: str
    model: str
    backend: str = "ollama"
    url: str | None = None
    key_env: str | None = None
    family_override: str | None = None
    vision_declared: bool | None = None

    @property
    def family(self) -> str:
        return self.family_override or family_of(self.model)

    @property
    def vision(self) -> bool:
        """Can this agent read an image? Declared wins over inferred."""
        if self.vision_declared is not None:
            return self.vision_declared
        if os.environ.get("KIMIYA_MOCK") == "1":
            return True
        m = self.model.lower()
        return any(p in m for p in VISION_PATTERNS)

    @property
    def resolved_url(self) -> str:
        if self.url:
            return self.url.rstrip("/")
        if self.backend == "openrouter":
            return "https://openrouter.ai/api/v1"
        return BASE_URL

    @property
    def host(self) -> str:
        u = self.resolved_url
        return u.split("//", 1)[-1].split("/", 1)[0]

    @property
    def is_local(self) -> bool:
        return self.host.split(":")[0] in ("127.0.0.1", "localhost")

    def label(self) -> str:
        where = "local" if self.is_local else self.host
        return f"{self.name}={self.model}@{where}"


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

    def install(self, task: str, sheet: dict):
        """Record an instrument measured elsewhere.

        Calibration from this workspace's own labels is the normal path.
        But an instrument may have been measured by a separate campaign —
        a harness that ran it hundreds of times against known ground
        truth — and re-deriving that here would be busywork. Such a sheet
        carries its `source` into every certificate that uses it, so a
        reviewer can see the numbers were imported and from where.
        """
        self._sheets[task] = dict(sheet)
        self.local_path.write_text(json.dumps(self._sheets, indent=2))

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


def _b64(path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


class Oracle:
    def __init__(self, timeout: int = 900):
        self.timeout = int(os.environ.get("KIMIYA_TIMEOUT", timeout))

    def models(self) -> list[str]:
        req = urllib.request.Request(BASE_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]

    def complete(self, agent: Agent, prompt: str, system: str = "",
                 temperature: float = 0.2, max_tokens: int = 1024,
                 images: list | None = None) -> str:
        if images and not agent.vision:
            raise RuntimeError(
                f"agent {agent.name} ({agent.model}) was given an image but "
                "is not vision-capable — it would answer about an image it "
                "never saw. Declare `vision = true` if it can in fact see.")
        if agent.backend == "ollama":
            return self._ollama(agent, prompt, system, temperature,
                                max_tokens, images)
        if agent.backend in ("openai", "openrouter"):
            return self._chat(agent, prompt, system, temperature,
                              max_tokens, images)
        raise RuntimeError(f"unknown backend '{agent.backend}'")

    def _ollama(self, agent, prompt, system, temperature, max_tokens,
                images=None):
        payload = {"model": agent.model, "prompt": prompt,
                   "system": system, "stream": False,
                   "options": {"temperature": temperature,
                               "num_predict": max_tokens}}
        if images:
            payload["images"] = [_b64(p) for p in images]
        req = urllib.request.Request(
            agent.resolved_url + "/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data.get("response", "")

    def _chat(self, agent, prompt, system, temperature, max_tokens,
              images=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            parts = [{"type": "text", "text": prompt}]
            for p in images:
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{_b64(p)}"}})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": prompt})
        payload = {"model": agent.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Content-Type": "application/json"}
        if agent.key_env:
            key = os.environ.get(agent.key_env, "")
            if not key:
                raise RuntimeError(
                    f"agent {agent.name}: environment variable "
                    f"{agent.key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            agent.resolved_url + "/chat/completions",
            data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content", "") or ""


class MockOracle(Oracle):
    """KIMIYA_MOCK=1: deterministic, offline. YES unless the claim text
    contains 'MOCKNO'; gen fills schema fields with plausible strings;
    locate returns one box placed deterministically from the query hash,
    so different descriptions land on different points."""

    def models(self) -> list[str]:
        return ["mock-llama:1b", "mock-qwen:1b", "mock-gemma:1b"]

    def complete(self, agent: Agent, prompt: str, system: str = "",
                 temperature: float = 0.2, max_tokens: int = 1024,
                 images: list | None = None) -> str:
        if "LOCATE" in system:
            if "MOCKMISS" in prompt:
                return "[]"
            seed = int(h(prompt), 16)
            x0, y0 = 100 + seed % 700, 100 + (seed >> 12) % 700
            return json.dumps([{"box_2d": [y0, x0, y0 + 40, x0 + 120],
                                "label": "mock control",
                                "confidence": 0.9}])
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
    bindings: dict           # agent name -> Agent, insertion-ordered

    @property
    def agents(self) -> list[Agent]:
        return list(self.bindings.values())

    def agent(self, name: str) -> Agent:
        return self.bindings[name]

    def default_generator(self) -> Agent:
        return self.agents[0]

    def panel_for(self, generator: Agent, k: int,
                  names: list[str] | None) -> tuple[list[Agent], bool]:
        gen_fam = generator.family
        cand = ([self.bindings[n] for n in names] if names
                else self.agents)
        others = [a for a in cand if a.family != gen_fam]
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
              task: str, claim: str, evidence: str, generator: Agent,
              k: int, tau: float, purpose: str,
              panel_names: list[str] | None, paraphrases: int,
              images: list | None = None) -> Judgment:
    panel, certified = pool.panel_for(generator, k, panel_names)
    if images:
        base = (f"PURPOSE: {purpose}\n\nThe attached screenshot is the "
                f"evidence.\n{evidence[:2000]}\n\nCLAIM: {claim}\n\n"
                "Does the screenshot support the claim?")
    else:
        base = (f"PURPOSE: {purpose}\n\nEVIDENCE:\n{evidence[:6000]}\n\n"
                f"CLAIM: {claim}\n\nDoes the evidence support the claim?")
    variants = [base]
    if paraphrases > 1:
        variants.append(base.replace(
            "Does the evidence support the claim?",
            "Is the claim warranted by the evidence above?").replace(
            "Does the screenshot support the claim?",
            "Is the claim warranted by the screenshot above?"))
    votes, voters = 0, []
    for i, agent in enumerate(panel):
        p = variants[i % len(variants)]
        try:
            out = oracle.complete(agent, p, system=JUDGE_SYSTEM,
                                  temperature=0.1, max_tokens=8,
                                  images=images)
            vote = out.strip().upper().startswith("YES")
            err = None
        except (OSError, RuntimeError) as e:
            vote, err = False, str(e)[:80]
        votes += int(vote)
        voters.append({"agent": agent.name, "model": agent.model,
                       "host": agent.host, "vote": vote,
                       **({"error": err} if err else {})})
    verdict = votes >= math.ceil(tau * k)
    rec = {"kind": "judge", "task": task, "purpose": purpose,
           "claim": claim[:300], "claim_hash": h(claim),
           "evidence_hash": h(evidence),
           **({"images": [str(p) for p in images]} if images else {}),
           "votes": votes, "k": k,
           "tau": tau, "verdict": verdict, "certified": certified,
           "panel": voters, "generator": generator.label(),
           "datasheet": sheets.get(task)}
    trace.append(rec)
    return Judgment(verdict, votes, k, certified, task, rec)


def run_gen(oracle: Oracle, trace: Trace, agent: Agent, prompt: str,
            schema_fields: list[str] | None, budget: int = 3):
    """schema_fields None => free text; else JSON with those fields."""
    who = agent.label()
    if schema_fields is None:
        try:
            out = oracle.complete(agent, prompt, temperature=0.3)
            trace.append({"kind": "gen", "agent": who,
                          "prompt_hash": h(prompt), "ok": True})
            return out.strip()
        except (OSError, RuntimeError) as e:
            trace.append({"kind": "gen", "agent": who,
                          "prompt_hash": h(prompt), "ok": False,
                          "error": str(e)[:80]})
            return None
    want = ", ".join(schema_fields)
    full = f"{prompt}\n\nFIELDS: {want}\nReturn ONLY a JSON object."
    for attempt in range(budget):
        try:
            out = oracle.complete(agent, full, system=GEN_SYSTEM,
                                  temperature=0.3 + 0.2 * attempt)
        except (OSError, RuntimeError) as e:
            trace.append({"kind": "gen", "agent": who, "attempt": attempt,
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
            trace.append({"kind": "gen", "agent": who, "attempt": attempt,
                          "prompt_hash": h(full), "ok": True})
            return obj
    trace.append({"kind": "gen", "agent": who, "prompt_hash": h(full),
                  "ok": False, "budget_exhausted": True})
    return None
